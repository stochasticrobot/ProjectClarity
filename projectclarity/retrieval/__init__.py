"""Read registered documents and retain provenance for their saved text.

For example, report.pdf and a supplied web page become saved raw files, readable
text, and receipts linking those files to source IDs. The graph's retrieve_sources
node calls source_retrieval.retrieve_one() for each registered source, including
authorized search selections and followed links. This package does not choose
new sources, ask an LLM questions, extract factual claims, or execute page scripts.
See source_retrieval.py for the reading sequence and public_url_fetcher.py for HTTP access rules.
"""
