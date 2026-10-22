"""Unit tests for deterministic request, source-list, and session initialization.

Purpose:
    Check what happens before the model is called: validate the question and
    source references, assign source IDs, and save the starting details.
    For example, listing the same URL twice should create one source, while
    a missing document should stop the run before a request can cost credits.
Responsibilities:
    Preserve original question/budget values, deduplicate URL/file references,
    enforce input size/syntax rules, and inspect the initial saved session. Temporary
    directories prevent test data from touching real research runs.
Typical use:
    Run ``python -m unittest tests.research.test_research_initialization -v`` or discover
    all tests. These tests call initialization helpers directly, without a graph.
Boundary:
    Reading the URL list is not downloading its URLs. No LLM or network is used,
    and the tests do not establish source reachability or research quality.
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from projectclarity.research_schemas import Budget, FileSource, ResearchRequest, ResearchSession, Source, URLSource
from projectclarity.research_planner import ResearchError
from projectclarity.agentic_workflow import initialize_research
from projectclarity.source_reference_loader import MAX_SOURCE_FILE_BYTES, load_sources


class InitializationTests(unittest.TestCase):
    """Use temporary files to inspect real local input and persistence behavior."""

    def setUp(self):
        """Allocate isolated source-list and output paths to inspect real initialization writes."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.urls = self.directory / "source_urls.txt"
        self.output = self.directory / "runs"

    def test_initialization_preserves_request_deduplicates_sources_and_saves_budgets(self):
        """Original wording and configured limits survive initialization unchanged."""
        # Comments and duplicate URLs should not create extra source records.
        self.urls.write_text("# fixtures\nhttps://example.org/a\n\nhttps://example.org/a\nhttps://example.org/b\n")
        request = ResearchRequest(
            question="  Who researches AI security in Canada?  ",
            urls_file=str(self.urls),
            budget=Budget(max_requests=1, max_output_tokens=1024),
        )
        session = initialize_research({"request": request}, self.output)["session"]
        saved = json.loads((self.output / session.session_id / "session.json").read_text())

        self.assertEqual(saved["request"]["question"], request.question)
        self.assertEqual(saved["request"]["budget"], request.budget.model_dump())
        self.assertEqual([source.source_id for source in session.sources], ["S1", "S2"])
        self.assertEqual(session.status, "initialized")
        self.assertEqual(session.usage.requests_attempted, 0)
        self.assertIsNone(session.plan)

    def test_invalid_url_fails_before_a_session_is_created(self):
        """Bad source input must fail locally, without leaving a research run."""
        for url in ["file:///etc/passwd", "not a URL", "https://user:secret@example.org/", "https://example.org:bad/"]:
            with self.subTest(url=url):
                self.urls.write_text(url)
                request = ResearchRequest(question="Find research", urls_file=str(self.urls))
                with self.assertRaisesRegex(ResearchError, "Invalid URL"):
                    initialize_research({"request": request}, self.output)
                self.assertFalse(self.output.exists())

    def test_source_file_and_url_count_are_bounded(self):
        """Limit prompt input before provider construction or invocation."""
        self.urls.write_text("x" * (MAX_SOURCE_FILE_BYTES + 1))
        with self.assertRaisesRegex(ResearchError, "32 KiB"):
            load_sources(str(self.urls))
        self.urls.write_text("\n".join(f"https://example.org/{index}" for index in range(26)))
        with self.assertRaisesRegex(ResearchError, "25 unique"):
            load_sources(str(self.urls))

    def test_missing_empty_or_non_utf8_source_file_is_rejected(self):
        """An omitted URL option is valid; a broken supplied file is an input error."""
        self.assertEqual(load_sources(None), [])
        with self.assertRaisesRegex(ResearchError, "Cannot read"):
            load_sources(str(self.urls))
        self.urls.write_text("# only a comment\n")
        with self.assertRaisesRegex(ResearchError, "has no URLs"):
            load_sources(str(self.urls))
        self.urls.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ResearchError, "UTF-8"):
            load_sources(str(self.urls))

    def test_blank_or_oversized_question_is_rejected(self):
        """Reject unusable objectives before spending a request budget."""
        for question in ["", "   ", "x" * 4001]:
            with self.subTest(length=len(question)):
                with self.assertRaises(ValidationError):
                    ResearchRequest(question=question)

    def test_mixed_sources_resolve_paths_and_deduplicate_aliases(self):
        """One file keeps one ID even when given as relative, absolute, or symlink paths."""
        document = self.directory / "report.pdf"
        document.write_bytes(b"Not a parsed PDF; only a source reference at this stage")
        alias = self.directory / "alias.pdf"
        alias.symlink_to(document)
        self.urls.write_text("https://example.org/research\n")
        references = ["report.pdf", str(document), str(alias)]
        request = ResearchRequest(question="Find research", urls_file=str(self.urls), document_paths=references)

        # Relative paths belong to the command's launch directory. Using the
        # actual working-directory context also catches accidental repo anchoring.
        with contextlib.chdir(self.directory):
            session = initialize_research({"request": request}, self.output)["session"]
        saved_path = self.output / session.session_id / "session.json"
        restored = ResearchSession.model_validate_json(saved_path.read_text())
        self.assertEqual(restored.schema_version, "1.7")
        self.assertEqual(restored.request.document_paths, references)
        self.assertEqual(restored.sources, [
            URLSource(source_id="S1", url="https://example.org/research"),
            FileSource(source_id="S2", path=str(document.resolve())),
        ])

    def test_bad_document_references_fail_before_session_creation(self):
        """Missing files, directories, remote URLs, and special files are not documents."""
        broken = self.directory / "broken.pdf"
        broken.symlink_to(self.directory / "missing.pdf")
        fifo = self.directory / "not-a-document"
        os.mkfifo(fifo)
        references = [str(self.directory / "missing.pdf"), str(self.directory), str(broken),
                      str(fifo), "https://example.org/report.pdf", "   ", "bad\x00path"]
        for reference in references:
            with self.subTest(reference=repr(reference)):
                request = ResearchRequest(question="Find research", document_paths=[reference])
                with self.assertRaisesRegex(ResearchError, "Document 1 must be"):
                    initialize_research({"request": request}, self.output)
                self.assertFalse(self.output.exists())

    def test_inaccessible_document_is_rejected_without_reading_it(self):
        """A failed permission check stops registration, independent of test-user privileges."""
        document = self.directory / "report.pdf"
        document.touch()
        # A mocked permission result works even if tests run as an elevated user
        # that could otherwise read a chmod(000) file. It is not a content read.
        with patch("projectclarity.source_reference_loader.os.access", return_value=False):
            with self.assertRaisesRegex(ResearchError, "readable local file"):
                load_sources(None, [str(document)])

    def test_source_limit_counts_urls_and_documents_together(self):
        """Adding a document cannot bypass the total of 25 unique source references."""
        document = self.directory / "report.pdf"
        document.touch()
        self.urls.write_text("\n".join(f"https://example.org/{index}" for index in range(24)))
        sources = load_sources(str(self.urls), [str(document), str(document)])
        self.assertEqual(len(sources), 25)  # A duplicate at the boundary remains valid.
        other = self.directory / "other.pdf"
        other.touch()
        with self.assertRaisesRegex(ResearchError, "25 unique supplied sources"):
            load_sources(str(self.urls), [str(document), str(other)])

    def test_document_arguments_have_structural_input_bounds(self):
        """Reject empty, oversized, non-string, or excessive document arguments early."""
        for references in [[""], ["x" * 4097], [123], ["report.pdf"] * 26]:
            with self.subTest(references=repr(references)[:100]):
                with self.assertRaises(ValidationError):
                    ResearchRequest(question="Find research", document_paths=references)


class SourceContractTests(unittest.TestCase):
    """Verify that the source kind selects one unambiguous location-field shape."""

    def test_explicit_kind_selects_url_or_file_and_round_trips_json(self):
        """Stored source records retain their type and ID when decoded again."""
        adapter = TypeAdapter(Source)
        for record, expected_type in [
            ({"source_id": "S1", "kind": "url", "url": "https://example.org/"}, URLSource),
            ({"source_id": "S2", "kind": "file", "path": "/local/report.pdf"}, FileSource),
        ]:
            with self.subTest(kind=record["kind"]):
                source = adapter.validate_python(record)
                self.assertIsInstance(source, expected_type)
                expected = {**record, **({"depth": 0, "discovered_from_link": None} if record["kind"] == "url" else {})}
                self.assertEqual(adapter.validate_json(source.model_dump_json()).model_dump(), expected)

    def test_ambiguous_or_incomplete_source_records_are_rejected(self):
        """A discriminator is useful only if bad kinds and mixed location fields fail."""
        adapter = TypeAdapter(Source)
        records = [
            {"source_id": "S1", "url": "https://example.org/"},
            {"source_id": "S1", "kind": "unknown", "url": "https://example.org/"},
            {"source_id": "S1", "kind": "file", "url": "https://example.org/"},
            {"source_id": "S1", "kind": "url", "path": "/report.pdf"},
            {"source_id": "S1", "kind": "file", "path": "/report.pdf", "url": "https://example.org/"},
            {"source_id": "S1", "kind": "file", "path": ""},
            {"source_id": "", "kind": "file", "path": "/report.pdf"},
        ]
        for record in records:
            with self.subTest(record=record):
                with self.assertRaises(ValidationError):
                    adapter.validate_python(record)
