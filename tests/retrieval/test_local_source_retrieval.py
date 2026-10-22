"""Check local reading and source receipts separately from the graph and parser.

For example, a file that disappears after planning should leave a failed receipt,
while an unreadable format should retain its raw bytes for inspection. Temporary
files keep developer documents untouched. Parser results are substituted in the
receipt tests to isolate acquisition/storage failures; real parsing is covered
in test_document_text_extraction.py and the CLI integration tests.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from projectclarity.research_schemas import FileSource
from projectclarity.retrieval.public_url_fetcher import PublicWebReader
from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError, RetrievalLimits
from projectclarity.retrieval.source_retrieval import read_local, retrieve_one


class LocalSourceRetrievalTests(unittest.TestCase):
    """Failures identify a source/read/parse boundary instead of looking like empty success."""

    def setUp(self):
        """Create a tiny local source and fresh shared counters for deterministic read-failure tests."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "notes.txt"
        self.path.write_bytes(b"abc")
        self.source = FileSource(source_id="S1", path=str(self.path))
        self.web = PublicWebReader(RetrievalBudget())

    def test_local_size_boundary_and_shared_budget_are_enforced(self):
        """An exact-size file is allowed, but per-file and whole-run excesses are not."""
        budget = RetrievalBudget(RetrievalLimits(max_source_bytes=3, max_total_bytes=3))
        self.assertEqual(read_local(self.path, budget), b"abc")
        self.assertEqual(budget.bytes_read, 3)
        for limits, expected in [
            (RetrievalLimits(max_source_bytes=2), "source_size_limit"),
            (RetrievalLimits(max_total_bytes=2), "total_size_limit"),
        ]:
            with self.subTest(expected=expected):
                with self.assertRaises(RetrievalError) as caught:
                    read_local(self.path, RetrievalBudget(limits))
                self.assertEqual(caught.exception.code, expected)

    def test_replaced_fifo_is_rejected_without_waiting_for_a_writer(self):
        """A special file substituted after planning must not hang a research worker."""
        fifo = self.root / "substituted-document"
        os.mkfifo(fifo)
        with self.assertRaises(RetrievalError) as caught:
            read_local(fifo, self.web.budget)
        self.assertEqual(caught.exception.code, "not_regular_file")

    def test_file_change_during_read_is_not_accepted_as_stable_evidence(self):
        """Changing metadata between the pre/post checks rejects the acquired bytes."""
        before = self.path.stat()
        after = SimpleNamespace(st_size=before.st_size, st_mtime_ns=before.st_mtime_ns + 1)
        # Keep the read real, but deterministically represent a simultaneous edit.
        with patch("projectclarity.retrieval.source_retrieval.os.fstat", side_effect=[before, after]):
            with self.assertRaises(RetrievalError) as caught:
                read_local(self.path, self.web.budget)
        self.assertEqual(caught.exception.code, "document_changed")

    def test_missing_local_file_leaves_a_failed_receipt_without_parsing(self):
        """A vanished input is recorded as file I/O failure, not successful empty text."""
        self.source.path = str(self.root / "missing.txt")
        with patch("projectclarity.retrieval.source_retrieval.parse_document") as parser:
            receipt = retrieve_one(self.source, self.root / "run", self.web)
        self.assertEqual((receipt.status, receipt.error_code), ("failed", "file_io"))
        self.assertIsNone(receipt.raw_path)
        parser.assert_not_called()
        saved = json.loads((self.root / "run/documents/S1/metadata.json").read_text())
        self.assertEqual(saved, receipt.model_dump())

    def test_parse_failure_retains_raw_source_but_does_not_publish_text(self):
        """An unsupported document still has inspectable acquisition evidence."""
        with patch("projectclarity.retrieval.source_retrieval.parse_document",
                   side_effect=RetrievalError("unsupported_format", "Unsupported fixture format.")):
            receipt = retrieve_one(self.source, self.root / "run", self.web)
        self.assertEqual((receipt.status, receipt.error_code), ("failed", "unsupported_format"))
        self.assertEqual((self.root / "run" / receipt.raw_path).read_bytes(), b"abc")
        self.assertEqual(receipt.raw_sha256, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertIsNotNone(receipt.acquired_at)
        self.assertIsNone(receipt.text_path)
        self.assertFalse((self.root / "run/documents/S1/text.txt").exists())

    def test_unexpected_parser_exception_payload_is_not_saved(self):
        """Third-party errors can identify their type without leaking document contents."""
        with patch("projectclarity.retrieval.source_retrieval.parse_document",
                   side_effect=RuntimeError("PRIVATE_DOCUMENT_PAYLOAD")):
            receipt = retrieve_one(self.source, self.root / "run", self.web)
        self.assertEqual(receipt.error_code, "retrieval_error")
        saved = (self.root / "run/documents/S1/metadata.json").read_text()
        self.assertIn("RuntimeError", saved)
        self.assertNotIn("PRIVATE_DOCUMENT_PAYLOAD", saved)
