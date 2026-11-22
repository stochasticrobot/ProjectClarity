"""Check live-suite safeguards offline; these tests do not claim model success."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from testing.live_model_tests import CASES, build_request, check_output, main, run_live

GOOD = {
    'basic-types': {'count': 5, 'label': 'beads', 'present': True},
    'nested-null': {'items': [{'name': 'null', 'colour': None}], 'note': None},
    'source-extraction': {
        'entities': [{'id': 'cedar', 'name': 'Cedar Robotics', 'type': 'organization', 'subtype': None}],
        'facts': [{'subject_id': 'cedar', 'predicate': 'based_in', 'value': 'Halifax',
                   'object_id': None, 'statement': 'Cedar Robotics is based in Halifax.',
                   'quote': 'Cedar Robotics is based in Halifax.'}]},
}


class LiveModelSuiteTests(unittest.TestCase):
    """Prove preview isolation, strict scoring and no silent retries or skips."""

    def test_preview_uses_config_without_dispatch(self):
        """Use dotenv selection without constructing a caller or output directory.

        Args: None.
        Returns: None; fails if preview dispatches or selects hardcoded Bedrock.
        """
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {}, clear=True):
            root = Path(temp); env = root / '.env'
            env.write_text('PROJECTCLARITY_PROVIDER=anthropic\nPROJECTCLARITY_MODEL=test-model\n')
            output = io.StringIO()
            with patch('testing.live_model_tests.ModelCalls') as caller, redirect_stdout(output):
                code = main(['--env-file', str(env), '--output-root', str(root / 'runs')])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['model'], 'test-model')
            caller.assert_not_called()
            self.assertFalse((root / 'runs').exists())

    def test_valid_outputs_and_provider_neutral_requests(self):
        """Accept valid fixtures and forward any configured model unchanged.

        Args: None.
        Returns: None; checks all three cases without external calls.
        """
        for case in CASES:
            check_output(case, json.dumps(GOOD[case]))
            self.assertEqual(build_request(case, 'future-model')['model'], 'future-model')

    def test_wrong_answer_and_types_fail(self):
        """Reject well-formed wrong answers, coercion, extras and missing values.

        Args: None.
        Returns: None; raises test failure if any malformed fixture is accepted.
        """
        for value in [dict(GOOD['basic-types'], count=6), dict(GOOD['basic-types'], count='5'),
                      dict(GOOD['basic-types'], present=1), dict(GOOD['basic-types'], extra=True), {}]:
            with self.assertRaises(ValueError):
                check_output('basic-types', json.dumps(value))
        with self.assertRaises(ValueError):
            check_output('nested-null', '{"items":[{"name":"null","colour":"null"}],"note":null}')

    def test_empty_or_unsupported_extraction_fails(self):
        """Reject empty facts, fabricated quotes and unresolved entity references.

        Args: None.
        Returns: None; validates negative evidence using the real domain checks.
        """
        with self.assertRaises(ValueError):
            check_output('source-extraction', '{"entities":[],"facts":[]}')
        for field, value in [('quote', 'Invented.'), ('object_id', 'missing'), ('value', 'Ottawa')]:
            result = json.loads(json.dumps(GOOD['source-extraction']))
            result['facts'][0][field] = value
            with self.assertRaises(ValueError):
                check_output('source-extraction', json.dumps(result))

    def test_failure_is_counted_once_and_other_cases_continue(self):
        """Keep a transport failure as failed evidence without retry or secret echo.

        Args: None.
        Returns: None; checks three calls, retained summary and validation marking.
        """
        with tempfile.TemporaryDirectory() as temp, patch('testing.live_model_tests.ModelCalls') as factory:
            caller = factory.return_value
            caller.provider = 'test'; caller.model = 'configured'; caller.contract_id = 'fixture'
            caller.side_effect = [RuntimeError('SECRET'), json.dumps(GOOD['nested-null']),
                                  json.dumps(GOOD['source-extraction'])]
            output = io.StringIO()
            with redirect_stdout(output):
                folder, result = run_live(Path(temp), Path(temp) / '.env')
            self.assertFalse(result['passed'])
            self.assertEqual(caller.call_count, 3)
            self.assertEqual(caller.mark_validation.call_count, 2)
            self.assertNotIn('SECRET', output.getvalue() + (folder / 'results.json').read_text())
            self.assertEqual((folder / 'results.json').stat().st_mode & 0o777, 0o600)

    def test_validation_failure_marks_receipt_rejected(self):
        """Record wrong output as rejected rather than successful transport.

        Args: None.
        Returns: None; confirms rejection marking and false overall result.
        """
        with tempfile.TemporaryDirectory() as temp, patch('testing.live_model_tests.ModelCalls') as factory:
            caller = factory.return_value
            caller.provider = 'test'; caller.model = 'configured'; caller.contract_id = 'fixture'
            caller.side_effect = ['{}', json.dumps(GOOD['nested-null']), json.dumps(GOOD['source-extraction'])]
            with redirect_stdout(io.StringIO()):
                _, result = run_live(Path(temp), Path(temp) / '.env')
            self.assertFalse(result['passed'])
            self.assertEqual(result['results'][0]['phase'], 'validation')
            self.assertFalse(caller.mark_validation.call_args_list[0].args[2])

    def test_cli_returns_nonzero_for_failed_live_run(self):
        """Make failed live checks usable as a command-line gate.

        Args: None.
        Returns: None; verifies nonzero status without any provider access.
        """
        with tempfile.TemporaryDirectory() as temp, patch('testing.live_model_tests.run_live') as run:
            run.return_value = (Path(temp), {'passed': False})
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--live', '--env-file', str(Path(temp) / '.env')]), 1)


if __name__ == '__main__':
    unittest.main()
