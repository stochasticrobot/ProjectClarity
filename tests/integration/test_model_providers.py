"""Exercise real workflow/transport routing; only external SDK calls are replaced."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.model_access.model_client import ModelCalls
from projectclarity.model_access.model_ledger import read_ledger
from projectclarity.model_access.model_outcomes import ModelOutputLimitError, ModelOutputStructureError
from projectclarity.model_access.providers.bedrock_provider import BedrockProvider, tool_schema
from projectclarity.model_access.providers.contracts import Capabilities, ModelResponse, PreparedRequest, TokenCount
from projectclarity.model_access.providers.registry import create_provider, register_provider, resolve_config
from projectclarity.knowledge_extraction.knowledge_extraction import extraction_request, validate_response
from projectclarity.ux.web_app import Sessions, create_app

ENV = {'PROJECTCLARITY_PROVIDER': 'bedrock', 'PROJECTCLARITY_MODEL': 'us.amazon.nova-micro-v1:0',
       'PROJECTCLARITY_BEDROCK_OUTPUT': 'tool_nullable', 'AWS_REGION': 'us-east-2', 'AWS_BEARER_TOKEN_BEDROCK': 'offline-secret'}
PLAN = {'information_needed': [{'kind': 'entity_type', 'name': 'Planet', 'description': 'Identify planets.'}]}


def fixture_wire_value(value, schema):
    """Have the offline model fixture obey the actual advertised output format.

    Args: value: Canonical fixture content; schema: Actual SDK-bound wire schema.
    Returns: Schema-shaped fixture value, including explicit nullable lists. This
        models the service boundary only; production decoding remains real.
    """
    if schema.get('description', '').startswith('Value container:'):
        return [] if value is None else [fixture_wire_value(value, schema['items'])]
    if isinstance(value, dict):
        return {key: fixture_wire_value(item, schema.get('properties', {}).get(key, {}))
                for key, item in value.items()}
    if isinstance(value, list):
        return [fixture_wire_value(item, schema.get('items', {})) for item in value]
    return value


def nova_reply(payload, stop='tool_use', usage=True):
    """Build a native AWS response, not a normalized transport fixture.

    Args: payload: Tool input; stop: Native stop reason; usage: Include counters.
    Returns: AWS-shaped dictionary with request ID and optional usage evidence.
    """
    result = {'output': {'message': {'role': 'assistant', 'content': [
        {'toolUse': {'name': 'submit_result', 'toolUseId': 'tool-1', 'input': payload}}]}},
        'stopReason': stop, 'ResponseMetadata': {'RequestId': 'aws-request'}}
    if usage:
        result['usage'] = {'inputTokens': 30, 'outputTokens': 10}
    return result


class LocalFixtureProvider:
    """Third, credential-free adapter proves registry extension, not Ollama support."""
    capabilities = Capabilities(4096, 32000, 'fixture_count', 'fixture_json')
    redaction_values = ()

    def __init__(self, config):
        """Accept the same configuration contract as hosted providers.

        Args: config: Registered provider settings.
        Returns: None; no network, credentials or global environment changes.
        """
        self.model = config.model

    def prepare(self, request):
        """Preserve the neutral request for this controlled fixture.

        Args: request: Actual workflow request.
        Returns: Prepared request with unchanged schema and allowance; no I/O.
        """
        return PreparedRequest(request, request, request['max_tokens'])

    def count(self, prepared):
        """Return a known test count for budget admission.

        Args: prepared: Exact generation request.
        Returns: Fixture-labelled token count; not an inference or tokenizer claim.
        """
        return TokenCount(10, 'fixture_count')

    def generate(self, prepared):
        """Answer real setup stages using their requested schema.

        Args: prepared: Neutral request from the actual setup graph.
        Returns: Valid deterministic plan or clarification; no SDK dependency.
        """
        properties = prepared.request['response_schema']['properties']
        payload = {'questions': []} if 'questions' in properties else PLAN
        return ModelResponse(json.dumps(payload), 'end_turn', self.model,
                             {'input_tokens': 10, 'output_tokens': 5}, {'fixture': True})


class ProviderIntegrationTests(unittest.TestCase):
    """Protect production routing, accounting, no fallback and future extension."""

    def run_setup(self, root, env_file, provider=None):
        """Drive the real HTTP setup endpoint and await its background graph.

        Args: root: Isolated data directory; env_file: Private configuration;
            provider: Optional registered backend override.
        Returns: Final public state and saved ledger; closes the session workers.
            Fails after five seconds instead of leaving a test hanging.
        """
        sessions = Sessions(root, env_file=env_file, provider=provider)
        sid = str(uuid4())
        try:
            with TestClient(create_app(sessions)) as http:
                self.assertEqual(http.post('/api/setup', json={'session_id': sid, 'question': 'List planets.'}).status_code, 202)
                for _ in range(500):
                    state = sessions.read(sid)
                    if state['status'] != 'working':
                        break
                    time.sleep(.01)
                self.assertEqual(state['status'], 'ready', state.get('error'))
                return state, read_ledger(root / sid)
        finally:
            sessions.close()

    def test_bedrock_real_http_graph_and_budget_settlement(self):
        """Exercise real web configuration, graph, adapter, validation and ledger.

        Args: None.
        Returns: None; two native replies settle estimated reservations with actual
            AWS counters. Anthropic is never called and secrets never reach artifacts.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV), patch.object(BedrockProvider, '_client') as factory, patch('projectclarity.model_access.providers.anthropic_provider.Anthropic') as anthropic:
            root = Path(tmp)
            sdk = factory.return_value
            sdk.converse.side_effect = [nova_reply(PLAN), nova_reply({'questions': []})]
            state, ledger = self.run_setup(root, root / 'unused')
            self.assertEqual(sdk.converse.call_count, 2)
            anthropic.assert_not_called()
            self.assertEqual(ledger['totals']['known_tokens'], 80)
            self.assertEqual(state['model_budget']['known_tokens'], 80)
            budget_path = next(root.glob('*/model-budget.json'))
            self.assertFalse(json.loads(budget_path.read_text())['reservations'])
            for row in ledger['calls']:
                self.assertEqual(row['provider'], 'bedrock')
                self.assertTrue(row['input_count_estimated'])
                self.assertEqual(row['count_method'], 'local_utf8_estimate')
                self.assertEqual(row['validation'], 'accepted')
            for call in sdk.converse.call_args_list:
                self.assertEqual(call.kwargs['modelId'], ENV['PROJECTCLARITY_MODEL'])
                self.assertEqual(call.kwargs['toolConfig']['toolChoice']['tool']['name'], 'submit_result')
            for path in root.rglob('*.json'):
                self.assertNotIn('offline-secret', path.read_text())

    def test_third_provider_requires_registration_not_workflow_changes(self):
        """Run the same production entry point with a credential-free third adapter.

        Args: None.
        Returns: None; fixture provider registration alone enables the real workflow.
            Does not claim an actual Ollama implementation or live local inference.
        """
        from projectclarity.model_access.providers import registry
        with TemporaryDirectory() as tmp, patch.dict(registry._PROVIDERS), patch.dict('os.environ', {}, clear=True):
            register_provider('local_fixture', LocalFixtureProvider, 'fixture-model')
            state, ledger = self.run_setup(Path(tmp), Path(tmp) / 'unused', 'local_fixture')
            self.assertEqual(state['status'], 'ready')
            self.assertEqual({row['provider'] for row in ledger['calls']}, {'local_fixture'})

    def test_bedrock_autonomous_http_workflow_through_report(self):
        """Run real setup, extraction, review and reporting over the Nova adapter.

        Args: None.
        Returns: None; external search/download and AWS replies are fixtures, but
            production orchestration, schemas, budgets and report assembly execute.
            This proves routing, not Nova's live research quality.
        """
        from tests.integration.test_document_pipeline import FixtureRun
        from tests.integration.test_research_graph import FullWorkflowCaller
        for mode in ('tool_nullable', 'json_text'):
            with TemporaryDirectory() as tmp, patch.dict('os.environ', {**ENV, 'PROJECTCLARITY_BEDROCK_OUTPUT': mode}), patch.object(BedrockProvider, '_client') as factory:
                root = Path(tmp); sid = str(uuid4()); fixtures = FullWorkflowCaller(questions=[])

                def send(**wire):
                    """Translate fixture input only at the native AWS network boundary.

                    Args: wire: Actual Converse payload from the production adapter.
                    Returns: AWS-shaped fixture reply chosen by the requested schema.
                    """
                    schema = (wire['toolConfig']['tools'][0]['toolSpec']['inputSchema']['json'] if mode == 'tool_nullable'
                              else json.loads(wire['system'][-1]['text'].split('Response schema:\n', 1)[1]))
                    properties = schema['properties']
                    stage = next(stage for key, stage in [
                        ('information_needed', 'define'), ('questions', 'clarify'),
                        ('searches', 'search_queries'), ('entities', 'extract-fixture'),
                        ('selected_facts', 'evidence-fixture'), ('decisions', 'triage-fixture'),
                        ('items', 'report-fixture')] if key in properties)
                    if 'entities' in properties and 'decisions' in properties:
                        stage = 'extract-omissions-fixture'
                    neutral = {'model': wire['modelId'], 'response_schema': schema,
                               'system': wire.get('system', [{}])[0].get('text', ''),
                               'messages': [{'role': m['role'], 'content': m['content'][0]['text']} for m in wire['messages']]}
                    payload = json.loads(fixtures(stage, neutral, sid))
                    if mode == 'tool_nullable':
                        return nova_reply(fixture_wire_value(payload, schema))
                    result = nova_reply({}, 'end_turn')
                    result['output']['message']['content'] = [{'text': json.dumps(payload)[1:]}]
                    return result

                sdk = factory.return_value
                sdk.converse.side_effect = send
                sessions = Sessions(root, env_file=root / 'unused', runner_factory=FixtureRun)
                try:
                    with TestClient(create_app(sessions)) as http:
                        result = http.post('/api/setup', json={'session_id': sid, 'question': 'Canadian AI companies',
                            'auto_advance': True, 'research_rounds': 1, 'model_request_budget': 100, 'model_token_budget': 1000000})
                        self.assertEqual(result.status_code, 202)
                        for _ in range(1000):
                            state = sessions.read(sid)
                            if state['status'] == 'report_completed':
                                break
                            time.sleep(.01)
                        self.assertEqual(state['status'], 'report_completed', state.get('error'))
                        ledger = read_ledger(root / sid)
                        groups = {row['stage_group'] for row in ledger['calls']}
                        self.assertTrue({'Definition', 'Extraction', 'Selection', 'Triage', 'Report'} <= groups, groups)
                        self.assertEqual(ledger['totals']['attempted'], sdk.converse.call_count)
                        self.assertEqual(state['model_budget']['known_tokens'], 40 * sdk.converse.call_count)
                        self.assertTrue(all(row['provider'] == 'bedrock' for row in ledger['calls']))
                        self.assertTrue(all(call.kwargs['inferenceConfig']['maxTokens'] <= 10000 for call in sdk.converse.call_args_list))
                finally:
                    sessions.close()

    def test_cap_estimate_truncation_and_unknown_usage(self):
        """Verify effective output caps, failure stops and missing usage semantics.

        Args: None.
        Returns: None; one attempt per call, no fallback, unknown usage not zero.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV), patch.object(BedrockProvider, '_client') as factory:
            root = Path(tmp); (root / 's').mkdir()
            caller = ModelCalls(root, root / 'unused')
            request = extraction_request('A source.', [], caller.model, 32768)
            factory.return_value.converse.return_value = nova_reply({}, 'max_tokens', usage=False)
            with self.assertRaises(ModelOutputLimitError):
                caller('extract-one', request, 's')
            self.assertEqual(factory.return_value.converse.call_count, 1)
            self.assertEqual(factory.return_value.converse.call_args.kwargs['inferenceConfig']['maxTokens'], 10000)
            ledger = read_ledger(root / 's')
            self.assertEqual(ledger['totals']['unknown_usage'], 1)
            self.assertEqual(ledger['calls'][0]['requested_max_output_tokens'], 32768)
            self.assertEqual(request['max_tokens'], 32768)

    def test_schema_validation_and_request_preservation(self):
        """Retain source text/schema constraints while rejecting invalid model output.

        Args: None.
        Returns: None; refs expand without source mutation and local strict validation
            still rejects unknown fields omitted from Nova's restricted root schema.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV):
            adapter = create_provider(resolve_config(Path(tmp) / 'unused'))
            request = extraction_request('Exact source: café.', [], ENV['PROJECTCLARITY_MODEL'], 8192)
            before = json.dumps(request)
            prepared = adapter.prepare(request)
            self.assertEqual(prepared.wire['messages'][0]['content'][0]['text'], request['messages'][0]['content'])
            self.assertNotIn('$ref', json.dumps(prepared.wire['toolConfig']))
            self.assertEqual(json.dumps(request), before)
            self.assertTrue(adapter.count(prepared).estimated)
            with self.assertRaises(ValueError):
                validate_response('{"entities": [], "facts": [], "invented": true}', 'Exact source: café.')
            with self.assertRaises(ValueError):
                adapter.prepare({**request, 'thinking': {'type': 'enabled'}})

    def test_private_client_auth_and_no_environment_mutation(self):
        """Check actual SDK client construction without sending a network request.

        Args: None.
        Returns: None; explicit bearer signing, one attempt, and private credentials
            survive absent process AWS variables. Client is always closed.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            path = Path(tmp) / '.env'
            path.write_text('\n'.join(f'{k}={v}' for k, v in ENV.items()))
            adapter = create_provider(resolve_config(path))
            client = adapter._client(90)
            try:
                self.assertEqual(client.meta.config.signature_version, 'bearer')
                self.assertEqual(client.meta.config.retries['total_max_attempts'], 1)
                self.assertEqual(client._request_signer._auth_token.token, 'offline-secret')
                import os
                self.assertNotIn('AWS_BEARER_TOKEN_BEDROCK', os.environ)
            finally:
                client.close()

    def test_unknown_provider_fails_closed(self):
        """Reject typos instead of silently using Anthropic or another backend.

        Args: None.
        Returns: None; bad provider selection fails before client creation.
        """
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                resolve_config(Path(tmp) / 'unused', 'not-registered')

    def test_decode_failure_retains_usage_and_raw_reply_without_retry(self):
        """Distinguish service completion from a rejected output representation.

        Args: None.
        Returns: None; undecodable output retains AWS counters and raw evidence,
            uses one attempt, and cannot become successful stage text.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV), patch.object(BedrockProvider, '_client') as factory:
            root = Path(tmp); folder = root / 's'; folder.mkdir()
            folder.joinpath('session.json').write_text(json.dumps({'model_request_budget': 2, 'model_token_budget': 100000}))
            caller = ModelCalls(root, root / 'unused')
            request = extraction_request('A source.', [], caller.model, 8192)
            payload = {'entities': [{'id': 'a', 'name': 'A', 'type': 'organization', 'subtype': 'null'}], 'facts': []}
            factory.return_value.converse.return_value = nova_reply(payload)
            with self.assertRaises(ModelOutputStructureError):
                caller('extract-bad-encoding', request, 's')
            row = read_ledger(folder)['calls'][0]
            self.assertEqual(row['outcome'], 'response_received')
            self.assertEqual(row['structure_status'], 'rejected')
            self.assertEqual(row['usage'], {'input_tokens': 30, 'output_tokens': 10})
            self.assertEqual(caller.budgets.snapshot('s')['known_tokens'], 40)
            receipt = json.loads((folder / row['receipt']).read_text())
            self.assertEqual(receipt['response']['raw']['output']['message']['content'][0]['toolUse']['input'], payload)
            self.assertEqual(receipt['response']['response_format'], 'nullable_list_v1')
            self.assertEqual(factory.return_value.converse.call_count, 1)

    def test_cli_rejects_undecodable_output_through_same_response_gate(self):
        """Prevent standalone validation from accidentally bypassing codec errors.

        Args: None.
        Returns: None; even canonically valid bare null is rejected when the model
            was explicitly asked to return a list. The CLI makes no hidden retry.
        """
        from testing.extraction_test_rig import call_step
        from tests.unit.test_structured_output import VALID, SOURCE
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV), patch.object(BedrockProvider, '_client') as factory:
            adapter = create_provider(resolve_config(Path(tmp) / 'unused'))
            request = extraction_request(SOURCE, [], ENV['PROJECTCLARITY_MODEL'], 8192)
            factory.return_value.converse.return_value = nova_reply(VALID)
            receipt = call_step(adapter, request, SOURCE)
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['error']['type'], 'ModelOutputStructureError')
            self.assertEqual(factory.return_value.converse.call_count, 1)

    def test_contract_identity_tracks_output_mode_not_credentials(self):
        """Pin the output contract while allowing short-term credentials to refresh.

        Args: None.
        Returns: None; credential changes keep identity, output-mode changes alter
            it and an existing caller fails before dispatch rather than mixing modes.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', ENV), patch.object(BedrockProvider, '_client') as factory:
            root = Path(tmp); (root / 's').mkdir()
            original = resolve_config(root / 'unused')
            with patch.dict('os.environ', {'AWS_BEARER_TOKEN_BEDROCK': 'another-secret'}):
                self.assertEqual(resolve_config(root / 'unused').contract_id, original.contract_id)
            caller = ModelCalls(root, root / 'unused')
            with patch.dict('os.environ', {'PROJECTCLARITY_BEDROCK_OUTPUT': 'json_text'}):
                self.assertNotEqual(resolve_config(root / 'unused').contract_id, original.contract_id)
                with self.assertRaises(RuntimeError):
                    caller('extract-contract-change', extraction_request('Source.', [], caller.model, 8192), 's')
            factory.assert_not_called()
            self.assertEqual(read_ledger(root / 's')['calls'][0]['outcome'], 'not_sent')

    def test_report_cache_does_not_cross_output_contracts(self):
        """Avoid silently replaying a saved report task under a different format.

        Args: None.
        Returns: None; the real report cache accepts a matching contract and rejects
            mismatching/legacy identity despite identical model/request/evidence.
        """
        from types import SimpleNamespace
        from projectclarity.reporting.batched_reporting import BoundedReportBuilder
        from projectclarity.reporting.report_payloads import digest
        with TemporaryDirectory() as tmp:
            root = Path(tmp); prior = root / 'prior/tasks'; prior.mkdir(parents=True)
            request = {'model': 'same-model', 'messages': []}
            for index, identity in enumerate(('current', 'different', None)):
                receipt = {'request': request, 'request_sha256': digest(request),
                           'status': 'response_received', 'evidence_sha256': 'evidence',
                           'provider': 'bedrock', 'contract_id': identity}
                (prior / 'task.json').write_text(json.dumps(receipt))
                attempt = root / f'attempt-{index}'; attempt.mkdir()
                owner = SimpleNamespace(root=attempt, session_root=root, progress={},
                    state={'report_history': [{'artifact_directory': 'prior'}]},
                    caller=SimpleNamespace(provider='bedrock', contract_id='current'))
                builder = BoundedReportBuilder(owner, 'evidence')
                self.assertEqual(bool(builder.cache), identity == 'current')
