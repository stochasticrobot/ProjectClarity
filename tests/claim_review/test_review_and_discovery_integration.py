"""Exercise the actual CLI, graph, SDK, parsers and file persistence without live services.

Only provider HTTP responses, source connection pools and DNS are synthetic.
The shared harness forbids real sockets and scans all artifacts for its fake key.
These tests establish orchestration and fail-closed policy, not model judgment
quality. Review labels below are authored fixtures, not benchmark scores.
"""
import json
import unittest
from unittest.mock import patch

import httpx2

from tests.planning import test_planning_cli_integration as planning_tests
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns
from tests.extraction.extraction_test_fixtures import proposed_batch, SOURCE_TEXT
from projectclarity.providers.model_call_inspection import inspect_calls
from projectclarity.retrieval.public_url_fetcher import PublicWebReader


class ReviewDiscoveryIntegrationTests(unittest.TestCase):
    """Run both rounds through production entry points, including adverse responses."""

    def setUp(self):
        """Create a seed with duplicate/disallowed links and a child with a forbidden second hop."""
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan["clarifications"] = []
        self.routes = {
            ("example.org", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
            ("example.org", "/research"): (200, {"Content-Type": "text/html"},
                ("<main><p>" + SOURCE_TEXT + '</p><a href="/team">Team</a>'
                 '<a href="/team#member">Duplicate team</a><a href="https://elsewhere.org/people">Other</a>'
                 '<a href="http://127.0.0.1/private">Private</a></main>').encode()),
            ("example.org", "/team"): (200, {"Content-Type": "text/html"},
                ("<main><p>" + SOURCE_TEXT + '</p><a href="/second-hop">More people</a></main>').encode()),
        }
        self.http = FakeHTTP(self.routes)
        pool = patch("projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", side_effect=self.http.pool)
        pool.start()
        self.addCleanup(pool.stop)
        self.overrides = {}
        self.seed_artifacts = {}
        self.review_verdict = "supported"
        self.review_in_scope = True
        self.empty_seed = False

    def provider(self, request):
        """Return task-shaped replies through the real SDK; optionally inject one controlled fault."""
        body = json.loads(request.content)
        schema = body["output_config"]["format"]["schema"]["title"]
        if schema in self.overrides:
            return self.overrides[schema](body)
        if schema == "ResearchPlan":
            self.assertFalse(self.http.requests, "Planning must happen before fetching")
            return self.cli.response()
        payload = json.loads(body["messages"][0]["content"])
        if schema == "ExtractionBatch":
            batch = {"entities": [], "claims": []} if self.empty_seed and payload["source_id"] == "S1" else proposed_batch()
            return self.cli.response(text=json.dumps(batch))
        if schema == "ClaimReviewBatch":
            self.assertIn(SOURCE_TEXT.strip(), payload["passage"]["text"])
            answer = {"assessments": [{"candidate_id": c["candidate_id"], "verdict": self.review_verdict,
                "in_scope": self.review_in_scope, "rationale": "Fixture assessment against saved passage.",
                "evidence_quotes": ["Alice works at Lab X."]} for c in payload["candidates"]]}
            return self.cli.response(text=json.dumps(answer))
        self.assertEqual(schema, "FollowupPlan")
        self.assertEqual([l["link_id"] for l in payload["links"]], ["S1_L1"])
        directory, _ = self.cli.saved_session()
        for relative in ["documents/S1/raw.bin", "documents/S1/text.txt", "extraction_calls/S1_P1.json"]:
            self.seed_artifacts[relative] = (directory / relative).read_bytes()
        if (directory / "llm_calls/review_S1_P1.json").exists():
            self.seed_artifacts["llm_calls/review_S1_P1.json"] = (directory / "llm_calls/review_S1_P1.json").read_bytes()
        answer = {"gaps": ["Find additional relevant people."], "selections": [
            {"link_id": "S1_L1", "goal_ids": ["G1"], "reason": "A team listing may name further researchers."}],
            "stop_reason": "One bounded lead is useful."}
        return self.cli.response(text=json.dumps(answer))

    def run_research(self, *flags, follow=True, budget=8):
        """Start from the root CLI shortcut and return actual persisted results."""
        args = ["--extract", "--review-claims", "--max-requests", str(budget)]
        if follow:
            args.append("--follow-links")
        result = self.cli.run_cli(self.provider, *args, *flags, plan_only=False,
            direct_entry=True, source_dns=public_dns)
        directory, session = self.cli.saved_session()
        return (*result, directory, session)

    def test_real_cli_reviews_adjudicates_and_follows_only_one_observed_hop(self):
        """All six calls and both document acquisitions occur on the real production route."""
        code, out, err, directory, session = self.run_research()
        self.assertEqual(code, 0, err)
        self.assertEqual(session["usage"]["requests_attempted"], 6)
        self.assertEqual(session["usage"]["total_tokens"], 6 * 370)
        self.assertEqual(session["visited_nodes"].count("retrieve_sources"), 2)
        self.assertEqual(session["visited_nodes"].count("review_claims"), 2)
        self.assertEqual(session["visited_nodes"].count("select_followup_sources"), 2)
        self.assertEqual([r[1] for r in self.http.requests], ["/robots.txt", "/research", "/team"])
        self.assertEqual(session["retrieval_usage"]["http_requests"], 3)
        expected_bytes = sum(len(self.routes[("example.org", path)][2]) for path in ["/robots.txt", "/research", "/team"])
        self.assertEqual(session["retrieval_usage"]["bytes_read"], expected_bytes)
        self.assertEqual(len(session["extraction"]["attempts"]), 2)
        candidates = session["extraction"]["candidates"]
        self.assertEqual([c["candidate_id"] for c in candidates], ["S1_P1_C1", "S2_P1_C1"])
        self.assertEqual({c["adjudication_status"] for c in candidates}, {"supported"})
        self.assertEqual({c["publication_status"] for c in candidates}, {"not_published"})
        self.assertEqual(session["sources"][1]["discovered_from_link"], "S1_L1")
        self.assertEqual(session["sources"][1]["depth"], 1)
        discovery = session["discovery"]
        self.assertEqual(discovery["stop_reason"], "single_hop_complete")
        self.assertEqual(discovery["links"][0]["parent_raw_sha256"], session["documents"][0]["raw_sha256"])
        self.assertEqual(discovery["links"][1]["exclusion_reason"], "already_known_or_duplicate")
        for relative, original in self.seed_artifacts.items():
            self.assertEqual((directory / relative).read_bytes(), original, relative)
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["counts"]["total"], 2)
        self.assertEqual(status["counts"]["retrieved"], 2)
        self.assertIn("supported=2", out)
        self.assertIn("2 supported by review", err)
        graph = (directory / "workflow_graph.txt").read_text()
        self.assertNotIn("edge absent", graph)
        self.assertIn("[select_followup_sources]", graph)
        trace = json.loads((directory / "llm_calls/review_S1_P1.json").read_text())
        self.assertEqual(trace["prompt_version"], "claim-review-v1")
        self.assertEqual(trace["parsed_result"]["assessments"][0]["candidate_id"], "S1_P1_C1")
        self.assertEqual((directory / "llm_calls/review_S1_P1.json").stat().st_mode & 0o777, 0o600)
        request_count = len(self.cli.requests)
        self.assertIn("review_S1_P1", inspect_calls(self.cli.output, session["session_id"]))
        self.assertIn("messages", inspect_calls(self.cli.output, session["session_id"], "review_S1_P1"))
        self.assertEqual(len(self.cli.requests), request_count)
        events = (directory / "events.jsonl").read_text()
        self.assertNotIn("Alice", events)
        self.assertNotIn("Fixture assessment", events)

    def test_review_without_discovery_never_fetches_links(self):
        """An opt-in review does not grant permission to visit discovered URLs."""
        code, _, err, _, session = self.run_research(follow=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.http.requests), 2)
        self.assertEqual(len(self.cli.requests), 3)
        self.assertFalse(session["discovery"])

    def test_unknown_reviewer_ids_fail_closed(self):
        """A syntactically valid reply for a different claim cannot approve this candidate."""
        def wrong_id(body):
            """Return a valid schema with an invented candidate identifier."""
            return self.cli.response(text=json.dumps({"assessments": [{"candidate_id": "invented",
                "verdict": "supported", "in_scope": True, "rationale": "Looks supported.",
                "evidence_quotes": ["Alice works at Lab X."]}]}))
        self.overrides["ClaimReviewBatch"] = wrong_id
        code, _, _, _, session = self.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "needs_review")
        self.assertEqual(session["claim_reviews"]["S1_P1_C1"]["error_code"], "review_candidate_id_mismatch")

    def test_invalid_selection_ids_do_not_trigger_network_requests(self):
        """A model cannot use valid-looking JSON to authorize an unobserved destination."""
        self.overrides["FollowupPlan"] = lambda body: self.cli.response(text=json.dumps({
            "gaps": [], "selections": [{"link_id": "S99_L1", "goal_ids": ["G1"], "reason": "Invented lead."}],
            "stop_reason": "Investigate."}))
        code, _, _, _, session = self.run_research()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.http.requests), 2)
        self.assertEqual(len(session["sources"]), 1)
        self.assertEqual(session["discovery"]["stop_reason"], "invalid_selection_references")

    def test_empty_extraction_can_still_discover_a_useful_link(self):
        """No seed claims means no review call, not an automatic end to research leads."""
        self.empty_seed = True
        code, _, err, _, session = self.run_research()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(session["documents"]), 2)
        self.assertEqual(len(session["extraction"]["candidates"]), 1)
        self.assertEqual(session["extraction"]["candidates"][0]["source_id"], "S2")
        self.assertEqual(len(self.cli.requests), 5)

    def test_followup_redirect_cannot_expand_origin_permission(self):
        """Even a selected link must not redirect us to an unapproved origin."""
        self.routes[("example.org", "/team")] = (302, {"Location": "https://elsewhere.org/people"}, b"")
        code, _, _, _, session = self.run_research()
        self.assertEqual(code, 1)
        self.assertEqual(session["documents"][1]["error_code"], "discovery_origin_denied")
        self.assertEqual([r[2]["headers"]["Host"] for r in self.http.requests], ["example.org"] * 3)
        self.assertEqual(len(session["extraction"]["candidates"]), 1)

    def test_http_budget_is_not_reset_for_discovery(self):
        """The first round's robots/page requests exhaust a two-request allowance."""
        code, _, err, _, session = self.run_research("--max-http-requests", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(session["discovery"]["stop_reason"], "fetch_budget_exhausted")
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual(len(self.http.requests), 2)

    def test_truncated_review_is_preserved_but_not_adjudicated_as_supported(self):
        """Answer text remains inspectable even when the provider stops at its output cap."""
        self.overrides["ClaimReviewBatch"] = lambda body: self.cli.response(text='{"assessments":', stop_reason="max_tokens")
        code, _, _, directory, session = self.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "needs_review")
        trace = json.loads((directory / "llm_calls/review_S1_P1.json").read_text())
        self.assertEqual(trace["response_text"], '{"assessments":')
        self.assertEqual(trace["error_code"], "incomplete_model_output")

    def test_review_provider_failure_is_not_zero_usage_or_a_positive_verdict(self):
        """A rate-limit response leaves a failure receipt without copying an exception body."""
        self.overrides["ClaimReviewBatch"] = lambda body: httpx2.Response(429, json={
            "type": "error", "error": {"type": "rate_limit_error", "message": "PRIVATE_ERROR_BODY"}})
        code, _, _, directory, session = self.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertIsNone(session["usage"]["total_tokens"])
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "needs_review")
        self.assertNotIn("PRIVATE_ERROR_BODY", (directory / "llm_calls/review_S1_P1.json").read_text())

    def test_insufficient_evidence_remains_uncertain_not_rejected_as_false(self):
        """A complete review can conclude unknown without turning that into contradiction."""
        self.review_verdict = "insufficient_evidence"
        code, _, err, _, session = self.run_research(follow=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "insufficient_evidence")

    def test_contradiction_is_retained_without_publishing(self):
        """The Python policy keeps a valid contrary assessment separate from accepted knowledge."""
        self.review_verdict = "contradicted"
        code, _, err, _, session = self.run_research(follow=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "contradicted")
        self.assertEqual(session["extraction"]["candidates"][0]["publication_status"], "not_published")

    def test_out_of_scope_support_is_not_a_research_answer(self):
        """Even a supported passage can be irrelevant to the user's scope."""
        self.review_in_scope = False
        code, _, err, _, session = self.run_research(follow=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "out_of_scope")

    def test_fabricated_review_quotation_fails_closed(self):
        """The second model is not trusted to manufacture its own supporting quotations."""
        self.overrides["ClaimReviewBatch"] = lambda body: self.cli.response(text=json.dumps({
            "assessments": [{"candidate_id": "S1_P1_C1", "verdict": "supported", "in_scope": True,
                "rationale": "Invented support.", "evidence_quotes": ["A sentence absent from this source."]}]}))
        code, _, _, _, session = self.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertEqual(session["claim_reviews"]["S1_P1_C1"]["error_code"], "review_quote_not_found")
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "needs_review")

    def test_source_changed_during_review_cannot_keep_supported_status(self):
        """Adjudication independently rechecks source bytes after the reviewer returns."""
        def change_source(body):
            """Mutate only the temporary saved evidence while returning a positive review."""
            directory, _ = self.cli.saved_session()
            (directory / "documents/S1/text.txt").write_text("Changed after the reviewer read it.")
            return self.cli.response(text=json.dumps({"assessments": [{"candidate_id": "S1_P1_C1",
                "verdict": "supported", "in_scope": True, "rationale": "Was supported.",
                "evidence_quotes": ["Alice works at Lab X."]}]}))
        self.overrides["ClaimReviewBatch"] = change_source
        code, _, _, _, session = self.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertEqual(session["extraction"]["candidates"][0]["adjudication_status"], "invalid_evidence")

    def test_cancellation_after_review_prevents_discovery(self):
        """Operator cancellation keeps candidates but sends no selection or follow-up request."""
        def cancel(body):
            """Use the same marker as the cancel command during the synthetic in-flight response."""
            directory, _ = self.cli.saved_session()
            (directory / "cancel.request").write_text("stop")
            return self.cli.response(text=json.dumps({"assessments": []}))
        self.overrides["ClaimReviewBatch"] = cancel
        code, _, _, _, session = self.run_research()
        self.assertEqual(code, 130)
        self.assertEqual(session["status"], "cancelled")
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual(len(self.http.requests), 2)
        self.assertEqual(len(session["extraction"]["candidates"]), 1)

    def test_no_links_stops_without_a_discovery_model_call(self):
        """A plain source is a valid bounded stop, not an excuse to invent a search URL."""
        self.routes[("example.org", "/research")] = (200, {"Content-Type": "text/plain"}, SOURCE_TEXT.encode())
        code, _, err, _, session = self.run_research()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual(session["discovery"]["stop_reason"], "no_eligible_links")

    def test_total_byte_allowance_is_shared_by_both_rounds(self):
        """The child is refused after charging bytes beyond the remaining cumulative allowance."""
        seed_bytes = sum(len(self.routes[("example.org", path)][2]) for path in ["/robots.txt", "/research"])
        code, _, _, _, session = self.run_research("--max-total-bytes", str(seed_bytes + 1))
        self.assertEqual(code, 1)
        self.assertEqual(session["documents"][1]["error_code"], "total_size_limit")
        self.assertGreater(session["retrieval_usage"]["bytes_read"], seed_bytes)
        self.assertEqual(len(session["extraction"]["candidates"]), 1)

    def test_llm_allowance_is_shared_and_reserves_minimum_followup_work(self):
        """Two seed pages use five calls; one remaining call cannot authorize an unfinished hop."""
        self.cli.urls.write_text("https://example.org/research\nhttps://example.org/seed-two\n")
        self.routes[("example.org", "/seed-two")] = (200, {"Content-Type": "text/plain"}, SOURCE_TEXT.encode())
        code, _, err, _, session = self.run_research(budget=6)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.cli.requests), 5)
        self.assertEqual(session["discovery"]["stop_reason"], "request_or_source_limit")
        self.assertEqual(len(session["documents"]), 2)

    def test_elapsed_allowance_includes_intervening_model_work(self):
        """Simulate elapsed review time on the actual shared reader; discovery must stop before selection."""
        readers = []
        original_fetch = PublicWebReader.fetch

        def remember_reader(reader, url):
            """Retain the actual production reader while still exercising its fetch implementation."""
            readers.append(reader)
            return original_fetch(reader, url)

        def slow_review(body):
            """Advance the reader's elapsed clock without a real three-minute test delay."""
            readers[0].budget.started -= 181
            return self.cli.response(text=json.dumps({"assessments": [{"candidate_id": "S1_P1_C1",
                "verdict": "supported", "in_scope": True, "rationale": "Fixture review.",
                "evidence_quotes": ["Alice works at Lab X."]}]}))

        self.overrides["ClaimReviewBatch"] = slow_review
        with patch.object(PublicWebReader, "fetch", remember_reader):
            code, _, err, _, session = self.run_research()
        self.assertEqual(code, 0, err)
        self.assertEqual(session["discovery"]["stop_reason"], "fetch_budget_exhausted")
        self.assertGreaterEqual(session["retrieval_usage"]["elapsed_seconds"], 181)
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual(len(self.http.requests), 2)

    def test_review_budget_exhaustion_leaves_explicit_unfinished_work(self):
        """A three-call cap used by plan plus two passages cannot masquerade as completed review."""
        self.cli.urls.write_text("https://example.org/research\nhttps://example.org/seed-two\n")
        self.routes[("example.org", "/seed-two")] = (200, {"Content-Type": "text/plain"}, SOURCE_TEXT.encode())
        code, _, _, _, session = self.run_research(follow=False, budget=3)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual({c["adjudication_status"] for c in session["extraction"]["candidates"]}, {"needs_review"})
        self.assertEqual(len(session["processing_calls"]), 2)
        self.assertEqual({c["error_code"] for c in session["processing_calls"]}, {"request_limit"})
        self.assertEqual({c["status"] for c in session["processing_calls"]}, {"not_attempted"})

    def test_same_route_operates_on_an_unrelated_supplied_origin(self):
        """Changing the seed domain needs no site-specific code or preapproved organization names."""
        self.cli.urls.write_text("https://archive.example.net/research\n")
        self.http.routes = {("archive.example.net", path): reply for (_, path), reply in self.routes.items()}
        code, _, err, _, session = self.run_research()
        self.assertEqual(code, 0, err)
        self.assertEqual(session["sources"][1]["url"], "https://archive.example.net/team")
        self.assertEqual([r[2]["headers"]["Host"] for r in self.http.requests], ["archive.example.net"] * 3)
        self.assertEqual(session["discovery"]["stop_reason"], "single_hop_complete")
