"""Let every research step speak to different AI services in the same way.

Research steps supply instructions, source text and the required answer fields.
The small records and interface here describe those requests, a service's limits,
its reply and reported usage. Provider adapters translate between these records
and each service's API. They do not decide whether a research claim is supported:
the application's existing validators still check the returned information.
This file itself makes no network calls and chooses no alternative model.
"""
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Protocol
from projectclarity.model_access.model_outcomes import ModelOutputLimitError, ModelOutputStructureError
from projectclarity.model_access.providers.structured_output import ResponseCodec


@dataclass(frozen=True)
class Capabilities:
    """Describe limits used to size requests, not guarantees of answer quality.

    Args:
        max_output_tokens: Largest output allowance accepted by this adapter.
        context_tokens: Input-plus-output token ceiling used for admission.
        count_method: Provenance label for pre-request token counting/estimation.
        structured_output: Description of the provider's response-format mechanism.
    Returns:
        Immutable capability record. Construction does not query the provider or
        verify the declarations; adapter tests and documentation must establish them.
    """
    max_output_tokens: int
    context_tokens: int
    count_method: str
    structured_output: str


@dataclass(frozen=True)
class TokenCount:
    """Explain the input allowance before a request, separately from billed usage.

    Args:
        tokens: Input token count or local estimate, including schema overhead.
        method: Stable label identifying how the value was obtained.
        estimated: True for a local estimate; False for provider-reported counts.
    Returns:
        Immutable admission record, not a charge or a claim of exact usage.
        The common caller rejects negative or non-integer counts before dispatch.
    """
    tokens: int
    method: str
    estimated: bool = False


@dataclass
class PreparedRequest:
    """Keep the application's request beside the exact outgoing service payload.

    Args:
        request: Detached neutral request after applying declared output limits.
        wire: Provider-specific payload, excluding authentication credentials.
        requested_max_tokens: Original allowance before a model-specific cap.
        response_codec: Optional explicit wire-to-canonical output representation.
    Returns:
        Request record used by both counting and generation, allowing receipts to
        disclose effective limits and translations. Construction performs no I/O.
    """
    request: dict
    wire: dict
    requested_max_tokens: int
    response_codec: ResponseCodec | None = None


@dataclass
class ModelResponse:
    """Keep a common reply and the original service response for later inspection.

    Args:
        text: Text or serialized structured result supplied to the stage validator.
        stop_reason: Common completion reason; incomplete replies remain explicit.
        model: Model ID reported by the provider, or the requested ID if absent.
        usage: Common token counters; None means usage was not reported.
        raw: Untouched provider response dictionary, retained as private evidence.
        response_id: Provider response identifier, or None when unavailable.
        request_id: Provider request identifier, or None when unavailable.
        response_format: Versioned output representation used for this request.
        conversions: Value-free conversion paths recorded by the response codec.
        structure_error: Safe decoder failure description, or None when decoded.
    Returns:
        Reply record; construction does not validate research facts or write files.
        The common caller checks completion before stage-specific validation.
    """
    text: str
    stop_reason: str
    model: str
    usage: dict | None
    raw: dict
    response_id: str | None = None
    request_id: str | None = None
    response_format: str = 'native_json_v1'
    conversions: list[dict] = field(default_factory=list)
    structure_error: str | None = None

    def completed_text(self):
        """Release text only after completion and representation decoding succeed.

        Args: None; uses this response's stop reason and structure_error.
        Returns: Text ready for stage-specific validation. Raises
            ModelOutputLimitError on confirmed truncation, RuntimeError on other
            incomplete replies, or ModelOutputStructureError on invalid encoding.
            No inference, retry, local schema repair or side effects occur.
        """
        if self.stop_reason == 'max_tokens':
            raise ModelOutputLimitError('Model output reached its token limit.')
        if self.stop_reason != 'end_turn':
            raise RuntimeError('Model response incomplete.')
        if self.structure_error:
            raise ModelOutputStructureError(self.structure_error)
        return self.text

    def receipt(self):
        """Build a portable receipt retaining native response evidence.

        Args: None.
        Returns: JSON-serializable metadata and raw response; no credentials added.
        """
        return dict(model=self.model, usage=self.usage, stop_reason=self.stop_reason,
                    id=self.response_id, request_id=self.request_id, raw=self.raw,
                    response_format=self.response_format, conversions=self.conversions,
                    structure_error=self.structure_error)


@dataclass(frozen=True)
class ProviderConfig:
    """Carry selected service settings privately to its adapter.

    Args:
        provider: Registered backend name, such as anthropic or bedrock.
        model: Backend-specific model ID resolved from options and settings.
        values: Merged dotenv/process values, potentially including credentials.
        contract_id: Non-secret fingerprint of provider/model/output policy, used
            to prevent saved-output reuse across incompatible contracts.
    Returns:
        Configuration record whose repr omits values. The mapping itself remains
        sensitive; callers must not serialize it into receipts or browser state.
    """
    provider: str
    model: str
    values: dict = field(repr=False)
    contract_id: str = ''


class ModelProvider(Protocol):
    """Extension contract: implement these operations and register a factory.

    Factories receive ProviderConfig. Adapters must disable retries, never mutate
    process environment, and retain unknown usage as None. Credentials are only
    supplied to SDK authentication and returned privately for receipt redaction.
    Local adapters may return an empty redaction tuple. Each invocation owns its
    own SDK client, so the adapter is safe for concurrent research workers.
    """
    capabilities: Capabilities
    redaction_values: tuple[str, ...]

    def prepare(self, request: dict) -> PreparedRequest:
        """Validate and translate without I/O.

        Args: request: Neutral model/system/messages/response_schema/max_tokens/timeout.
        Returns: Effective limits and exact wire data; unsupported inputs raise.
        """
        ...

    def count(self, prepared: PreparedRequest) -> TokenCount:
        """Count or estimate the complete wire input, including its schema.

        Args: prepared: The exact prepared generation request.
        Returns: Labelled count; failures raise before generation, never fall back.
        """
        ...

    def generate(self, prepared: PreparedRequest) -> ModelResponse:
        """Make one generation attempt without retries or executing returned tools.

        Args: prepared: Validated payload produced by this adapter.
        Returns: Normalized reply with native evidence; transport errors propagate.
        """
        ...


def effective_request(request, capabilities):
    """Validate the common text contract and apply declared output limits.

    Args:
        request: Neutral dictionary; unknown fields are rejected, never discarded.
        capabilities: Selected model's explicit output and context limits.
    Returns:
        Detached request capped at the model output maximum. Bad types, unsupported
        content or unknown options raise ValueError before dispatch. No I/O occurs.
    """
    allowed = {'model', 'system', 'messages', 'response_schema', 'max_tokens', 'timeout'}
    if set(request) - allowed:
        raise ValueError('Unsupported model request fields: ' + ', '.join(sorted(set(request) - allowed)))
    result = deepcopy(request)
    if not isinstance(result.get('model'), str) or not result['model'].strip():
        raise ValueError('A model ID is required.')
    limit = result.get('max_tokens')
    if type(limit) is not int or limit < 1:
        raise ValueError('max_tokens must be a positive integer.')
    if not isinstance(result.get('system', ''), str):
        raise ValueError('System instructions must be text.')
    if not isinstance(result.get('messages'), list):
        raise ValueError('messages must be a list.')
    for message in result['messages']:
        if set(message) != {'role', 'content'} or message['role'] not in ('user', 'assistant') or not isinstance(message['content'], str):
            raise ValueError('Only user/assistant text messages are supported.')
    if 'response_schema' in result and not isinstance(result['response_schema'], dict):
        raise ValueError('response_schema must be a JSON schema object.')
    timeout = result.get('timeout', 90)
    if type(timeout) not in (float, int) or timeout <= 0:
        raise ValueError('timeout must be positive seconds.')
    result['max_tokens'] = min(limit, capabilities.max_output_tokens)
    return result
