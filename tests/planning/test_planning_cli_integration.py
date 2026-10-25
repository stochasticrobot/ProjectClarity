"""Exercise the production research CLI with an in-memory HTTP provider.

Purpose:
    Run the same research command you use, but answer its model request with
    a handwritten reply instead of contacting Anthropic. For example, a valid
    reply should become plan.md, while a plan citing nonexistent source S999
    should be refused. This catches broken connections between the command,
    graph, model adapter, validation, and saved files.
Responsibilities:
    Run projectclarity.__main__, substitute synthetic credentials and HTTP
    responses, deny real sockets/DNS, and inspect requests, statuses, saved files,
    budget enforcement, and failure handling. Temporary directories isolate runs.
Typical use:
    Run ``python -m unittest tests.planning.test_planning_cli_integration -v`` or discover the
    suite. Fixtures are handwritten; no developer key or model credits are used.
Boundary:
    The actual CLI/graph/SDK execute, but the model does not. Passing tests prove
    application wiring and validation, not prompt quality or live provider access.
"""

import contextlib
import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from tests.planning.planning_fixtures import planning_reply


FIXTURE = Path(__file__).parent / "fixtures" / "valid_research_plan.json"
TEST_KEY = "sk-ant-projectclarity-offline-only"


class PlanningCLIIntegrationTests(unittest.TestCase):
    """Keep real module execution and provider serialization in the regression path."""

    def setUp(self):
        """Create fresh temporary inputs, a handwritten plan and request counters for each test.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.urls = self.directory / "source_urls.txt"
        self.urls.write_text("https://example.org/research\n")
        self.output = self.directory / "runs"
        self.plan = json.loads(FIXTURE.read_text())
        self.requests = []

    def response(self, *, text=None, stop_reason="end_turn"):
        """A provider-shaped message lets the real SDK and schema parser do their work.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            text: Synthetic provider answer text; when None, use the harness's default valid
                planning fixture translated to the initial-reply schema. Explicit text
                is passed through unchanged, including malformed replies.
            stop_reason: Synthetic provider stop reason, such as end_turn or max_tokens, used to
                exercise output-completion handling.

        Returns:
            A synthetic httpx2.Response passed through the real provider SDK; no network request
            is made by this responder.
        """
        return httpx2.Response(200, json={
            "id": "msg_planning_fixture", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps(planning_reply(self.plan)) if text is None else text}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 150, "output_tokens": 220},
        })

    def run_cli(self, provider_response, *extra_args, include_urls=True, key_in_file=False, plan_only=True, source_dns=None, direct_entry=False, details=True, knowledge=False):
        """Invoke __main__, intercept HTTP, and fail any accidental real networking.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            provider_response: Callable receiving each SDK HTTP request and returning a
                synthetic httpx2.Response.
            include_urls: Whether to supply the harness's URL-list fixture in addition to any
                local-document arguments.
            key_in_file: Whether the synthetic API key comes from the temporary .env file
                instead of the test environment.
            plan_only: Whether this run stops after planning instead of entering review and
                retrieval.
            source_dns: Optional DNS substitute for source retrieval; None rejects all DNS
                attempts.
            direct_entry: Whether to invoke the root CLI shortcut rather than the explicit
                research subcommand.
            details: Whether to request the full CLI plan/graph display instead of compact
                output.
            knowledge: True explicitly enables auxiliary capture, False adds the explicit
                disable flag, and None leaves CLI defaults untouched. Tests may override
                the auxiliary cap using extra_args.
            *extra_args: Additional command-line argument strings appended to this helper's base
                invocation. This harness explicitly selects legacy single-engine/no-cooldown
                defaults; recovery tests override them with real CLI flags.

        Returns:
            A tuple of exit code, captured stdout and captured stderr from the real CLI entry
            point.
        """
        def respond(request):
            """Record each SDK request before handing it to the test-selected fake HTTP responder.

            Args:
                request: Actual httpx2.Request emitted by the SDK; inspected locally and answered
                    with fixture data.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            """
            self.requests.append(request)
            return provider_response(request)

        argv = [
            "projectclarity", "research", "--question", "Who researches AI security in Canada?",
            "--output-dir", str(self.output),
            "--max-fetch-seconds", "180",
            "--search-min-interval-seconds", "0", "--search-retries", "0",
            "--search-fallbacks", "none", "--search-cooldown-seconds", "0", "--search-recovery-rounds", "0",
        ]
        if knowledge is False:
            argv.append("--no-knowledge")
        elif knowledge is True:
            argv.extend(['--knowledge-max-requests', '1000'])
        # Existing plan-rendering assertions explicitly exercise --details;
        # new friendly-output checks pass details=False to cover the default UI.
        if details:
            argv.append("--details")
        if direct_entry:
            # Exercise the public shortcut, not a test-specific workflow.
            argv.remove("research")
        if plan_only:
            argv.append("--plan-only")
        if include_urls:
            argv.extend(["--urls", str(self.urls)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
            patch("langchain_anthropic.chat_models._get_default_httpx_client", return_value=client),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "" if key_in_file else TEST_KEY, "LANGSMITH_TRACING": "true"}),
            patch("projectclarity.nodes.plan_research.research_planner.PROJECT_ENV_FILE", self.directory / ".env"),
            patch("projectclarity.nodes.plan_research.research_planner.getpass.getpass", side_effect=AssertionError("Unexpected key prompt")),
            patch("sys.argv", argv + list(extra_args)),
            patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access")) as connect,
            patch("socket.getaddrinfo", side_effect=source_dns or AssertionError("Unexpected DNS lookup")) as lookup,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            # This invokes projectclarity/__main__.py, not an alternative demo runner.
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_module("projectclarity", run_name="__main__")
            connect.assert_not_called()
            if source_dns is None:
                lookup.assert_not_called()
        self.assertNotIn(TEST_KEY, stdout.getvalue() + stderr.getvalue())
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(TEST_KEY.encode(), path.read_bytes())
        return exit_result.exception.code, stdout.getvalue(), stderr.getvalue()

    def saved_session(self):
        """Require exactly one session, so hidden duplicate runs cannot go unnoticed.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            A pair of the sole saved run-directory Path and its parsed session dictionary; zero
            or multiple runs fail the assertion.
        """
        paths = list(self.output.glob("*/session.json"))
        self.assertEqual(len(paths), 1)
        return paths[0].parent, json.loads(paths[0].read_text())

    def test_project_dotenv_authenticates_without_prompting(self):
        """The production CLI can use the local key file without leaking it to outputs.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        # This temporary synthetic credential is separate from the developer's
        # real file; the shared runner checks all saved files/output for leakage.
        (self.directory / ".env").write_text(f"ANTHROPIC_API_KEY={TEST_KEY}\n")
        code, _, stderr = self.run_cli(lambda request: self.response(), key_in_file=True)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].headers["x-api-key"], TEST_KEY)

    def test_module_entry_runs_both_nodes_and_saves_reviewable_plan(self):
        """The real command sends one bounded request and saves plan plus provenance.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, stdout, stderr = self.run_cli(lambda request: self.response(), "--max-output-tokens", "1024")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.headers["x-api-key"], TEST_KEY)
        body = json.loads(request.content)
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        # This checks the actual serialized request, not merely the Python class:
        # the provider must be asked for the same field the UI will display.
        schema = body["output_config"]["format"]["schema"]
        self.assertIn("question_category", schema["required"])
        self.assertEqual(schema["properties"]["question_category"]["type"], "string")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertNotIn("tools", body)
        self.assertNotIn(TEST_KEY, request.content.decode())
        directory, saved = self.saved_session()
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
        self.assertEqual(saved["status"], "needs_clarification")
        self.assertEqual(saved["usage"]["requests_attempted"], 1)
        self.assertEqual(saved["usage"]["total_tokens"], 370)
        self.assertEqual(json.loads((directory / "plan.json").read_text()), self.plan)
        self.assertEqual(saved["plan"]["question_category"], self.plan["question_category"])
        category_section = "## Question category\n" + self.plan["question_category"]
        self.assertIn(category_section, stdout)
        self.assertIn(category_section, (directory / "plan.md").read_text())
        self.assertIn("Clarifications to resolve", (directory / "plan.md").read_text())
        self.assertIn("No sources fetched or searches executed", stdout)

    def test_question_without_urls_can_produce_a_plan_ready_for_review(self):
        """Supplied URLs are optional; no clarification does not imply approval.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.plan["strategy"][0]["source_ids"] = []
        self.plan["clarifications"] = []
        code, stdout, stderr = self.run_cli(lambda request: self.response(), include_urls=False)
        self.assertEqual(code, 0, stderr)
        _, saved = self.saved_session()
        self.assertEqual(saved["sources"], [])
        self.assertEqual(saved["status"], "plan_ready")
        self.assertIn("No approval or execution has been recorded", stdout)

    def test_serialized_request_explains_expected_pre_retrieval_state(self):
        """The actual provider request must state the invariant as trusted instructions.

        This verifies the prompt/schema handoff, not whether a live model obeys it. The reply is
        still a fixture; model-quality evaluation is separate.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(lambda request: self.response())
        self.assertEqual(code, 0, stderr)
        body = json.loads(self.requests[0].content)
        system = body["system"]
        if isinstance(system, list):
            system = "\n".join(block.get("text", "") for block in system)
        from projectclarity.nodes.plan_research.research_planner import SYSTEM_PROMPT
        # Verify the real SDK sends the one editable prompt, not an extra block.
        self.assertEqual(system, SYSTEM_PROMPT)
        self.assertIn("Do not assume sources have been retrieved or their contents verified", system)
        self.assertNotIn("plan_only", system)
        self.assertIn("Put questions research can\n  answer in goals or strategy", system)
        self.assertIn("Put only questions that require user\n  input before research can start in clarifications", system)
        schema = body["output_config"]["format"]["schema"]
        self.assertIn("Questions answerable during or after research belong in goals and strategy", schema["properties"]["clarifications"]["description"])
        content = body["messages"][0]["content"]
        payload = json.loads(content if isinstance(content, str) else content[0]["text"])
        self.assertIs(payload["source_contents_available"], False)
        self.assertNotIn("run_mode", payload)
        self.assertNotIn("workflow_phase", payload)
        _, saved = self.saved_session()
        self.assertEqual(saved["prompt_version"], "research-plan-v11")

    def test_mixed_sources_keep_document_bodies_and_full_paths_out_of_model_request(self):
        """The real command plans over source references without reading local documents.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        # Equal filenames are intentional: source IDs, not labels, must identify
        # distinct files. The contents are sentinels, not real research documents.
        documents = [self.directory / "one" / "report.pdf", self.directory / "two" / "report.pdf"]
        for document in documents:
            document.parent.mkdir()
            document.write_text("DOCUMENT_CONTENT_MUST_STAY_UNREAD")
        self.plan["strategy"][0]["source_ids"] = ["S1", "S2", "S3"]
        real_open = io.open

        def guard_document_reads(file, *args, **kwargs):
            """Forbid opening source bodies while allowing real URL-list, checkpoint and output I/O.

            Args:
                file: Path or file descriptor passed to the patched open() call.
                *args: Remaining open() arguments, such as mode, forwarded for permitted files.
                **kwargs: open() keyword arguments, such as encoding, forwarded for permitted files.

            Returns:
                The real file object for allowed application I/O; fail the assertion if planning
                opens a source body.
            """
            if isinstance(file, (str, Path)) and Path(file).resolve() in documents:
                self.fail("Planning tried to open a document's contents")
            return real_open(file, *args, **kwargs)

        # Path.open uses io.open. Keep all output/URL-list I/O real while denying
        # opens of the two document files; the runner separately denies networking.
        with patch("io.open", side_effect=guard_document_reads):
            code, stdout, stderr = self.run_cli(
                lambda request: self.response(),
                "--document", str(documents[0]), "--document", str(documents[1]),
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(self.requests), 1)
        wire = self.requests[0].content.decode()
        self.assertNotIn(str(self.directory), wire)
        self.assertNotIn("DOCUMENT_CONTENT_MUST_STAY_UNREAD", wire)
        body = json.loads(wire)
        content = body["messages"][0]["content"]
        payload = json.loads(content if isinstance(content, str) else content[0]["text"])
        self.assertEqual(payload["supplied_sources"], [
            {"source_id": "S1", "kind": "url", "url": "https://example.org/research"},
            {"source_id": "S2", "kind": "file", "label": "report.pdf"},
            {"source_id": "S3", "kind": "file", "label": "report.pdf"},
        ])
        self.assertIs(payload["source_contents_available"], False)
        self.assertEqual(payload["source_policy"], "supplied_only")
        directory, saved = self.saved_session()
        self.assertEqual(saved["schema_version"], "1.7")
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
        self.assertEqual([source["path"] for source in saved["sources"][1:]], [str(path) for path in documents])
        self.assertEqual(json.loads((directory / "plan.json").read_text()), self.plan)
        self.assertIn("S2 [file]", stdout)
        self.assertIn("S3 [file]", (directory / "plan.md").read_text())
        self.assertIn("Local document contents have not been read", stdout)

    def test_document_only_input_does_not_require_a_url_list(self):
        """A file can be S1 and support a proposed step without any --urls argument.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        document = self.directory / "report.txt"
        document.touch()
        code, _, stderr = self.run_cli(lambda request: self.response(), "--document", str(document), include_urls=False)
        self.assertEqual(code, 0, stderr)
        _, saved = self.saved_session()
        self.assertEqual(saved["sources"], [{"source_id": "S1", "kind": "file", "path": str(document)}])

    def test_missing_document_stops_cli_before_provider_or_session(self):
        """The CLI must surface a bad path before any model request can spend credits.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(
            lambda request: self.fail("No request allowed"),
            "--document", str(self.directory / "missing.pdf"),
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.output.exists())
        self.assertIn("Document 1 must be", stderr)

    def test_zero_request_budget_stops_before_provider_invocation(self):
        """A budget is an enforced gate, not merely a value stored in metadata.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(lambda request: self.fail("No request allowed"), "--max-requests", "0")
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [])
        directory, saved = self.saved_session()
        self.assertEqual(saved["usage"]["requests_attempted"], 0)
        self.assertEqual(saved["status"], "failed")
        self.assertIn("budget exhausted", stderr)
        self.assertFalse((directory / "plan.json").exists())

    def test_schema_invalid_response_is_not_published_as_a_plan(self):
        """Reject an incomplete schema even when the HTTP request succeeded.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(lambda request: self.response(text='{"objective": "Incomplete"}'))
        self.assertEqual(code, 1)
        directory, saved = self.saved_session()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["usage"]["total_tokens"], 370)
        self.assertIsNone(saved["plan"])
        self.assertFalse((directory / "plan.md").exists())
        self.assertIn("invalid plan", stderr)

    def test_missing_category_is_rejected_before_retrieval(self):
        """Even an otherwise valid reply cannot silently omit this requested output.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        del self.plan["question_category"]
        self.plan["clarifications"] = []
        code, _, stderr = self.run_cli(lambda request: self.response(), plan_only=False)
        self.assertEqual(code, 1)
        directory, saved = self.saved_session()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
        self.assertIsNone(saved["plan"])
        self.assertFalse((directory / "plan.json").exists())
        self.assertFalse((directory / "documents.json").exists())
        self.assertIn("invalid plan", stderr)

    def test_invented_source_reference_is_rejected(self):
        """A schema-valid plan cannot cite source IDs absent from the session.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.plan["strategy"][0]["source_ids"] = ["S999"]
        code, _, stderr = self.run_cli(lambda request: self.response())
        self.assertEqual(code, 1)
        directory, saved = self.saved_session()
        self.assertIsNone(saved["plan"])
        self.assertFalse((directory / "plan.json").exists())
        self.assertIn("source ID that was not supplied", stderr)

    def test_truncated_response_is_not_accepted_even_if_its_json_parses(self):
        """A parser's repair capability must not turn an unfinished call into success.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(lambda request: self.response(stop_reason="max_tokens"))
        self.assertEqual(code, 1)
        directory, saved = self.saved_session()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(saved["usage"]["stop_reason"], "max_tokens")
        self.assertFalse((directory / "plan.json").exists())
        self.assertIn("did not finish", stderr)

    def test_throttling_has_no_retry_and_redacts_provider_error_body(self):
        """Account failures persist as failures, with unknown tokens and no secret leak.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        response = httpx2.Response(429, json={
            "type": "error", "error": {"type": "rate_limit_error", "message": TEST_KEY},
        })
        code, _, stderr = self.run_cli(lambda request: response)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        _, saved = self.saved_session()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["usage"]["requests_attempted"], 1)
        self.assertIsNone(saved["usage"]["total_tokens"])
        self.assertIn("HTTP 429", stderr)

    def test_invalid_input_never_invokes_provider_or_creates_session(self):
        """The module entry point must stop bad CLI input before any external work.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, stderr = self.run_cli(lambda request: self.fail("No request allowed"), "--question", "   ")
        self.assertEqual(code, 2)
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.output.glob("*/session.json")), [])
        self.assertIn("Invalid input", stderr)
