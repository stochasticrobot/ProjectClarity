"""Run retrieval through the actual command and LangGraph, without live services.

For example, one supplied URL and one local PDF must produce saved text and
matching provenance hashes. We substitute the Anthropic HTTP reply, source HTTP
connection pool, and DNS only; graph routing, source checks, parser subprocesses,
and local files stay real. These are integration checks, not LLM-quality scores.
"""

import hashlib
import json
import unittest
from unittest.mock import patch

from tests.planning import test_planning_cli_integration as planning_tests
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns, text_pdf
from projectclarity.retrieval.public_url_fetcher import PublicWebReader
from projectclarity.retrieval import source_retrieval


class RetrievalCLIIntegrationTests(unittest.TestCase):
    """Reuse the existing real-module CLI harness without duplicating its test cases."""

    def setUp(self):
        """Reuse the real CLI harness and substitute source HTTP, with a plan that permits retrieval."""
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan["clarifications"] = []
        self.routes = {("example.org", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
                       ("example.org", "/research"): (200, {"Content-Type": "text/html"},
                         b"<html><main><h1>Source fixture</h1><p>Visible research text.</p></main><script>unwanted_script()</script></html>")}
        self.http = FakeHTTP(self.routes)
        pool = patch("projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", side_effect=self.http.pool)
        pool.start()
        self.addCleanup(pool.stop)

    def test_real_cli_retrieves_url_and_pdf_with_inspectable_provenance(self):
        """The retrieval node must produce both artifacts after planning/review, not just log its name."""
        document = self.cli.directory / "report.pdf"
        pdf = text_pdf()
        document.write_bytes(pdf)
        self.cli.plan["strategy"][0]["source_ids"] = ["S1", "S2"]
        code, stdout, stderr = self.cli.run_cli(lambda request: self.cli.response(),
            "--document", str(document), plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 0, stderr)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research", "review_plan", "retrieve_sources"])
        self.assertEqual(saved["mode"], "plan_and_retrieve")
        self.assertEqual(saved["status"], "sources_retrieved")
        # Retrieval rewrites plan.md with acquisition results. The planner's
        # category must remain present after that final production write too.
        category = self.cli.plan["question_category"]
        self.assertEqual(saved["plan"]["question_category"], category)
        self.assertEqual(json.loads((directory / "plan.json").read_text())["question_category"], category)
        self.assertIn("## Question category\n" + category, stdout)
        self.assertIn("## Question category\n" + category, (directory / "plan.md").read_text())
        self.assertEqual(saved["usage"]["requests_attempted"], 1)
        self.assertEqual(saved["retrieval_usage"]["http_requests"], 2)
        manifest = json.loads((directory / "documents.json").read_text())
        self.assertEqual(manifest["documents"], saved["documents"])
        for record in saved["documents"]:
            raw = (directory / record["raw_path"]).read_bytes()
            text = (directory / record["text_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record["raw_sha256"])
            self.assertEqual(hashlib.sha256(text).hexdigest(), record["text_sha256"])
            self.assertEqual(len(raw), record["byte_count"])
            self.assertIsNotNone(record["acquired_at"])
            self.assertEqual(json.loads((directory / "documents" / record["source_id"] / "metadata.json").read_text()), record)
        self.assertEqual((directory / "documents/S2/raw.bin").read_bytes(), pdf)
        self.assertIn("Canadian AI security", (directory / "documents/S2/text.txt").read_text())
        self.assertNotIn("unwanted_script", (directory / "documents/S1/text.txt").read_text())
        self.assertEqual(saved["documents"][0]["final_url"], "https://example.org/research")
        self.assertIn("2/2 sources", stdout)
        self.assertNotIn("No sources fetched", stdout)
        # This run did not opt into extraction, so only planning calls the LLM.
        self.assertEqual(len(self.cli.requests), 1)
        self.assertNotIn(b"Visible research text", self.cli.requests[0].content)

    def test_main_entry_shortcut_accepts_url_list_and_multiple_documents(self):
        """Root-level flags retain mixed sources and reach the actual retrieval node."""
        first = self.cli.directory / "first note.txt"
        second = self.cli.directory / "second note.txt"
        first.write_text("First local evidence.\n")
        second.write_text("Second local evidence.\n")
        self.cli.plan["strategy"][0]["source_ids"] = ["S1", "S2", "S3"]
        code, stdout, stderr = self.cli.run_cli(lambda request: self.cli.response(),
            "--document", str(first), "--document", str(second),
            plan_only=False, source_dns=public_dns, direct_entry=True)
        self.assertEqual(code, 0, stderr)
        directory, session = self.cli.saved_session()
        self.assertEqual(session["status"], "sources_retrieved")
        self.assertEqual(session["request"]["urls_file"], str(self.cli.urls))
        self.assertEqual(session["request"]["document_paths"], [str(first), str(second)])
        self.assertEqual([source["kind"] for source in session["sources"]], ["url", "file", "file"])
        self.assertIn("Visible research text.", (directory / "documents/S1/text.txt").read_text())
        self.assertEqual((directory / "documents/S2/text.txt").read_text(), first.read_text())
        self.assertEqual((directory / "documents/S3/text.txt").read_text(), second.read_text())
        self.assertEqual(len(self.cli.requests), 1)
        self.assertNotIn("What would you like to research?", stdout)

    def test_one_failed_source_does_not_discard_other_sources(self):
        """A failed URL and malformed PDF retain their own receipts while a text file succeeds."""
        self.routes[("example.org", "/research")] = (404, {}, b"")
        bad = self.cli.directory / "bad.pdf"
        bad.write_bytes(b"%PDF-not-valid")
        good = self.cli.directory / "notes.txt"
        good.write_text("Readable local notes.\n")
        code, stdout, _ = self.cli.run_cli(lambda request: self.cli.response(),
            "--document", str(bad), "--document", str(good), plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 1)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved["status"], "retrieval_partial")
        self.assertEqual([r["status"] for r in saved["documents"]], ["failed", "failed", "retrieved"])
        self.assertIn("HTTP 404", saved["documents"][0]["error"])
        self.assertIsNotNone(saved["documents"][1]["raw_path"])
        self.assertIsNone(saved["documents"][1]["text_path"])
        self.assertEqual((directory / "documents/S3/text.txt").read_text(), "Readable local notes.\n")
        self.assertIn("1/3 sources", stdout)

    def test_original_plan_is_saved_before_any_source_is_acquired(self):
        """Guard the real URL and local readers, not merely the recorded node names.

        Both readers must see the completed, saved original plan before they
        begin. They then execute normally, so this cannot pass by skipping fetch.
        The provider callback also checks that no acquisition happened earlier.
        """
        document = self.cli.directory / "evidence.txt"
        document.write_text("SOURCE_CONTENT_ONLY_AVAILABLE_AFTER_PLANNING\n")
        self.cli.plan["strategy"][0]["source_ids"] = ["S1", "S2"]
        acquisitions = []
        original_fetch = PublicWebReader.fetch
        original_read = source_retrieval.read_local

        def require_original_plan(kind):
            """Assert the original plan is saved and counted before recording an acquisition attempt."""
            directory, session = self.cli.saved_session()
            self.assertEqual(session["plan"], self.cli.plan)
            self.assertEqual(json.loads((directory / "plan.json").read_text()), self.cli.plan)
            self.assertEqual(session["visited_nodes"], ["initialize_research", "plan_research", "review_plan", "retrieve_sources"])
            self.assertEqual(session["usage"]["requests_attempted"], 1)
            acquisitions.append(kind)

        def fetch_after_plan(reader, url):
            """Check planning order, then delegate URL acquisition to the real public fetcher."""
            require_original_plan("url")
            return original_fetch(reader, url)

        def read_after_plan(path, budget):
            """Check planning order, then delegate local reading to the real acquisition helper."""
            require_original_plan("file")
            return original_read(path, budget)

        def planning_reply(request):
            """Verify no acquisition occurred before the provider returns its planning fixture."""
            self.assertEqual(acquisitions, [])
            self.assertEqual(self.http.requests, [])
            directory, session = self.cli.saved_session()
            self.assertFalse((directory / "documents.json").exists())
            self.assertIsNone(session["plan"])
            self.assertNotIn(b"SOURCE_CONTENT_ONLY_AVAILABLE_AFTER_PLANNING", request.content)
            body = json.loads(request.content)
            content = body["messages"][0]["content"]
            payload = json.loads(content if isinstance(content, str) else content[0]["text"])
            self.assertEqual(payload["run_mode"], "plan_and_retrieve")
            return self.cli.response()

        with patch.object(PublicWebReader, "fetch", fetch_after_plan), \
             patch.object(source_retrieval, "read_local", read_after_plan):
            code, _, stderr = self.cli.run_cli(planning_reply, "--document", str(document),
                plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(acquisitions, ["url", "file"])
        directory, session = self.cli.saved_session()
        self.assertEqual(session["status"], "sources_retrieved")
        self.assertEqual((directory / "documents/S2/text.txt").read_text(), document.read_text())

    def test_research_uncertainty_is_retained_without_human_review(self):
        """An evidence question belongs in the saved work plan, not a startup pause.

        The model reply is handwritten: this checks that the real workflow
        preserves its investigation task and proceeds to acquire sources. It
        does not prove that a live model puts uncertainty in the right field,
        or that a later extraction/research stage investigates it: this run
        deliberately stops after retrieval and never enables --extract.
        """
        question = "Which candidates have evidence connecting their work to AI security?"
        strategy = "Identify candidates, inspect supporting work, and separate confirmed relevance from unverified adjacent leads."
        assumption = "Adjacent robustness or fairness work is an unverified lead, not a confirmed security result."
        self.cli.plan["goals"][1]["question"] = question
        self.cli.plan["strategy"][0]["action"] = strategy
        self.cli.plan["assumptions"] = [assumption]
        code, stdout, stderr = self.cli.run_cli(lambda request: self.cli.response(),
            plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 0, stderr)
        directory, session = self.cli.saved_session()
        self.assertEqual(session["plan"], self.cli.plan)
        self.assertEqual(session["status"], "sources_retrieved")
        self.assertEqual(session["visited_nodes"], ["initialize_research", "plan_research", "review_plan", "retrieve_sources"])
        self.assertIsNone(session["pending_review"])
        self.assertFalse((directory / "review_request.json").exists())
        self.assertIn("Visible research text.", (directory / "documents/S1/text.txt").read_text())
        for statement in (question, strategy, assumption):
            self.assertIn(statement, stdout)
            self.assertIn(statement, (directory / "plan.md").read_text())
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        self.assertNotIn("review_required", [event["event"] for event in events])
        self.assertEqual(len(self.cli.requests), 1)

    def test_clarifications_stop_normal_mode_before_retrieval(self):
        """An unresolved plan interrupts at review, without silently fetching sources."""
        self.cli.plan["clarifications"] = ["Which time period should the research cover?"]
        code, stdout, _ = self.cli.run_cli(lambda request: self.cli.response(), plan_only=False)
        self.assertEqual(code, 2)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved["status"], "awaiting_review")
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research", "review_plan"])
        self.assertFalse((directory / "documents.json").exists())
        self.assertEqual(self.http.requests, [])
        self.assertIn("Retrieval has not started", stdout)

    def test_explicit_plan_only_stops_even_when_plan_is_ready(self):
        """Retrieval and later nodes must not break the no-source-reading promise of --plan-only."""
        code, _, stderr = self.cli.run_cli(lambda request: self.cli.response(), plan_only=True)
        self.assertEqual(code, 0, stderr)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved["status"], "plan_ready")
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
        self.assertFalse((directory / "documents").exists())
        self.assertEqual(self.http.requests, [])

    def test_invalid_plan_cannot_reach_retrieval(self):
        """A provider reply with missing plan fields must not reach review or retrieval."""
        code, _, _ = self.cli.run_cli(lambda request: self.cli.response(text='{"objective":"Incomplete"}'), plan_only=False)
        self.assertEqual(code, 1)
        _, saved = self.cli.saved_session()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
        self.assertEqual(self.http.requests, [])

    def test_no_supplied_sources_is_not_reported_as_successful_retrieval(self):
        """Zero input documents should produce an actionable message, not 'research complete'."""
        self.cli.plan["strategy"][0]["source_ids"] = []
        code, _, stderr = self.cli.run_cli(lambda request: self.cli.response(), include_urls=False, plan_only=False)
        self.assertEqual(code, 1)
        _, saved = self.cli.saved_session()
        self.assertEqual(saved["status"], "retrieval_failed")
        self.assertIn("No sources supplied", stderr)

    def test_file_changed_to_symlink_after_planning_is_not_followed(self):
        """Recheck local references at read time instead of trusting earlier initialization."""
        document = self.cli.directory / "notes.txt"
        document.write_text("Original notes")
        other = self.cli.directory / "other.txt"
        other.write_text("Do not acquire this replacement")

        def provider_reply(request):
            """Replace only the temporary input with a symlink between initialization and retrieval."""
            document.unlink()
            document.symlink_to(other)
            return self.cli.response()

        code, _, _ = self.cli.run_cli(provider_reply, "--document", str(document), include_urls=False, plan_only=False)
        self.assertEqual(code, 1)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved["documents"][0]["status"], "failed")
        self.assertFalse((directory / "documents/S1/raw.bin").exists())
        self.assertEqual(self.http.requests, [])
