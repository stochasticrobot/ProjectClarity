"""Bridge application requests to the configured model adapter and record the outcome.

The caller supplies instructions, messages and an optional response schema. This
module selects the adapter, checks configured call/token budgets when enabled,
sends a request and returns completed response text. It saves request/response
receipts, timing, usage and the caller's later validation result. It does not
understand the task described in a prompt or validate its factual answer.

A separate helper in this file loads a prompt and builds a request dictionary.
Provider-specific request translation and response decoding belong to adapters
and codecs. This bridge does not automatically retry failed generation calls."""
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from projectclarity.model_access.providers.registry import resolve_config, create_provider
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.model_access.model_ledger import now, write_entry, stage_group
from projectclarity.model_access.research_budget import ResearchBudgets
from projectclarity.model_access.model_outcomes import ModelOutputLimitError, ModelOutputStructureError

ROOT = Path(__file__).resolve().parents[1]

def request_for(filename, data, schema, model):
    """Build a bounded model request from a separate editable prompt.

    Args:
        filename: Prompt filename within this project's prompts directory.
        data: Caller-supplied payload serialized as JSON, not interpolated instructions.
        schema: Pydantic response class defining the expected shape.
        model: Provider model identifier.
    Returns:
        Request dictionary; no network call. Missing prompt files raise OSError.
    """
    shape = schema.model_json_schema()
    for item in [shape, *shape.get('$defs', {}).values()]:
        item.pop('title', None)
        item.pop('description', None)
    return dict(model=model, max_tokens=4096,
                system=(ROOT / 'prompts' / filename).read_text(),
                response_schema=shape,
                messages=[{'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])

class ModelCalls:
    """Single-attempt provider transport retaining private request/response receipts."""

    def __init__(self, root, env_file, model=None, provider=None):
        """Resolve provider selection without creating a client or making requests.

        Args:
            root: Private session-artifact directory.
            env_file: Private dotenv file, read for selection now and credentials per call.
            model: Optional model override; otherwise configured/provider default.
            provider: Optional registered provider override; defaults to configuration.
        Returns:
            None. Pins provider/model/output-contract identity for this caller.
            Credentials stay out of browser responses and saved session settings;
            credential refresh is allowed, but changing output policy needs restart.
        """
        self.root, self.env_file = root, env_file
        config = resolve_config(env_file, provider, model)
        self.provider, self.model = config.provider, config.model
        self.contract_id = config.contract_id
        self.count_method = None
        self.latest = {}
        self.budgets = ResearchBudgets(root)

    def __call__(self, stage, request, session_id):
        """Send one provider request and retain enough evidence to audit its outcome.
        
        The ledger is written before dispatch so a process stop does not make a
        started attempt disappear. Transport completion and application validation
        are recorded separately: this method does not validate the returned JSON.
        
        Args:
            stage: Receipt label identifying the workflow step and, where applicable,
                its document/chunk or batch. Used to find this attempt's ledger row.
            request: Neutral request dictionary, including model, prompt,
                source data and output schema; may override the request timeout.
            session_id: Validated session UUID naming a directory under self.root.
        Returns:
            Concatenated text from a complete provider response. Writes private
            request/response receipts and accounting metadata, and records the latest
            attempt for later validation. A missing key, provider failure or incomplete
            response raises RuntimeError without an automatic retry. An advertised
            representation that cannot be decoded raises ModelOutputStructureError;
            raw response, actual usage and rejection provenance remain available. Confirmed
            max_tokens raises its ModelOutputLimitError subtype so extraction can
            subdivide text; other stages retain their existing failure handling. Session-backed
            requests reserve adapter-counted or explicitly estimated input plus effective maximum output against one run-wide
            allowance; they wait at a limit until Continue or Quit. The configured
            key is redacted from saved receipt strings; raw receipts are not public UI
            state. Ledger/filesystem errors can also propagate.
            Output-format version, conversion counts and decoder outcome are
            recorded separately from the research stage's validation result.
        """
        import time
        call_id=uuid4().hex;label=f'{stage}-{call_id}.json';folder=self.root/session_id
        row={'call_id':call_id,'stage':stage,'stage_group':stage_group(stage),'model':request.get('model',self.model),
             'provider':self.provider,'contract_id':self.contract_id,'started_at':now(),'finished_at':None,'duration_seconds':None,'attempted':False,
             'outcome':'running','validation':'unknown','provider_request_id':None,'response_id':None,
             'receipt':label,'usage':None,'legacy':False,'max_output_tokens':request.get('max_tokens'),
             'timeout_seconds':request.get('timeout',90),'stop_reason':None}
        self.latest[(session_id,stage)]=row;write_entry(folder,row)
        key=();receipt={'request':request,'status':'failed','call_id':call_id};started=time.monotonic()
        try:
            config = resolve_config(self.env_file, self.provider, self.model)
            if config.contract_id != self.contract_id:
                raise ValueError('Provider output configuration changed; restart before further model calls.')
            adapter = create_provider(config)
            key = adapter.redaction_values
            prepared = adapter.prepare(request)
            receipt.update(provider=self.provider, contract_id=self.contract_id, wire_request=prepared.wire,
                           effective_request=prepared.request,
                           response_format=prepared.response_codec.name if prepared.response_codec else 'text_v1')
            row.update(max_output_tokens=prepared.request['max_tokens'],
                       requested_max_output_tokens=prepared.requested_max_tokens,
                       structured_output=adapter.capabilities.structured_output,
                       response_format=prepared.response_codec.name if prepared.response_codec else 'text_v1')
            if self.budgets.enabled(session_id):
                self.budgets.wait_until_running(session_id)
                count = adapter.count(prepared)
                if type(count.tokens) is not int or count.tokens < 0:
                    raise ValueError('Adapter input count must be a nonnegative integer.')
                if count.tokens + prepared.request['max_tokens'] > adapter.capabilities.context_tokens:
                    raise ValueError('Input admission count plus output allowance exceeds model context; split the input.')
                row.update(input_count=count.tokens, count_method=count.method,
                           input_count_estimated=count.estimated)
                self.budgets.reserve(session_id, call_id, count.tokens + prepared.request['max_tokens'])
            row['attempted']=True;write_entry(folder,row)
            response = adapter.generate(prepared)
            receipt['response'] = response.receipt()
            row.update(usage=response.usage, response_id=response.response_id,
                       provider_request_id=response.request_id, model=response.model,
                       outcome='response_received', stop_reason=response.stop_reason,
                       structure_status='rejected' if response.structure_error else 'decoded',
                       conversion_count=len(response.conversions))
            text=response.text;receipt['raw_text']=text
            if response.stop_reason != 'end_turn':
                row['outcome'] = 'incomplete'
            if response.structure_error:
                row['validation'] = 'not_run'
                receipt['status'] = 'invalid_structure'
            text = response.completed_text()
            receipt['status']='response_received';return text
        except Exception as error:
            if row['outcome'] != 'incomplete' and not isinstance(error, ModelOutputStructureError):
                row['outcome']='failed' if row['attempted'] else 'not_sent'
            row['provider_request_id']=row['provider_request_id'] or getattr(error,'request_id',None)
            receipt['error']=str(error)
            if isinstance(error, (ModelOutputLimitError, ModelOutputStructureError)):
                raise
            raise RuntimeError('The model request failed or was incomplete. No automatic retry was made.') from error
        finally:
            row.update(finished_at=now(),duration_seconds=round(time.monotonic()-started,3))
            try:
                write_entry(folder,row)
            finally:
                self.budgets.finish(session_id, call_id, row['attempted'], row['usage'])
            save(folder/label,receipt,key)

    def count_input_tokens(self, request):
        """Obtain a labelled admission count through the selected adapter.

        Args:
            self: Configured model caller; credentials remain private.
            request: Exact generation request including model, messages, system
                prompt and response schema. Output allowance is not input text.
        Returns:
            Input count, with provenance exposed in count_method. Anthropic uses its
            count API; Nova uses the explicitly approved local UTF-8 estimate.
            No generation call or fallback occurs. Failures, including a changed
            output contract since caller creation, prevent generation.
        """
        try:
            config = resolve_config(self.env_file, self.provider, self.model)
            if config.contract_id != self.contract_id:
                raise ValueError('Provider output configuration changed; restart before further model calls.')
            adapter = create_provider(config)
            count = adapter.count(adapter.prepare(request))
            if type(count.tokens) is not int or count.tokens < 0:
                raise ValueError('Adapter input count must be a nonnegative integer.')
            self.count_method = count.method
            return count.tokens
        except Exception as error:
            raise RuntimeError('Input token admission count failed; generation was not sent.') from error

    def call_metadata(self, stage, session_id):
        """Return the exact attempt's output usage for extraction sizing decisions.

        Args: stage: Unique chunk call label; session_id: Owning session UUID.
        Returns: Detached accounting metadata or an empty dictionary when absent.
            Output includes reasoning. No prompts or credentials are exposed, and
            this read never sends a request or changes validation/accounting.
        """
        row = self.latest.get((session_id, stage), {})
        return deepcopy({key: row.get(key) for key in
                         ('call_id', 'receipt', 'attempted', 'stop_reason', 'usage', 'max_output_tokens',
                          'provider', 'count_method', 'input_count_estimated',
                          'response_format', 'structure_status', 'conversion_count')})

    def mark_validation(self, stage, session_id, accepted):
        """Attach structural acceptance to the exact latest transport attempt.

        Args: stage: Exact call label; session_id: Owning run; accepted: Check result.
        Returns: None; updates the ledger only if that attempt exists. Usage and
            transport outcomes are preserved even when output is rejected.
        """
        row=self.latest.get((session_id,stage))
        if row is not None:
            row['validation']=('accepted' if accepted else 'rejected') if row['outcome']=='response_received' else 'not_run'
            write_entry(self.root/session_id,row)
