# Application source

This package implements the research application. Start with the
[architecture guide](../../CODE_GUIDE.md) for a guided code tour, or
[research_graph.py](workflow/research_graph.py) for stage order and stopping rules.
The HTTP entry point is [web_app.py](ux/web_app.py).

## Package map

- [Workflow](workflow/README.md): sessions, graph routing, and research rounds.
- [Question definition](question_definition/README.md): the information an answer needs.
- [Search](search/README.md) and [documents](documents/README.md): discovery and collection.
- [Extraction](knowledge_extraction/README.md): facts and supporting quotations.
- [Evidence selection](evidence_selection/README.md) and [claim review](claim_review/README.md): relevance and source support.
- [Link ranking](link_ranking/README.md): destinations for another research round.
- [Reporting](reporting/README.md): cited items and CSV output.
- [Model access](model_access/README.md): provider requests and usage receipts.
- [Prompts](prompts/README.md) and [UX](ux/README.md): task instructions and browser interface.

Prompts, package READMEs, and browser assets ship with the installed application.
Credentials and generated research do not. Automated checks live in
[tests](../../tests/README.md); developer experiments live in
[testing](../../testing/README.md).
