"""Translate text/JSON requests to the direct Anthropic Messages API.

This adapter supplies authentication, maps the requested response schema to the
API's JSON-format option, counts input tokens and sends one generation request.
It returns response text, completion state, usage and the original SDK response.
It does not understand the task, validate answer content or retry failed calls.

The fixed capability values below are the application's current adapter settings,
not a model-by-model capability lookup or a guarantee for every Anthropic model."""
from anthropic import Anthropic
from projectclarity.model_access.providers.structured_output import NativeJSONCodec
from projectclarity.model_access.providers.contracts import (
    Capabilities, ModelResponse, PreparedRequest, TokenCount, effective_request,
)


class AnthropicProvider:
    """Preserve the existing direct Anthropic endpoint and single-attempt policy."""
    # Positional fields: max output tokens, input-plus-output admission ceiling.
    # 32768 is an application output cap; 200000 is the configured context ceiling.
    # These are fixed settings, not dynamically verified limits of the chosen model.
    capabilities = Capabilities(32768, 200000, 'provider_count', 'native_json_schema')

    def __init__(self, config):
        """Read private credentials without opening a connection.

        Args: config: Selected model and merged dotenv/process settings.
        Returns: None. Missing key raises ValueError; no fallback credential source.
        """
        self.key = (config.values.get('ANTHROPIC_API_KEY') or '').strip()
        if not self.key:
            raise ValueError('No ANTHROPIC_API_KEY configured.')
        self.redaction_values = (self.key,)

    def prepare(self, request):
        """Translate neutral JSON output into Anthropic's native schema option.

        Args: request: Common text request, never an SDK-specific payload.
        Returns: Detached effective request and wire fields; invalid input raises.
        """
        effective = effective_request(request, self.capabilities)
        wire = {k: v for k, v in effective.items() if k != 'response_schema'}
        if 'response_schema' in effective:
            wire['output_config'] = {'format': {'type': 'json_schema', 'schema': effective['response_schema']}}
        codec = NativeJSONCodec(effective['response_schema']) if 'response_schema' in effective else None
        return PreparedRequest(effective, wire, request['max_tokens'], codec)

    def count(self, prepared):
        """Count complete Anthropic input, retaining schema overhead.

        Args: prepared: Exact generation payload prepared by this adapter.
        Returns: Provider-labelled count. One count call; errors propagate without retry.
        """
        payload = {k: v for k, v in prepared.wire.items() if k not in ('max_tokens', 'timeout')}
        with Anthropic(api_key=self.key, base_url='https://api.anthropic.com', max_retries=0,
                       timeout=prepared.request.get('timeout', 90)) as client:
            return TokenCount(client.messages.count_tokens(**payload).input_tokens, 'provider_count')

    def generate(self, prepared):
        """Send one Messages call and normalize text, stop reason and usage.

        Args: prepared: Validated request and exact wire payload.
        Returns: ModelResponse including the untouched SDK response dump. Provider
            failures propagate; incomplete replies remain explicitly incomplete.
        """
        with Anthropic(api_key=self.key, base_url='https://api.anthropic.com', max_retries=0,
                       timeout=prepared.request.get('timeout', 90)) as client:
            response = client.messages.create(**prepared.wire)
        raw = response.model_dump(mode='json')
        request_id = getattr(response, '_request_id', None)
        return ModelResponse(''.join(b.text for b in response.content if b.type == 'text'),
                             response.stop_reason, raw.get('model', prepared.request['model']), raw.get('usage'), raw,
                             raw.get('id'), request_id if isinstance(request_id, str) else None)
