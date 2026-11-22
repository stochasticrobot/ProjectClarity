"""Keep quote layout adaptation separate from factual or passage-ID guessing."""
import unittest
from projectclarity.claim_review.citation_layout import source_quote


class CitationLayoutTests(unittest.TestCase):
    """Restore only unambiguous whitespace changes from supplied source text."""

    def test_unique_layout_restores_exact_source_span(self):
        """Args: None. Returns: None; original source characters and audit survive."""
        source='Intro. We build digital\nsolutions. End.'
        exact,audit=source_quote('We build digital solutions.',source)
        self.assertEqual(exact,'We build digital\nsolutions.')
        self.assertEqual(source[audit['source_start']:audit['source_end']],exact)
        self.assertEqual(audit['original'],'We build digital solutions.')
        self.assertEqual(source_quote(exact,source),(exact,None))

    def test_changed_words_punctuation_wrong_passage_and_ambiguity_fail(self):
        """Args: None. Returns: None; no broad fuzzy or cross-passage matching exists."""
        for quote,source in [('We build AI.','They build AI.'),('We build AI','We build A.I.'),
                             ('We build AI.','Another passage.'),('We build AI.','We\nbuild AI. We\tbuild AI.'),
                             ('  ','Text')]:
            with self.subTest(quote=quote,source=source),self.assertRaises(ValueError):
                source_quote(quote,source)
