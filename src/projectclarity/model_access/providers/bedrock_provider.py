"""Implement the currently supported Nova Micro path through AWS Bedrock Converse.

Despite the generic filename and class name, this is NOT a general adapter for
all Bedrock models. It accepts only the listed Nova Micro model IDs and combines
AWS client/authentication code with Nova-specific limits, counting estimates and
structured-output instructions. Those concerns have not yet been separated into
generic Bedrock transport and model-specific strategies.

When a response schema is requested, the default mode adds JSON instructions and
an opening-brace assistant prefix. A shared codec parses the returned continuation;
this does not guarantee schema compliance or correct answers. An explicit
experimental alternative encodes nullable values as zero/one-element lists in a
tool response, which is never executed. There is no automatic mode fallback.

Replies and usage are retained for caller-side validation and accounting. Nova
Micro input counting uses a labelled local estimate, not an exact tokenizer or a
proven upper bound. The adapter does not repair factual content or retry requests."""
import json
from contextlib import closing

import boto3
from botocore.config import Config
from botocore.session import Session
from botocore.tokens import ScopedEnvTokenProvider, TokenProviderChain

from projectclarity.model_access.providers.contracts import (
    Capabilities, ModelResponse, PreparedRequest, TokenCount, effective_request,
)
from projectclarity.model_access.providers.structured_output import (
    NullableListCodec, JSONTextCodec, OutputStructureError, inline_schema,
)

TOOL_NAME = 'submit_result'
NOVA_MICRO_MODELS = {'amazon.nova-micro-v1:0', 'us.amazon.nova-micro-v1:0', 'eu.amazon.nova-micro-v1:0'}


def tool_schema(schema):
    """Inline local schema references and adapt Nova's restricted root object.

    Args: schema: Original application JSON schema, preserved unchanged.
    Returns: Detached tool schema with local refs expanded and root limited to
        type/properties/required. Local validation still enforces all original
        constraints. Unsupported root constraints, cyclic/external refs or constrained ref siblings raise
        ValueError rather than silently dropping substantive rules. No I/O occurs.
    """
    expanded = inline_schema(schema)
    if expanded.get('type') != 'object':
        raise ValueError('Nova structured responses require an object schema.')
    unsupported = set(expanded) - {'type', 'properties', 'required', 'title', 'description', 'additionalProperties', '$schema'}
    if unsupported:
        raise ValueError('Unsupported Nova root schema constraints: ' + ', '.join(sorted(unsupported)))
    return {k: v for k, v in expanded.items() if k in ('type', 'properties', 'required')}


class BedrockProvider:
    """Nova Micro-only implementation; not generic support for Bedrock models."""
    # Nova Micro profile: output cap and input-plus-output admission ceiling.
    # These settings must not be applied to other Bedrock models without qualification.
    capabilities = Capabilities(10000, 128000, 'local_utf8_estimate', 'json_prefill_v1')

    def __init__(self, config):
        """Validate Nova configuration without changing environment or making calls.

        Args: config: Private settings, selected provider and model ID.
        Returns: None. Requires a Bedrock bearer token and AWS_REGION. Only the
            declared Nova Micro IDs are supported; no implicit model fallback.
            PROJECTCLARITY_BEDROCK_OUTPUT selects json_text (default) or the
            experimental tool_nullable mode; invalid values raise ValueError.
        """
        if config.model not in NOVA_MICRO_MODELS:
            raise ValueError('Bedrock adapter currently supports Nova Micro only; add a capability profile for other models.')
        self.output_mode = config.values.get('PROJECTCLARITY_BEDROCK_OUTPUT') or 'json_text'
        if self.output_mode not in ('json_text', 'tool_nullable'):
            raise ValueError('Bedrock output mode must be json_text or tool_nullable.')
        self.capabilities = Capabilities(10000, 128000, 'local_utf8_estimate',
            'json_prefill_v1' if self.output_mode == 'json_text' else 'forced_schema_tool_nullable_list_v1')
        self.key = (config.values.get('AWS_BEARER_TOKEN_BEDROCK') or '').strip()
        self.region = (config.values.get('AWS_REGION') or '').strip()
        if not self.key or not self.region:
            raise ValueError('Bedrock requires AWS_BEARER_TOKEN_BEDROCK and AWS_REGION.')
        self.redaction_values = (self.key,)

    def _client(self, timeout):
        """Create one SDK client with a private token resolver and retries disabled.

        Args: timeout: Read timeout in seconds for this request.
        Returns: Bedrock runtime client; caller closes it. Only the explicit region
            and bearer token are used. No shared environment or default session is
            mutated, so different configurations cannot exchange credentials.
        """
        session = Session()
        session.register_component('token_provider', TokenProviderChain([
            ScopedEnvTokenProvider(session, environ={'AWS_BEARER_TOKEN_BEDROCK': self.key})]))
        return boto3.Session(botocore_session=session).client(
            'bedrock-runtime', region_name=self.region,
            config=Config(signature_version='bearer', connect_timeout=10, read_timeout=timeout,
                          retries={'mode': 'standard', 'total_max_attempts': 1}))

    def prepare(self, request):
        """Translate a canonical request using the explicitly configured output mode.

        Args: request: Common text/JSON request with desired output allowance.
        Returns: PreparedRequest capped to 10000 output tokens with original cap
            retained separately. Structured replies use JSON prefilling by default, or the explicitly
            selected experimental nullable-list tool mode. Both use temperature
            zero and retain the canonical schema for ordinary validation.
            No source truncation or I/O; unsupported schemas fail before dispatch.
        """
        if request.get('model') not in NOVA_MICRO_MODELS:
            raise ValueError('Unsupported Nova Micro model ID.')
        effective = effective_request(request, self.capabilities)
        wire = {'modelId': effective['model'],
                'messages': [{'role': m['role'], 'content': [{'text': m['content']}]} for m in effective['messages']],
                'inferenceConfig': {'maxTokens': effective['max_tokens']}}
        if effective.get('system'):
            wire['system'] = [{'text': effective['system']}]
        codec = None
        if 'response_schema' in effective and self.output_mode == 'json_text':
            schema = inline_schema(effective['response_schema'])
            if schema.get('type') != 'object':
                raise ValueError('Nova JSON prefilling requires an object response schema.')
            codec = JSONTextCodec(schema)
            instructions = ('Return one complete JSON object matching the response schema. '
                'Do not use Markdown fences or explanatory text. Include every required field, '
                'including required fields whose value is null. Use JSON null, not the text '
                '"null", for an absent value. Preserve all supported information required by '
                'the task; do not return empty results merely to simplify formatting. '
                'Continue the assistant response from its opening brace.\nResponse schema:\n')
            wire.setdefault('system', []).append({'text': instructions + json.dumps(schema, ensure_ascii=False)})
            wire['messages'].append({'role': 'assistant', 'content': [{'text': codec.prefix}]})
            wire['inferenceConfig']['temperature'] = 0
        elif 'response_schema' in effective:
            codec = NullableListCodec(effective['response_schema'])
            wire['inferenceConfig']['temperature'] = 0
            wire['toolConfig'] = {'tools': [{'toolSpec': {
                'name': TOOL_NAME, 'description': (
                    'Return the complete result of the task. Include every required field '
                    'in every object, including nested records. Nullable values '
                    'are represented as lists: use [] for no value and [value] for '
                    'one present value, exactly as described in the field schema. '
                    'Never write the word "null" to mean no value. Include every '
                    'required list. Do not return only part of the result. '
                    'The result must include: ' + ', '.join(codec.schema.get('required', [])) + '.'),
                'inputSchema': {'json': tool_schema(codec.schema)}}}],
                'toolChoice': {'tool': {'name': TOOL_NAME}}}
        return PreparedRequest(effective, wire, request['max_tokens'], codec)

    def count(self, prepared):
        """Estimate the full serialized input including expanded schema overhead.

        Args: prepared: Exact Converse request used for generation.
        Returns: One token per UTF-8 input byte plus 2048 framing headroom, explicitly
            labelled estimated. This deliberately over-reserves ordinary text but
            is not an exact tokenizer or guaranteed upper bound. No count API call.
            Oversized counts are returned so the report planner can split inputs.
        """
        payload = {k: v for k, v in prepared.wire.items() if k not in ('modelId', 'inferenceConfig')}
        tokens = len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + 2048
        return TokenCount(tokens, 'local_utf8_estimate', True)

    def generate(self, prepared):
        """Call Converse once and decode only the explicitly advertised output mode.

        Args: prepared: Validated Converse payload; no returned tool is executed.
        Returns: ModelResponse retaining native AWS evidence. One Converse request
            is attempted after local admission; oversized input raises ValueError
            before dispatch. Actual usage is taken from the response, when present.
            JSON mode parses the exact continuation of its sent prefix. Tool mode
            requires exactly one correctly named tool with object input. Neither
            mode guesses nulls or strips arbitrary prose. Decoder failures retain
            complete raw evidence and usage with a
            structure_error; completed_text rejects them before stage validation.
            Malformed tools/refusals remain incomplete. Network errors propagate.
        """
        if self.count(prepared).tokens + prepared.request['max_tokens'] > self.capabilities.context_tokens:
            raise ValueError('Nova input estimate plus output allowance exceeds its context limit; split the input.')
        with closing(self._client(prepared.request.get('timeout', 90))) as client:
            raw = client.converse(**prepared.wire)
        blocks = raw.get('output', {}).get('message', {}).get('content', [])
        stop = raw.get('stopReason', 'unknown')
        text = ''.join(b['text'] for b in blocks if isinstance(b.get('text'), str))
        conversions = []
        structure_error = None
        if isinstance(prepared.response_codec, JSONTextCodec) and stop == 'end_turn':
            try:
                decoded = prepared.response_codec.decode(text)
                text = json.dumps(decoded.value, ensure_ascii=False)
                conversions = decoded.conversions
            except OutputStructureError as error:
                structure_error = str(error)
        elif 'toolConfig' in prepared.wire and stop == 'tool_use':
            tools = [b['toolUse'] for b in blocks if 'toolUse' in b]
            if len(tools) == 1 and tools[0].get('name') == TOOL_NAME and isinstance(tools[0].get('input'), dict):
                text, stop = json.dumps(tools[0]['input'], ensure_ascii=False), 'end_turn'
                try:
                    decoded = prepared.response_codec.decode(tools[0]['input'])
                    text = json.dumps(decoded.value, ensure_ascii=False)
                    conversions = decoded.conversions
                except OutputStructureError as error:
                    structure_error = str(error)
            else:
                stop = 'invalid_structured_response'
        elif 'toolConfig' in prepared.wire and stop == 'end_turn':
            stop = 'missing_structured_response'
        native_usage = raw.get('usage')
        usage = None
        if isinstance(native_usage, dict):
            usage = {target: native_usage[source] for source, target in (
                ('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                ('cacheReadInputTokens', 'cache_read_input_tokens'),
                ('cacheWriteInputTokens', 'cache_creation_input_tokens')) if source in native_usage}
        request_id = raw.get('ResponseMetadata', {}).get('RequestId')
        return ModelResponse(text, stop, prepared.request['model'], usage, raw,
                             request_id=request_id,
                             response_format=prepared.response_codec.name if prepared.response_codec else 'text_v1',
                             conversions=conversions, structure_error=structure_error)
