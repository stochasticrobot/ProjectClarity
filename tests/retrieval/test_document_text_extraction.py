"""Check the text a reader actually gets from supported document formats.

These tests start the real bounded parser subprocess. For example, an HTML page
must retain its article but not its script, and an image-only PDF must fail rather
than invent text. A separate timeout check substitutes the subprocess result so
the suite does not have to wait for a deliberately slow parser.
"""

import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from projectclarity.retrieval.retrieval_limits import RetrievalError, RetrievalLimits, RunCancelled
from projectclarity.retrieval.document_text_extractor import parse_document
from tests.retrieval.retrieval_test_fixtures import text_pdf


class ParsingTests(unittest.TestCase):
    """Use actual bytes and parser libraries, not canned parser return values."""

    def test_text_normalizes_line_endings_without_dropping_words(self):
        """Text remains readable and line-based after Windows newlines and extra spaces."""
        text, parser = parse_document(b"  First   line\r\nSecond\tline\r\n", "text/plain", RetrievalLimits())
        self.assertEqual(text, "First line\nSecond line\n")
        self.assertIn("normalize-v1", parser)

    def test_html_keeps_main_text_and_removes_scripts_and_navigation(self):
        """Article text survives; script/nav markup must not appear as article evidence."""
        raw = b"<html><nav>Menu junk</nav><main><h1>Research</h1><p>Useful source text.</p></main><script>run_bad_action()</script></html>"
        text, parser = parse_document(raw, "text/html", RetrievalLimits())
        self.assertIn("Useful source text.", text)
        self.assertNotIn("Menu junk", text)
        self.assertNotIn("run_bad_action", text)
        self.assertIn("beautifulsoup4-", parser)

    def test_pdf_text_is_extracted_from_a_real_pdf(self):
        """The PDF path exercises pypdf, not a fixture pretending to be parsed output."""
        text, parser = parse_document(text_pdf(), "application/pdf", RetrievalLimits())
        self.assertIn("Canadian AI security", text)
        self.assertIn("pypdf-", parser)

    def test_empty_encrypted_malformed_and_oversized_outputs_fail_explicitly(self):
        """Unusable or over-limit material cannot silently become successful empty text."""
        cases = [(b"  \n", "text/plain", RetrievalLimits(), "no_text"),
                 (text_pdf(""), "application/pdf", RetrievalLimits(), "no_text"),
                 (text_pdf(encrypted=True), "application/pdf", RetrievalLimits(), "encrypted_pdf"),
                 (b"%PDF-not-valid", "application/pdf", RetrievalLimits(), "parse_error"),
                 (text_pdf(pages=2), "application/pdf", RetrievalLimits(max_pdf_pages=1), "page_limit"),
                 (b"too much text", "text/plain", RetrievalLimits(max_text_characters=4), "text_size_limit"),
                 (b"abcd", "text/plain", RetrievalLimits(max_text_characters=4), "text_size_limit"),
                 (b"\xff", "text/plain", RetrievalLimits(), "parse_error"),
                 (b"binary", "application/zip", RetrievalLimits(), "unsupported_format")]
        for raw, mime, limits, expected in cases:
            with self.subTest(expected=expected, mime=mime):
                with self.assertRaises(RetrievalError) as caught:
                    parse_document(raw, mime, limits)
                self.assertEqual(caught.exception.code, expected)

    def test_parser_timeout_becomes_a_controlled_failure(self):
        """A stuck parser has a finite wait and cannot print document-bearing exceptions."""
        process = MagicMock()
        process.__enter__.return_value = process
        process.communicate.side_effect = [subprocess.TimeoutExpired("parser", 0.25), (b"", None)]
        # The clock crosses the deadline after one polling timeout. The second
        # communicate call reaps the killed child rather than restarting parsing.
        with (
            patch("projectclarity.retrieval.document_text_extractor.subprocess.Popen", return_value=process),
            patch("projectclarity.retrieval.document_text_extractor.time.monotonic", side_effect=[0, 0, 16]),
        ):
            with self.assertRaises(RetrievalError) as caught:
                parse_document(b"text", "text/plain", RetrievalLimits())
        self.assertEqual(caught.exception.code, "parser_timeout")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)

    def test_cancelling_a_busy_parser_kills_and_reaps_its_child_process(self):
        """A cooperative stop cannot leave an expensive parsing child running in the background."""
        real_popen = subprocess.Popen
        children = []

        def slow_child(command, **options):
            # Use a real OS child with a deterministic delay instead of relying
            # on a particular PDF being slow. The production supervisor is real.
            """Substitute a sleeping OS child to test the real parser supervisor's cancellation cleanup."""
            child = real_popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **options)
            children.append(child)
            return child

        control = MagicMock(side_effect=[None, RunCancelled("Stop fixture")])
        with patch("projectclarity.retrieval.document_text_extractor.subprocess.Popen", side_effect=slow_child):
            with self.assertRaises(RunCancelled):
                parse_document(b"text", "text/plain", RetrievalLimits(), check_control=control)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
