"""Store optional broad observations separately from the answer to one question.

The knowledge-capture feature records entities, relationships and attributes found
in sources, including observations not needed for the current answer. This package
defines the response format, checks its separate allowance and stores accepted
source-linked observations in SQLite. Capturing an observation does not make it a
reviewed answer claim. Importing the package starts no capture or database work.
"""
