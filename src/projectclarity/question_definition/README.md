# Define the answer requirements

Turns a research question into the fields an answer must contain. Those fields
provide a shared target for relevance selection, claim review, and reporting.

[requirements_definition.py](requirements_definition.py) defines the information
item schema, builds the model request, and validates the returned definition.
The [setup graph](../workflow/research_graph.py) owns ambiguity checking, human
clarification, and refinement; this module does not decide when to run them.

The output is a structured set of answer requirements, not search results or
verified facts. Later stages use the finalized question and requirements rather
than inventing a different answer format. See [prompts](../prompts/README.md) for
the model-facing instructions and [workflow](../workflow/README.md) for persistence.
