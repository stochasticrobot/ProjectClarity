"""Group tests for reading supplied documents, not for extracting factual claims.

These checks use temporary files and simulated HTTP responses. Running normal
test discovery does not crawl websites or spend model credits. The integration
checks still run the real CLI and LangGraph nodes, so they catch wiring mistakes
that calling a parser function alone would miss.
"""
