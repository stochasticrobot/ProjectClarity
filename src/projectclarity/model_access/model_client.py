"""Give every research step a common way to ask the AI model for help.

The application asks the LLM model to do several different jobs: understand a research
question, extract facts from documents, review evidence and write a report. This
file prepares and sends those requests to the model provider, then returns the model's reply
to the step that asked for it. The instructions for each job live in prompts/.

It also records what was sent, what came back, how long the request took and any
reported text usage. Those records help explain failures and track usage that may
incur charges. The requesting step decides whether the reply is usable; this file
does not decide the research answer or silently repeat a failed request.
"""
import json
from copy import deepcopy
import os
from pathlib import Path
from uuid import uuid4

from anthropic import Anthropic
from dotenv import dotenv_values
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.model_access.model_ledger import now, write_entry, stage_group
from projectclarity.model_access.research_budget import ResearchBudgets
from projectclarity.model_access.model_outcomes import ModelOutputLimitError

ROOT = Path(__file__).resolve().parents[1]

def request_for(filename, data, schema, model):
    """Build a bounded model request from a separate editable prompt.

    Args:
        filename: Prompt filename within this project's prompts directory.
        data: Research context serialized as data, not interpolated instructions.
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
                output_config={'format': {'type': 'json_schema', 'schema': shape}},
                messages=[{'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])

class ModelCalls:
    """Single-attempt provider transport retaining private request/response receipts."""

    def __init__(self, root, env_file, model='claude-haiku-5-5'):
        """Configure provider access without loading or copying credentials yet.

        Args:
            root: Private session-artifact directory.
            env_file: Existing credential file, used only when a call is requested.
            model: Default model used throughout the web research workflow.
        Returns:
            None. Credentials stay out of browser responses and configuration files.
        """
        self.root, self.env_file, self.model = root, env_file, model
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
            request: Exact provider request dictionary, including model, prompt,
                source data and output schema; may override the request timeout.
            session_id: Validated session UUID naming a directory under self.root.
        Returns:
            Concatenated text from a complete provider response. Writes private
            request/response receipts and accounting metadata, and records the latest
            attempt for later validation. A missing key, provider failure or incomplete
            response raises RuntimeError without an automatic retry. Confirmed
            max_tokens raises its ModelOutputLimitError subtype so extraction can
            subdivide text; other stages retain their existing failure handling. Session-backed
            requests reserve counted input plus maximum output against one run-wide
            allowance; they wait at a limit until Continue or Quit. The configured
            key is redacted from saved receipt strings; raw receipts are not public UI
            state. Ledger/filesystem errors can also propagate.
        """
        import time
        call_id=uuid4().hex;label=f'{stage}-{call_id}.json';folder=self.root/session_id
        row={'call_id':call_id,'stage':stage,'stage_group':stage_group(stage),'model':request.get('model',self.model),
             'started_at':now(),'finished_at':None,'duration_seconds':None,'attempted':False,
             'outcome':'running','validation':'unknown','provider_request_id':None,'response_id':None,
             'receipt':label,'usage':None,'legacy':False,'max_output_tokens':request.get('max_tokens'),
             'timeout_seconds':request.get('timeout',90),'stop_reason':None}
        self.latest[(session_id,stage)]=row;write_entry(folder,row)
        key='';receipt={'request':request,'status':'failed','call_id':call_id};started=time.monotonic()
        try:
            key=(os.environ.get('ANTHROPIC_API_KEY') or dotenv_values(self.env_file).get('ANTHROPIC_API_KEY') or '').strip()
            if not key:raise RuntimeError('No API key configured.')
            if self.budgets.enabled(session_id):
                self.budgets.wait_until_running(session_id)
                tokens = self.count_input_tokens(request) + request['max_tokens']
                self.budgets.reserve(session_id, call_id, tokens)
            with Anthropic(api_key=key, base_url='https://api.anthropic.com', max_retries=0, timeout=90) as client:
                row['attempted']=True;write_entry(folder,row)
                response=client.messages.create(**request)
            receipt['response']=response.model_dump(mode='json')
            row.update(usage=receipt['response'].get('usage'),response_id=response.id,
                       provider_request_id=getattr(response,'_request_id',None),model=response.model,
                       outcome='response_received',stop_reason=response.stop_reason)
            text=''.join(b.text for b in response.content if b.type=='text');receipt['raw_text']=text
            if response.stop_reason!='end_turn':
                row['outcome']='incomplete'
                if response.stop_reason == 'max_tokens':
                    raise ModelOutputLimitError('Model output reached its token limit.')
                raise RuntimeError('Model response incomplete.')
            receipt['status']='response_received';return text
        except Exception as error:
            if row['outcome']!='incomplete':row['outcome']='failed' if row['attempted'] else 'not_sent'
            row['provider_request_id']=row['provider_request_id'] or getattr(error,'request_id',None)
            receipt['error']=str(error)
            if isinstance(error, ModelOutputLimitError):
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
        """Ask the provider to count a complete request before generation.

        Args:
            self: Configured model caller; credentials remain private.
            request: Exact generation request including model, messages, system
                prompt and response schema. Output allowance is not input text.
        Returns:
            Provider-reported input token count. This makes a token-count API call,
            not a generation call. Missing credentials or count failures stop the
            request before generation; no approximate fallback or retry is used.
        """
        key = (os.environ.get('ANTHROPIC_API_KEY') or dotenv_values(self.env_file).get('ANTHROPIC_API_KEY') or '').strip()
        if not key:
            raise RuntimeError('No API key configured for token counting.')
        payload = {name: request[name] for name in
                   ('model', 'messages', 'system', 'output_config', 'thinking', 'tools', 'tool_choice')
                   if name in request}
        try:
            with Anthropic(api_key=key, base_url='https://api.anthropic.com', max_retries=0, timeout=90) as client:
                return client.messages.count_tokens(**payload).input_tokens
        except Exception as error:
            raise RuntimeError('Input token count failed; generation was not sent.') from error

    def call_metadata(self, stage, session_id):
        """Return the exact attempt's output usage for extraction sizing decisions.

        Args: stage: Unique chunk call label; session_id: Owning session UUID.
        Returns: Detached accounting metadata or an empty dictionary when absent.
            Output includes reasoning. No prompts or credentials are exposed, and
            this read never sends a request or changes validation/accounting.
        """
        row = self.latest.get((session_id, stage), {})
        return deepcopy({key: row.get(key) for key in
                         ('call_id', 'receipt', 'attempted', 'stop_reason', 'usage', 'max_output_tokens')})

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
