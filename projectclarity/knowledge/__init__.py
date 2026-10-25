"""Accumulate source-grounded graph observations separately from answer selection.

The workflow node captures entities and assertions, validates their source anchors,
and commits them incrementally to a local SQLite node/edge store. This package is
not the LangGraph checkpoint store, a truth-certification service or an MCP server.
"""
