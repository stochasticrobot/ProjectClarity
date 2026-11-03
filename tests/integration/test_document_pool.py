"""Check that document input does not depend on how the paths were discovered.

The tests supply paths directly, including ordinary file paths in the pool, and
exercise the document processor without search configuration. A separate check
runs search without downloading its results. Readers and AI replies are invented
for these offline checks; local-file reading is not enabled by these fixtures.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import json
import unittest
from unittest.mock import patch

from projectclarity.documents.document_pool import DocumentPool
from projectclarity.documents.document_pipeline import DocumentPipeline
from projectclarity.workflow.research_graph import build_document_graph
from projectclarity.search.search_execution import SearchExecution
from tests.integration.test_document_pipeline import ExtractCaller


class DocumentPoolTests(unittest.TestCase):
    """Protect the boundary between discovering paths and processing documents."""

    def test_pool_preserves_paths_without_interpreting_origins(self):
        """Accept different path formats and preserve notes without search assumptions.

        Args: None.
        Returns: None; asserts exact deduplication, order and copied origin notes.
        """
        note={'supplied_by':'user','details':['reading list']}
        pool=DocumentPool(['/documents/report.pdf',
            {'path':'https://example.org/report','provenance':note},
            {'path':'/documents/report.pdf','provenance':{'supplied_by':'catalogue'}}])
        note['details'].append('changed outside pool')
        self.assertEqual([e['path'] for e in pool.entries],
                         ['/documents/report.pdf','https://example.org/report'])
        self.assertEqual(pool.entries[1]['provenance'][0]['details'],['reading list'])
        self.assertEqual(pool.entries[0]['provenance'],[{'supplied_by':'catalogue'}])

    def test_document_processing_needs_no_search_terms_or_provider(self):
        """Run the real document graph with only directly supplied URLs and a reader.

        Args: None.
        Returns:
            None; proves one download per unique supplied path, full extraction,
            a saved input manifest and no search node or search service calls.
        """
        reads=[]

        class Reader:
            """Provide readable source bytes without contacting a website."""

            def fetch(self, path):
                """Record the supplied path and return its complete fixture text.

                Args: path: Exact document path requested by the real processor.
                Returns: Source address, headers, bytes and redirect history.
                """
                reads.append(path)
                return path,{'content-type':'text/plain'},b'Acme builds AI tools in Canada.',[]

        with TemporaryDirectory() as tmp, patch('projectclarity.search.web_search.search_web',side_effect=AssertionError('No search allowed')), patch('projectclarity.search.web_search.search_wikipedia',side_effect=AssertionError('No search allowed')):
            state={'session_id':'direct-input','research_question':'Which companies build AI?',
                   'information_needed':[]}
            caller=ExtractCaller()
            worker=DocumentPipeline(Path(tmp),state,lambda _:None,caller,Event(),
                paths=['https://example.org/manual','https://example.org/manual'],reader=Reader())
            self.assertNotIn('search',build_document_graph(worker).get_graph().nodes)
            worker.execute()
            self.assertEqual(reads,['https://example.org/manual'])
            self.assertEqual(state['run']['status'],'completed')
            self.assertEqual(state['run']['facts'],1)
            self.assertNotIn('searches',state['run'])
            self.assertNotIn('provider',state['run'])
            manifest=json.loads((worker.root/'manifest.json').read_text())
            self.assertEqual(manifest['paths'][0]['path'],'https://example.org/manual')
            self.assertNotIn('searches',manifest)
            self.assertEqual(len(caller.calls),1)
            with self.assertRaises(ValueError):worker.set_paths(['https://example.org/other'])

    def test_search_returns_paths_without_building_document_records(self):
        """Run the independent search step without constructing a document pipeline.

        Args: None.
        Returns:
            None; asserts candidate paths and search receipts, with no downloaded
            files or document-status records. Duplicate paths can be pooled later.
        """
        calls=[]

        def search(query, limit, cancelled):
            """Return the same candidate for two prepared queries without network I/O.

            Args:
                query: Prepared text received by search.
                limit: Requested result count.
                cancelled: Callback checked before supplying the fixture reply.
            Returns: A search result containing an address, title and short description.
            """
            cancelled();calls.append(query)
            return {'provider':'fixture','results':[{'url':'https://example.org/doc#part',
                    'title':'Document','snippet':'A lead, not downloaded evidence.'}]}

        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            state={'searches':[{'query':'first'},{'query':'second'}]}
            progress={'events':[],'limits':{}}
            worker=SearchExecution(root,state,progress,lambda _:None,Event(),search=search)
            paths=worker.execute()
            self.assertEqual(calls,['first','second'])
            self.assertEqual([p['path'] for p in paths],['https://example.org/doc']*2)
            self.assertEqual(len(DocumentPool(paths).entries),1)
            self.assertNotIn('sources',progress)
            self.assertFalse(list(root.glob('document-*')))
            self.assertTrue((root/'search-1.json').exists())
