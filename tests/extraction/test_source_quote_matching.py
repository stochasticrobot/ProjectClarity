"""Check layout-tolerant anchoring without accepting rewritten or ambiguous evidence.

These are small invented strings, not model-quality examples. Both validators
use this matcher; production-CLI tests separately prove it is wired into them.
"""
import unittest
from projectclarity.extraction.source_quote_matching import match_source_quote, QuoteMatchError


class SourceQuoteMatchingTests(unittest.TestCase):
    """Require original Unicode character offsets and unchanged non-whitespace text."""

    def test_layout_whitespace_maps_back_to_unchanged_source(self):
        """Newlines, tabs and nonbreaking spaces collapse only in the matching view."""
        source = "Préface. Élodie\nworks\t at\u00a0Lab X. Fin."
        found = match_source_quote(source, "Élodie works at Lab X.")
        self.assertEqual(found.start, len("Préface. "))
        self.assertEqual(source[found.start:found.end], "Élodie\nworks\t at\u00a0Lab X.")
        self.assertEqual(found.quote, source[found.start:found.end])
        self.assertEqual(found.match_method, "whitespace_normalized")

    def test_exact_match_remains_exact(self):
        """Already-exact quotes keep their original location and explicit match label."""
        found = match_source_quote("Alice works here.", "Alice works here.")
        self.assertEqual((found.start, found.end, found.match_method), (0, 17, "exact"))

    def test_changed_words_punctuation_case_and_word_boundaries_are_not_repaired(self):
        """Whitespace tolerance must not become fuzzy matching or erase a negation."""
        source = "Alice does not work at Lab-X."
        for quote in ["Alice does work at Lab-X.", "Alice does not work at Lab X.",
                      "alice does not work at Lab-X.", "Alicedoes not work at Lab-X.", "   "]:
            with self.subTest(quote=quote), self.assertRaisesRegex(QuoteMatchError, "quote_not_found"):
                match_source_quote(source, quote)

    def test_layout_variants_are_ambiguous_even_if_one_is_exact(self):
        """An exact occurrence cannot hide another equally plausible normalized location."""
        with self.assertRaisesRegex(QuoteMatchError, "ambiguous_quote"):
            match_source_quote("Alice works here. Alice\nworks here.", "Alice works here.")

    def test_overlapping_occurrences_are_ambiguous(self):
        """Count overlapping locations as well as separated repetitions."""
        with self.assertRaisesRegex(QuoteMatchError, "ambiguous_quote"):
            match_source_quote("aaa", "aa")
