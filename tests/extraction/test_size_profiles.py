"""Verify experimental profiles through real CLI, parsing, SDK and saved receipts.

Only provider HTTP responses are synthetic. These checks prove packing and budget
routing, not live model capability, extraction accuracy or speed improvements.
"""
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from projectclarity.extraction.settings import ExtractionSettings
from projectclarity.extraction.saved_source_passages import iter_passages, RECORD_START
from projectclarity.nodes.extract_knowledge.node import extract_knowledge
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.research_schemas import ResearchRequest, ResearchSession, Budget
from tests.extraction import test_knowledge_extraction_integration as integration_tests


class SizeProfileTests(unittest.TestCase):
    """Require complete source accounting and task-local, persistent settings."""

    def fixture(self):
        """Create an isolated production CLI fixture with synthetic transport.

        Args:
            self: Test owner registering fixture cleanup.
        Returns:
            Fixture with a local source and no-clarification plan; no live calls.
        """
        case = integration_tests.KnowledgeExtractionIntegrationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_all_profiles_pack_structured_sources_through_cli(self):
        """Keep every row and exact saved offsets while changing actual call size.

        Args:
            self: Test owner supplying an unseen non-Parliament roster.
        Returns:
            None. Checks parser, graph, actual SDK request and persisted receipts;
            empty synthetic replies do not establish semantic extraction recall.
        """
        for selected, expected_calls, cap in [(None, 2, 16384), ('small', 4, 8192), ('medium', 2, 16384), ('large', 1, 32768)]:
            profile = selected or 'medium'
            with self.subTest(profile=selected):
                case = self.fixture()
                case.document = case.cli.directory / 'population.html'
                case.document.write_text('<table><tr><th>Name</th><th>Affiliation</th></tr>' +
                    ''.join(f'<tr><td>Researcher {i}</td><td>Institute {i}</td></tr>' for i in range(32)) + '</table>')
                flags = ('--extraction-profile', selected) if selected else ()
                code, _, err, directory, saved = case.run_extraction(*flags,
                    batch={'entities': [], 'claims': []}, budget=12)
                self.assertEqual(code, 0, err)
                wires = [json.loads(r.content) for r in case.cli.requests]
                extraction = [w for w in wires if w['output_config']['format']['schema']['title'] == 'ExtractionBatch']
                self.assertEqual(len(extraction), expected_calls + 1)  # Table heading is a separate prose passage.
                self.assertEqual(wires[0]['max_tokens'], 8192)
                self.assertEqual({w['max_tokens'] for w in extraction}, {cap})
                text = (directory / 'documents/S1/text.txt').read_text()
                received = [json.loads(w['messages'][0]['content'])['passage']['text'] for w in extraction]
                self.assertEqual(''.join(received), text)
                self.assertEqual(sum(len(RECORD_START.findall(t)) for t in received), 32)
                self.assertEqual(sum(bool(RECORD_START.search(t)) for t in received), expected_calls)
                attempts = saved['extraction']['attempts']
                self.assertEqual([(a['start'], a['end']) for a in attempts],
                    [(start, end) for _, start, end in iter_passages(text, ExtractionSettings.for_profile(profile))])
                sizing = saved['request']['extraction_settings']
                self.assertEqual(sizing['profile'], profile)
                self.assertEqual(saved['extraction']['sizing'], sizing)
                self.assertEqual(saved['usage']['requests_attempted'], 2 + expected_calls)
                for a in attempts:
                    trace = json.loads((directory / a['trace_path']).read_text())
                    receipt = json.loads((directory / 'input_receipts' / (a['batch_id'] + '.json')).read_text())
                    self.assertEqual(trace['extraction_settings'], sizing)
                    self.assertEqual(trace['max_output_tokens'], cap)
                    self.assertEqual(receipt['extraction_settings'], sizing)
                    self.assertFalse(receipt['errors'])

    def test_large_prose_crosses_old_message_cap_without_losing_text(self):
        """Exercise the larger serialized envelope, not just compact table batching.

        Args:
            self: Test owner creating prose long enough to exceed the old48k guard.
        Returns:
            None. Full production dispatch succeeds offline and preserves all text.
        """
        case = self.fixture()
        case.document.write_text('A' * 70000)
        code, _, err, directory, saved = case.run_extraction('--extraction-profile', 'large',
            batch={'entities': [], 'claims': []}, budget=8)
        self.assertEqual(code, 0, err)
        attempts = saved['extraction']['attempts']
        self.assertEqual(len(attempts), 2)
        trace = json.loads((directory / attempts[0]['trace_path']).read_text())
        receipt = json.loads((directory / 'input_receipts/S1_P1.json').read_text())
        self.assertGreater(receipt['message_characters'], 48000)
        self.assertEqual(receipt['limit_characters'], 128000)
        self.assertEqual(len(json.loads(trace['messages'][-1]['content'])['passage']['text']), 48000)
        self.assertEqual(sum(a['end'] - a['start'] for a in attempts),
            len((directory / 'documents/S1/text.txt').read_text()))

    def test_legacy_and_explicit_overrides_preserve_other_task_caps(self):
        """Preserve old request budgets and allow model-specific extraction overrides.

        Args:
            self: Test owner using real request validation and SDK serialization.
        Returns:
            None. No review/coverage/global cap is enlarged by extraction options.
        """
        request = ResearchRequest(question='Legacy', budget=Budget(max_output_tokens=512))
        self.assertEqual(request.extraction_settings.max_output_tokens, 512)
        restored = ResearchRequest.model_validate_json(request.model_dump_json())
        self.assertEqual(restored.extraction_settings, request.extraction_settings)
        case = self.fixture()
        code, _, err, _, saved = case.run_extraction('--max-output-tokens', '512',
            '--extraction-profile', 'medium', '--extraction-output-tokens', '4096')
        self.assertEqual(code, 0, err)
        self.assertEqual([json.loads(r.content)['max_tokens'] for r in case.cli.requests], [512, 4096])
        self.assertEqual(saved['request']['budget']['max_output_tokens'], 512)
        with self.assertRaises(ValidationError):
            restored.extraction_settings.max_output_tokens = 1024
        with self.assertRaises(ValidationError):
            ExtractionSettings.for_profile('large', max_input_characters=100)

    def test_envelope_failure_leaves_unsent_receipt(self):
        """Reject too-small whole-input allowances before any extraction dispatch.

        Args:
            self: Test owner retaining real planning and extraction preflight.
        Returns:
            None. Exactly one planning call is sent; extraction records its failure.
        """
        case = self.fixture()
        _, _, _, directory, saved = case.run_extraction('--extraction-record-characters', '1000',
            '--extraction-passage-characters', '1000', '--extraction-message-characters', '1000',
            '--extraction-input-characters', '1000')
        self.assertEqual(len(case.cli.requests), 1)
        receipt = json.loads((directory / 'input_receipts/S1_P1.json').read_text())
        self.assertIn('input_contract_too_large', receipt['errors'])
        self.assertEqual(saved['extraction']['attempts'][0]['status'], 'failed')
        self.assertFalse((directory / 'extraction_calls/S1_P1.json').exists())

    def test_restart_keeps_partition_and_rejects_profile_changes(self):
        """Retain completed work without resolving defaults again or silently repacking.

        Args:
            self: Test owner reading a real completed medium-profile run.
        Returns:
            None. Re-entry sends no duplicate calls, while mismatched settings fail
            before changing saved receipts or creating a provider client.
        """
        case = self.fixture()
        _, _, _, directory, saved = case.run_extraction('--extraction-profile', 'medium')
        session = ResearchSession.model_validate(saved)
        original = json.dumps(session.extraction['attempts'], sort_keys=True)
        with patch('projectclarity.nodes.extract_knowledge.node.create_extractor', side_effect=AssertionError('replayed')):
            extract_knowledge({'session': session}, directory.parent)
        self.assertEqual(json.dumps(session.extraction['attempts'], sort_keys=True), original)
        before = (directory / 'extraction.json').read_bytes()
        session.request.extraction_settings = ExtractionSettings.for_profile('large')
        with self.assertRaisesRegex(ResearchError, 'saved passage inventory'):
            extract_knowledge({'session': session}, directory.parent)
        self.assertEqual((directory / 'extraction.json').read_bytes(), before)

    def test_mixed_unicode_and_oversized_records_cover_every_character(self):
        """Exercise held-out boundary shapes without an LLM or text normalization.

        Args:
            self: Test owner constructing prose, table labels and an oversized row.
        Returns:
            None. Every profile covers each source character exactly once and
            keeps every interval within its declared hard passage ceiling.
        """
        text = 'préface🙂\n' * 1000 + '[Record T1R1]\n' + 'é🙂' * 25000 + '\n\n'
        text += ''.join(f'[Record C{i}]\nName {i} | not current\n\n' for i in range(71)) + 'After.'
        for profile in ('small', 'medium', 'large'):
            settings = ExtractionSettings.for_profile(profile)
            intervals = list(iter_passages(text, settings))
            self.assertEqual(''.join(text[a:b] for _, a, b in intervals), text)
            self.assertTrue(all(0 < b - a <= settings.max_passage_characters for _, a, b in intervals))

    def test_review_wire_limit_is_independent_of_large_extraction(self):
        """Send both tasks through the SDK and verify different actual allowances.

        Args:
            self: Test owner using the production review-and-extraction fixture.
        Returns:
            None. Review keeps its global cap and produces accepted saved reviews;
            this is routing evidence, not a live-model accuracy benchmark.
        """
        from tests.claim_review import test_review_and_discovery_integration as review_tests
        case = review_tests.ReviewDiscoveryIntegrationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        code, _, err, _, saved = case.run_research('--extraction-profile', 'large', follow=False)
        self.assertEqual(code, 0, err)
        caps = {}
        for request in case.cli.requests:
            wire = json.loads(request.content)
            caps[wire['output_config']['format']['schema']['title']] = wire['max_tokens']
        self.assertEqual(caps['ExtractionBatch'], 32768)
        self.assertEqual(caps['ClaimReviewBatch'], 8192)
        self.assertTrue(saved['claim_reviews'])

    def test_numeric_override_alone_does_not_raise_legacy_output_cap(self):
        """Keep unrelated saved output limits when only record packing is overridden.

        Args:
            self: Test owner supplying CLI numeric overrides without a preset.
        Returns:
            None. Actual planning and extraction wire limits both remain512.
        """
        case = self.fixture()
        code, _, err, _, saved = case.run_extraction('--max-output-tokens', '512', '--extraction-records', '4')
        self.assertEqual(code, 0, err)
        self.assertEqual([json.loads(r.content)['max_tokens'] for r in case.cli.requests], [512, 512])
        self.assertEqual(saved['request']['extraction_settings']['max_records_per_passage'], 4)
