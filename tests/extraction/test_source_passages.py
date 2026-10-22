"""Verify that extraction sees bounded pieces of the exact retrieved text version.

Temporary files stand in for the acquisition output. The reader must not use an
edited file under an old fingerprint or follow a saved path outside the run's
document area. Passage coverage checks make skipped tails detectable.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from projectclarity.extraction.saved_source_passages import EvidenceInputError, iter_passages, read_verified_text


class SourcePassageTests(unittest.TestCase):
    """Exercise real file reads and exact text coordinates without network access."""

    def test_passages_cover_all_characters_once_with_stable_bounded_offsets(self):
        """Long prose and Unicode cannot disappear at chunk boundaries or exceed the cap."""
        text = "é" * 15000 + "\n" + "Paragraph.\n" * 1700
        passages = list(iter_passages(text))
        self.assertEqual("".join(text[start:end] for _, start, end in passages), text)
        self.assertTrue(all(0 < end - start <= 12000 for _, start, end in passages))
        self.assertEqual([item[0] for item in passages], [f"P{i}" for i in range(1, len(passages) + 1)])

    def test_modified_text_and_escaping_paths_are_rejected(self):
        """An existing file is not evidence unless it matches the registered saved version."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            path = root / "documents/text.txt"
            path.write_text("original")
            doc = SimpleNamespace(text_path="documents/text.txt", text_sha256=hashlib.sha256(b"original").hexdigest())
            self.assertEqual(read_verified_text(root, doc), "original")
            path.write_text("changed")
            with self.assertRaisesRegex(EvidenceInputError, "source_version_mismatch"):
                read_verified_text(root, doc)
            doc.text_path = "../outside.txt"
            with self.assertRaisesRegex(EvidenceInputError, "invalid_text_path"):
                read_verified_text(root, doc)

    def test_oversized_saved_text_is_refused_before_decoding_or_model_use(self):
        """A bounded read prevents a replaced file from consuming unlimited memory."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "documents/text.txt").write_bytes(b"12345")
            doc = SimpleNamespace(text_path="documents/text.txt", text_sha256="unused")
            with patch("projectclarity.extraction.saved_source_passages.MAX_SAVED_TEXT_BYTES", 4):
                with self.assertRaisesRegex(EvidenceInputError, "saved_text_too_large"):
                    read_verified_text(root, doc)
