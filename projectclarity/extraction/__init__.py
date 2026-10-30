"""Turn saved source passages into proposed claims with inspectable evidence links.

A candidate claim is a statement suggested by the extraction model, not an accepted
fact. Modules here define the response and saved-attempt formats, read exact saved
text, build request messages, match quotations and retain private call diagnostics.
For example, a proposed affiliation must keep the source passage and quotation that
will later be checked and reviewed.

The processing step lives in nodes/extract_knowledge; agentic_workflow connects it
to later validation and saving steps. Task instructions are loaded from the user's
prompt text files, not authored by this package initializer. Importing this package
starts no model request, web search or publication.
"""
