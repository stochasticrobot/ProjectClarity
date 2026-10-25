"""Discover source leads through authorized web search and observed HTML links.

nodes/web_search records bounded provider results; nodes/select_sources
ranks those leads and registers chosen URLs for normal retrieval. Separately,
nodes/select_followup_sources selects successive batches from links already
observed on retrieved pages, preserving shared destinations. Neither path treats snippets or link labels as
claim evidence. The graph, not these package imports, executes the chosen stages.
"""
