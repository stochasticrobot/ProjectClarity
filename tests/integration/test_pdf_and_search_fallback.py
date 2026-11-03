"""Check that PDF documents and failed searches are handled usefully and visibly.

The tests build small PDF files and invented search/download responses. They check
that readable text keeps its page references, unsupported PDFs fail explicitly and
an alternate search is used only under the intended conditions. These controlled
examples exercise the research code without relying on a live search service.
"""
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from uuid import uuid4
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from projectclarity.documents.web_acquisition import parse_document, SourceError
from projectclarity.search.web_search import search_wikipedia
from projectclarity.workflow.source_collection import SourceCollection
from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
from projectclarity.claim_review.claim_triage import TriageRun
from tests.integration.test_document_pipeline import ExtractCaller


def pdf_bytes(texts, encrypted=False):
    """Build real text/blank PDF pages for parser and pipeline tests.

    Args:
        texts: One ASCII text string per physical page; empty creates a blank page.
        encrypted: Whether to require a nonempty password.
    Returns:
        PDF bytes, without writing files or contacting external services.
    """
    writer=PdfWriter()
    for text in texts:
        page=writer.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 20 700 Td ('+text+') Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted:writer.encrypt('password')
    output=BytesIO();writer.write(output);return output.getvalue()


class PdfFallbackTests(unittest.TestCase):
    """Verify full provenance, explicit omissions and truthful search routing."""
    def test_pdf_pages_and_failures(self):
        """Check real page extraction, blank-page warnings and unreadable failures.

        Args: None.
        Returns: None; asserts exact physical pages and classified error outcomes.
        """
        parsed=parse_document(pdf_bytes(['Acme Canada','','AI products']),'application/octet-stream')
        self.assertEqual([p['page'] for p in parsed['pages']],[1,2,3])
        self.assertFalse(parsed['pages'][1]['has_text'])
        for page,text in zip(parsed['pages'],['Acme Canada','','AI products']):
            self.assertEqual(parsed['text'][page['start']:page['end']],text)
        self.assertIn('2',parsed['warnings'][0])
        for data,code in [(pdf_bytes(['']),'empty_text'),(pdf_bytes(['Secret'],True),'encrypted_pdf'),(b'%PDF-broken','invalid_pdf')]:
            with self.assertRaises(SourceError) as raised:parse_document(data,'application/pdf')
            self.assertEqual(raised.exception.code,code)

    def test_api_empty_error_and_encoding(self):
        """Exercise the documented API response contract without network access.

        Args: None.
        Returns: None; validates URL encoding, snippet cleanup and failure handling.
        """
        urls=[]
        def transport(url,limit,cancelled):
            """Capture the request and supply one article lead.

            Args: url, limit, cancelled: Production HTTP contract.
            Returns: Successful fake API response containing untrusted snippet markup.
            """
            urls.append(url)
            return 200,{},json.dumps({'query':{'search':[{'title':'AI & Canada','snippet':'<b>AI</b> work'}]}}).encode()
        receipt=search_wikipedia('AI & Canada',5,lambda:None,transport)
        self.assertIn('srsearch=AI+%26+Canada',urls[0])
        self.assertEqual(receipt['results'][0]['snippet'],'AI work')
        self.assertIn('AI_%26_Canada',receipt['results'][0]['url'])
        self.assertEqual(search_wikipedia('x',5,lambda:None,lambda *a:(200,{},b'{"query":{"search":[]}}'))['results'],[])
        for response in [(429,{},b''),(200,{},b'{"error":{}}')]:
            with self.assertRaises(SourceError):search_wikipedia('x',5,lambda:None,lambda *a:response)

    def test_graph_second_fallback_pdf_provenance(self):
        """Run the real graph through failed second search, fallback and PDF extraction.

        Args: None.
        Returns: None; checks deduplication, full text delivery and persisted page IDs.
        """
        calls=[]
        lead={'url':'https://example.org/a.pdf','title':'Report','snippet':'Lead only'}
        def search(query,limit,cancelled):
            """Return one primary lead, then an explicit unavailable second search.

            Args: query, limit, cancelled: Production search contract.
            Returns: First-query receipt; raises SourceError on the second query.
            """
            if query=='second':raise SourceError('search_blocked','HTTP202')
            return {'provider':'offline','results':[lead]}
        def fallback(query,limit,cancelled):
            """Record the single fallback and return the same URL to test deduplication.

            Args: query, limit, cancelled: Production fallback contract.
            Returns: Wikipedia-shaped receipt with an overlapping lead.
            """
            calls.append(query);return {'provider':'wikipedia','results':[lead]}
        body=pdf_bytes(['Acme builds AI. '*300,'','Canadian operations.'])
        class Reader:
            """Offline acquisition replacement; production parsing remains intact."""
            def fetch(self,url):
                """Supply full real PDF bytes without network requests.

                Args: url: Discovered source URL.
                Returns: Ordinary acquisition tuple, including unmodified PDF bytes.
                """
                return url,{'content-type':'application/pdf'},body,[]
        with TemporaryDirectory() as tmp:
            state={'session_id':str(uuid4()),'research_question':'AI companies?','information_needed':[],
                   'searches':[{'query':'first'},{'query':'second'}]}
            caller=ExtractCaller();run=SourceCollection(Path(tmp),state,lambda s:None,caller,Event(),search=search,reader=Reader(),fallback=fallback)
            run.execute()
            self.assertEqual(calls,['second'])
            self.assertEqual(len(run.run['sources']),1)
            self.assertEqual([note['query_index'] for note in run.run['sources'][0]['provenance']],[1,2])
            self.assertEqual(run.run['searches'][1]['primary_failure']['error'],'HTTP202')
            self.assertEqual(run.run['status'],'partial') # unreadable physical page
            index=json.loads((run.root/'knowledge.json').read_text())
            self.assertTrue(index['chunks'])
            content=''
            for chunk in index['chunks']:
                receipt=json.loads((run.root/chunk['artifact']).read_text())
                self.assertEqual(receipt['pages'],chunk['pages'])
                self.assertNotIn(2,chunk['pages'])
                content+=json.loads(receipt['request']['messages'][0]['content'])['document_chunk']
            self.assertEqual(content,(run.root/'document-001/document.txt').read_text())
            self.assertTrue((run.root/'search-2-primary-failed.json').exists())
            self.assertEqual(json.loads((run.root/'search-2.json').read_text())['provider'],'wikipedia')
            selection=EvidenceSelectionStage(Path(tmp),state,lambda s:None,caller,Event());selection.execute()
            self.assertEqual(selection.selection['status'],'completed')
            self.assertTrue(selection.records)
            for record in selection.records:self.assertTrue(record['source']['pages'])
            triage=TriageRun(Path(tmp),state,lambda s:None,caller,Event());triage.execute()
            self.assertEqual(triage.triage['status'],'completed')
            for record in triage.decided:self.assertTrue(record['source']['pages'])

    def test_fallback_failure_and_cancellation(self):
        """Keep primary and fallback failure distinct and never fallback after stop.

        Args: None.
        Returns: None; validates bounded attempts, cancellation and explicit errors.
        """
        calls=[]
        def search(query,limit,cancelled):
            """Fail primary requests without attempting network access.

            Args: query, limit, cancelled: Search contract.
            Returns: Never; raises an explicit provider error.
            """
            raise SourceError('search_error','primary failed')
        def fallback(query,limit,cancelled):
            """Record one fallback failure.

            Args: query, limit, cancelled: Fallback contract.
            Returns: Never; raises a separate error.
            """
            calls.append(query);raise SourceError('search_error','fallback failed')
        with TemporaryDirectory() as tmp:
            state={'session_id':str(uuid4()),'research_question':'AI?','information_needed':[],'searches':[{'query':'one'},{'query':'two'}]}
            run=SourceCollection(Path(tmp),state,lambda s:None,ExtractCaller(),Event(),search=search,fallback=fallback)
            run.execute();self.assertEqual(calls,['two']);self.assertEqual(run.run['status'],'failed')
            record=run.run['searches'][1];self.assertEqual(record['primary_failure']['error'],'primary failed');self.assertEqual(record['error'],'fallback failed')
            stop=Event();stop.set();calls.clear()
            run=SourceCollection(Path(tmp),state,lambda s:None,ExtractCaller(),stop,search=search,fallback=fallback)
            run.execute();self.assertEqual(calls,[]);self.assertEqual(run.run['status'],'cancelled')
