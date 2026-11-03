"""Check shortlist policy edge cases independently of model judgment.

The integration test covers thousands of URLs through the real workflow. These
small examples isolate duplicate ownership, conservative exclusions and quotas.
"""
import unittest
from projectclarity.link_ranking.link_shortlisting import shortlist_links, exclusion_reason


def occurrence(identifier, url, source, score, title='Useful title'):
    """Create one saved-link input without source files or network calls.

    Args:
        identifier: Stable occurrence ID.
        url: Normalized destination.
        source: Final source-page URL, also used as its document ID in this fixture.
        score: Source contribution points.
        title: Visible anchor text; empty text exercises missing-title handling.
    Returns: Minimal complete occurrence record accepted by the shortlister.
    """
    return {'id':identifier,'url':url,'source_document_id':source,'source_url':source,
            'source_title':source,'document_score':score,'link_title':title}


class LinkShortlistingTests(unittest.TestCase):
    """Verify policy limits without treating excluded content as semantically irrelevant."""

    def test_duplicate_ownership_and_processed_redirect_aliases(self):
        """Keep all associations but use the strongest source and omit read URLs.

        Args: None.
        Returns: None; confirms dedup before scoring and original/final exclusion.
        """
        rows=[occurrence('L1','https://x.test/a','https://source.test/a',10),
              occurrence('L2','https://x.test/a','https://source.test/b',30),
              occurrence('L3','https://x.test/old','https://source.test/a',10),
              occurrence('L4','https://x.test/final','https://source.test/a',10)]
        result,counts=shortlist_links(rows,[{'downloaded':True,'url':'https://x.test/old#fragment','final_url':'https://x.test/final'}])
        self.assertEqual(counts['duplicates_combined'],1)
        self.assertEqual(counts['excluded'],2)
        chosen=next(row for row in result if row['url']=='https://x.test/a')
        self.assertEqual(chosen['document_score'],30)
        self.assertEqual(len(chosen['source_associations']),2)
        self.assertEqual(chosen['source_url'],'https://source.test/b')
        self.assertNotIn('status',rows[0])

    def test_conservative_exclusions_preserve_potential_research_content(self):
        """Exclude exact utility paths/assets, not useful page categories by guesswork.

        Args: None.
        Returns: None; PDFs, About, Contact and research about privacy remain eligible.
        """
        for path in ('/login','/privacy-policy.html','/terms/','/logo.PNG','/app.js'):
            self.assertTrue(exclusion_reason('https://x.test'+path,set()),path)
        for path in ('/about','/contact','/paper.pdf','/privacy-study','/docs/data.csv'):
            self.assertFalse(exclusion_reason('https://x.test'+path,set()),path)

    def test_one_source_is_limited_and_untitled_shortlist_is_not_sent(self):
        """Apply the per-page cap even when the total limit has not been reached.

        Args: None.
        Returns: None; fifty shortlisted, ten explicitly unassessed, zero invented
            title scores on unassessed links and a zero bonus for missing titles.
        """
        rows=[occurrence(f'L{i}',f'https://x.test/{i:03d}','https://source.test/a',0,'') for i in range(60)]
        result,counts=shortlist_links(rows,[])
        self.assertEqual(counts['shortlisted'],50)
        self.assertEqual(counts['not_assessed'],10)
        self.assertTrue(all(row['title_score']==0 for row in result if row['shortlisted']))
        self.assertTrue(all(row['rating'] is None for row in result if not row['shortlisted']))
        self.assertEqual(shortlist_links([],[])[1]['shortlisted'],0)
