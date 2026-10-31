"""Exercise question-only discovery through the production terminal entry point and LangGraph.

Only search-provider results, model HTTP replies and source HTTP/DNS are fixtures.
These prove routing/reference/budget behavior, not search relevance or judgment
accuracy. The search adapter/worker have separate transport-boundary checks.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import json
import unittest
from unittest.mock import patch

from tests.planning import test_planning_cli_integration as planning_tests
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns
from tests.extraction.extraction_test_fixtures import proposed_batch, SOURCE_TEXT
from projectclarity.discovery.search_provider import SearchProviderError


class SearchCoveragePipelineTests(unittest.TestCase):
    """Two rounds discover an unknown denominator without hard-coded roster logic."""

    def setUp(self):
        """Prepare independent search results and sources, never a local alternative graph.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan["clarifications"] = []
        self.cli.plan["strategy"][0]["source_ids"] = []
        self.cli.plan["completion_criteria"] = [{"criterion_id": "K1", "goal_ids": ["G1"],
            "description": "Identify everyone on the current roster.", "kind": "enumeration",
            "measurement_method": "Find an authoritative roster total and identify each distinct member.",
            "target_basis": "research", "target_count": None, "limitations": []}]
        self.http = FakeHTTP({
            ("example.org", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
            ("example.org", "/person"): (200, {"Content-Type": "text/plain"}, SOURCE_TEXT.encode()),
            ("directory.example.net", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
            ("directory.example.net", "/roster"): (200, {"Content-Type": "text/plain"}, b"The current roster has one member, Alice.\n"),
        })
        self.round = 0
        self.search_calls = []
        self.overrides = {}
        self.seed_bytes = None
        self.review = False

    def search(self, query, max_results, monitor=None):
        """Return provider-shaped leads; duplicate/private URLs must not enter selection.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            query: Requested query recorded by the harness; fixture results are selected by call
                order.
            max_results: Requested result cap accepted for adapter compatibility; this fixed
                fixture does not derive its records from the cap.
            monitor: Production monitor accepted for adapter compatibility; this responder does
                not emit its own events.

        Returns:
            Provider-shaped result dictionaries selected by the fixture call count, including
            deliberately duplicate or disallowed URLs.
        """
        self.search_calls.append(query)
        if len(self.search_calls) == 1:
            return [{"url": "https://example.org/person", "title": "Person", "snippet": "An unread lead."},
                {"url": "https://example.org/person#duplicate", "title": "Duplicate", "snippet": ""},
                {"url": "http://127.0.0.1/private", "title": "Private", "snippet": ""}]
        return [{"url": "https://directory.example.net/roster", "title": "Roster", "snippet": "Find the total."}]

    def provider(self, request):
        """Respond by actual provider schema; record and inspect the real node payloads.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            request: Actual httpx2.Request emitted by the SDK; inspected locally and answered
                with fixture data.

        Returns:
            A synthetic httpx2.Response passed through the real provider SDK; no network request
            is made by this responder.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        body = json.loads(request.content)
        schema = body["output_config"]["format"]["schema"]["title"]
        if schema in self.overrides:
            return self.overrides[schema](body)
        if schema == "ResearchPlan":
            self.assertFalse(self.search_calls)
            self.assertIn("completion_criteria", body["output_config"]["format"]["schema"]["required"])
            return self.cli.response()
        payload = json.loads(body["messages"][0]["content"])
        if schema == "SearchSourceSelection":
            self.assertEqual(len(payload["results"]), 1)
            answer = {"selections": [{"result_id": payload["results"][0]["result_id"], "goal_ids": ["G1"], "reason": "Useful evidence or lead."}]}
        elif schema == "ExtractionBatch":
            self.assertNotIn("completion_criteria", payload)
            self.assertNotIn('early_coverage_study', payload)
            self.assertNotIn("An unread lead.", payload["passage"]["text"])
            answer = proposed_batch()
            answer["claims"][0]["research_roles"] = ["answer_evidence", "lead"]
            if payload["source_id"] == "S2":
                answer["claims"][0].update(statement="The current roster has one member, Alice.",
                    research_roles=["coverage_evidence"], evidence=[{"passage_id": "P1", "quote": "The current roster has one member, Alice."}])
        elif schema == "ClaimReviewBatch":
            answer = {"assessments": [{"candidate_id": c["candidate_id"], "verdict": "supported", "in_scope": True,
                "rationale": "Fixture review only.", "report_mappings": fixture_mappings(c, payload), "eligibility_assessments": fixture_eligibility(c, payload), "evidence_quotes": [c['proposal']['evidence'][0]['quote']]}
                for c in payload["candidates"]]}
        elif schema == "CoverageAssessment":
            self.round += 1
            directory, _ = self.cli.saved_session()
            source = directory / "documents/S1/text.txt"
            if self.seed_bytes is None:
                self.seed_bytes = source.read_bytes()
            else:
                self.assertEqual(source.read_bytes(), self.seed_bytes)
            last = self.round > 1
            answer = {"assessments": [{"criterion_id": "K1", "judgment": "satisfied" if last else "partial",
                "items": [{"key": "Alice", "candidate_ids": ["S1_P1_C1"]}],
                "target_count": 1 if last else None,
                "benchmark_candidate_ids": ["S2_P1_C1"] if last else [],
                "evidence_candidate_ids": ["S1_P1_C1"],
                "gaps": [] if last else ["Authoritative roster total remains unknown."], "rationale": "Fixture assessment only."}],
                "next_searches": [] if last else [{"query": "official current roster total", "goal_ids": ["G1"], "reason": "Establish denominator."}]}
        else:
            raise AssertionError(schema)
        return self.cli.response(text=json.dumps(answer))

    def run_pipeline(self, *extra, search=None, plan_only=False, budget=12, include_urls=False):
        """Keep the application's graph/selection/extraction/coverage code intact.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            search: Optional search-provider substitute; None uses this harness's fixed
                provider-shaped leads.
            plan_only: Whether this run stops after planning instead of entering review and
                retrieval.
            budget: Maximum model requests allowed for the fixture run, shared by planning and
                subsequent model-backed nodes.
            include_urls: Whether to supply the harness's URL-list fixture in addition to any
                local-document arguments.
            *extra: Additional command-line argument strings appended to this helper's base
                invocation.

        Returns:
            A tuple of terminal interface exit code, captured stdout, captured stderr, run-directory Path and
            saved-session dictionary.
        """
        args = ["--search", "--max-requests", str(budget), "--max-output-tokens", "4096"]
        if not plan_only:
            args.append("--extract")
            if self.review:
                args.append("--review-claims")
        with patch("projectclarity.nodes.web_search.node.search_web", side_effect=search or self.search), patch(
                "projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", side_effect=self.http.pool):
            result = self.cli.run_cli(self.provider, *args, *extra, plan_only=plan_only,
                include_urls=include_urls, source_dns=public_dns, details=False)
        directory, session = self.cli.saved_session()
        return (*result, directory, session)

    def test_trailing_nbsp_official_directory_reaches_selection_and_retrieval(self):
        """Replay the observed Commons URL defect through the real research entry point.

        Args:
            self: Production graph harness with synthetic search, provider and HTTP transports.
        Returns:
            None. Assert original URL provenance survives, normalized duplicates merge,
            the directory is offered/selected/retrieved and its text is extracted.
            No real Commons or paid model requests occur; this tests routing, not facts.
        """
        target = 'https://www.ourcommons.ca/Members'
        self.http.routes[('www.ourcommons.ca', '/robots.txt')] = (
            200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')
        self.http.routes[('www.ourcommons.ca', '/Members')] = (
            200, {'Content-Type': 'text/plain'}, SOURCE_TEXT.encode())

        def padded_search(query, max_results, monitor=None):
            """Substitute the failed provider URL while preserving ordinary discovery rounds.

            Args:
                query: Production query recorded by the existing search fixture.
                max_results: Requested provider result limit.
                monitor: Production observer forwarded unchanged.
            Returns:
                Search results with the observed NBSP defect and a clean duplicate
                on the first round; later rounds use the original fixture.
            """
            rows = self.search(query, max_results, monitor)
            if len(self.search_calls) == 1:
                rows[0]['url'] = target + '\u00a0'
                rows[1]['url'] = target
            return rows

        code, _, err, directory, session = self.run_pipeline(search=padded_search)
        self.assertEqual(code, 0, err)
        result = session['search']['results'][0]
        self.assertEqual(result['original_url'], target + '\u00a0')
        self.assertEqual(result['url'], target)
        self.assertTrue(result['eligible'])
        self.assertIn(result['result_id'], session['search']['selections'][0]['offered_ids'])
        self.assertEqual(session['sources'][0]['url'], target)
        self.assertEqual(session['sources'][0]['discovered_from_search'], result['result_id'])
        document = session['documents'][0]
        self.assertEqual(document['status'], 'retrieved')
        self.assertEqual(document['final_url'], target)
        self.assertEqual((directory / document['text_path']).read_text().strip(), SOURCE_TEXT.strip())
        self.assertTrue(any(c['source_id'] == document['source_id']
                            and c['evidence_status'] == 'anchored' for c in session['extraction']['candidates']))

    def test_question_only_two_rounds_search_extract_and_measure(self):
        """The real terminal entry point discovers pages, then a benchmark, without supplied URLs or special
        politics rules.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Discovery retains the proposed total but unreviewed benchmark
            evidence cannot establish it; no extra review calls are enabled.
        """
        code, out, err, directory, session = self.run_pipeline()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.search_calls), 2)
        self.assertEqual(len(self.cli.requests), 7)
        self.assertEqual(session["visited_nodes"].count("web_search"), 2)
        self.assertEqual(session["visited_nodes"].count("assess_research_gaps"), 2)
        self.assertEqual(session["coverage"]["history"][0]["criteria"][0]["target_count"], None)
        progress = session["coverage"]["criteria_progress"][0]
        self.assertEqual((progress["observed_count"], progress["target_count"]), (1, 1))
        self.assertEqual(progress["status"], "partial")
        self.assertEqual(progress['population']['state'], 'unverified')
        self.assertEqual(progress['population']['unsupported_candidate_ids'], ['S2_P1_C1'])
        self.assertIn('population_benchmark_not_supported', progress['blocking_reasons'])
        self.assertIn("model grouped", out.replace("-", " "))
        self.assertEqual(session["sources"][1]["discovered_from_search"], "R4")
        self.assertIn('Search Q1 · duckduckgo:', err)
        for query in self.search_calls:
            self.assertIn(query, err)
        self.assertIn('results returned (unverified leads)', err)
        self.assertIn('Search queries: 2 (no separate query cap)', err)
        self.assertIn('search-selected sources: 2/250 (248 left)', err)
        events = (directory / 'events.jsonl').read_text()
        for query in self.search_calls:
            self.assertNotIn(query, events)
        self.assertEqual(session["extraction"]["candidates"][1]["proposal"]["research_roles"], ["coverage_evidence"])
        self.assertNotIn("edge absent", (directory / "workflow_graph.txt").read_text())
        self.assertEqual(json.loads((directory / "coverage.json").read_text()), session["coverage"])

    def test_search_also_traverses_optional_claim_review(self):
        """New discovery is upstream of the existing semantic-review pipeline, not an alternate
        pipeline.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Explicit review permits the benchmark-supported model judgment;
            this positive control is distinct from the unreviewed partial outcome.
        """
        self.review = True
        code, _, err, _, session = self.run_pipeline()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.cli.requests), 9)
        self.assertEqual({c["adjudication_status"] for c in session["extraction"]["candidates"]}, {"supported"})
        progress = session['coverage']['criteria_progress'][0]
        self.assertEqual(progress['status'], 'model_assessed_satisfied')
        self.assertEqual(progress['population']['state'], 'model_asserted_supported_benchmark')

    def test_plan_only_does_not_search(self):
        """Permission to search is not execution during plan-only mode.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, err, _, session = self.run_pipeline(plan_only=True)
        self.assertEqual(code, 0, err)
        self.assertFalse(self.search_calls)
        self.assertEqual(session["coverage"]["assessment_status"], "not_assessed")

    def test_missing_new_criteria_fail_before_search(self):
        """The prompt request is now an enforced output contract, not an optional prose suggestion.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        del self.cli.plan["completion_criteria"]
        code, _, _, _, session = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertFalse(self.search_calls)
        self.assertEqual(session["status"], "failed")

    def test_search_source_limit_is_cumulative(self):
        """A partial assessment cannot reset the selected-source allowance for the next round.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, err, _, session = self.run_pipeline("--max-search-sources", "1")
        self.assertEqual(code, 1, err)
        self.assertEqual(session["workflow"]["stop_reason"], "budget_exhausted")
        self.assertEqual(len(self.search_calls), 1)
        self.assertEqual(session["search"]["stop_reason"], "search_source_limit")
        self.assertEqual(session["coverage"]["criteria_progress"][0]["status"], "partial")

    def test_cancellation_after_search_does_not_fetch_results(self):
        """The real cancel marker stops between the search provider and source selection.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        def cancel(query, limit, monitor=None):
            """Produce a lead while requesting cancellation during the in-flight query.

            Args:
                query: Search-query text to send to the configured web-search provider.
                limit: Requested per-query result cap passed through to the fixture search method.
                monitor: Optional RunMonitor for progress events and cooperative cancellation; None
                    disables those hooks.

            Returns:
                Fixture search-result dictionaries after writing the real cancellation marker.
            """
            directory, _ = self.cli.saved_session()
            (directory / "cancel.request").write_text("stop")
            return self.search(query, limit, monitor)
        code, _, _, _, session = self.run_pipeline(search=cancel)
        self.assertEqual(code, 130)
        self.assertEqual(session["status"], "cancelled")
        self.assertFalse(self.http.requests)

    def test_supplied_source_coverage_without_search_permission(self):
        """The new coverage contract is useful independently and cannot silently authorize web
        search.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.cli.plan["strategy"][0]["source_ids"] = ["S1"]
        self.cli.urls.write_text("https://example.org/person\n")
        with patch("projectclarity.nodes.web_search.node.search_web", side_effect=AssertionError("Search was not authorized")), patch(
                "projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", side_effect=self.http.pool):
            code, _, err = self.cli.run_cli(self.provider, "--extract", "--assess-coverage", "--max-requests", "8",
                plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 0, err)
        _, session = self.cli.saved_session()
        self.assertFalse(session["search"])
        self.assertEqual(session["coverage"]["criteria_progress"][0]["status"], "partial")

    def test_query_cap_preserves_unknown_target(self):
        """A bounded stop does not convert an unknown denominator into completed coverage.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, err, _, session = self.run_pipeline("--max-search-queries", "1")
        self.assertEqual(code, 1, err)
        self.assertEqual(session["workflow"]["stop_reason"], "budget_exhausted")
        self.assertEqual(len(self.search_calls), 1)
        self.assertEqual(session["coverage"]["criteria_progress"][0]["status"], "partial")
        self.assertIn("unknown", out)

    def test_request_limit_cannot_claim_coverage(self):
        """Do not buy a selection without enough calls for reading and assessment.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. A three-call question-only run cannot afford planning, selection,
            extraction and coverage. Assert it stops after planning with explicit
            limits, no fabricated findings and no assessed-completion claim.
        """
        code, _, _, _, session = self.run_pipeline(budget=3)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(session['sources'], [])
        self.assertEqual(session["coverage"]["assessment_status"], "not_assessed")
        self.assertIn("request_limit", session["coverage"]["limits_reached"])

    def test_search_failure_is_not_empty_success(self):
        """Record provider failure distinctly; do not invent or silently substitute sources.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        def failure(*args):
            """Simulate the adapter's application-owned failure code.

            Args:
                *args: Search-adapter arguments accepted but unused because this fixture always
                    raises.

            Returns:
                Never returns normally; raise SearchProviderError with the deliberate safe fixture
                failure code.
            """
            raise SearchProviderError("search_timeout", {'type': 'TimeoutException', 'message': 'PRIVATE_PROVIDER_DETAIL'})
        code, _, err, directory, session = self.run_pipeline(search=failure)
        self.assertEqual(code, 1)
        self.assertEqual(session["search"]["queries"][0]["error_code"], "search_timeout")
        self.assertIn('duckduckgo failed: search_timeout', err)
        self.assertIn('TimeoutException: PRIVATE_PROVIDER_DETAIL', err)
        self.assertNotIn('PRIVATE_PROVIDER_DETAIL', (directory / 'events.jsonl').read_text())
        self.assertFalse(self.http.requests)

    def test_invented_selection_never_becomes_source(self):
        """Observed search-result IDs, not model URLs, authorize source registration.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.overrides["SearchSourceSelection"] = lambda body: self.cli.response(text=json.dumps({
            "selections": [{"result_id": "R999", "goal_ids": ["G1"], "reason": "Unobserved."}]}))
        code, _, _, _, session = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertFalse(session["sources"])
        self.assertFalse(self.http.requests)

    def test_coverage_checks_evidence_again_after_model_returns(self):
        """A stale source cannot back a successful coverage assessment.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        def mutate(body):
            """Change only a temporary fixture after the model received its evidence.

            Args:
                body: Decoded provider-request JSON passed by the fixture router; contains the
                    actual serialized task payload.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            """
            directory, _ = self.cli.saved_session()
            (directory / "documents/S1/text.txt").write_text("Changed source.")
            return self.cli.response(text=json.dumps({"assessments": [{"criterion_id": "K1", "judgment": "partial",
                "items": [{"key": "Alice", "candidate_ids": ["S1_P1_C1"]}], "target_count": None,
                "benchmark_candidate_ids": [], "evidence_candidate_ids": [], "gaps": ["Total unknown."], "rationale": "Old evidence."}],
                "next_searches": []}))
        self.overrides["CoverageAssessment"] = mutate
        code, _, _, _, session = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertEqual(session["coverage"]["history"][0]["error_code"], "evidence_changed_during_assessment")
