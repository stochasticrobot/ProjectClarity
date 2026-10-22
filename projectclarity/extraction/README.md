# Extract proposed knowledge

This folder asks the model to read saved document passages in light of the research plan.
It proposes people, organizations, relationships and claims, then checks that the
quoted text really exists. A matching quotation is not proof that the claim is true.

**Start with `extract_knowledge_node.py`.** It is the actual LangGraph step.
The review folder handles the separate question “does this passage support the claim?”

## Files

| File | What it does |
| --- | --- |
| [extract_knowledge_node.py](extract_knowledge_node.py) | Runs extraction over new passages, sharing the run budget and retaining earlier results. |
| [knowledge_extraction_messages.py](knowledge_extraction_messages.py) | Builds the model instructions and task input: what to look for and which passage to read. |
| [extraction_contracts.py](extraction_contracts.py) | Defines the forms for the model’s answer and our saved extraction records. |
| [saved_source_passages.py](saved_source_passages.py) | Reads the saved source safely, checks its version, and divides long text into passages. |
| [validate_evidence_node.py](validate_evidence_node.py) | Checks names/IDs, source versions and the exact locations of quotations. |
| [source_quote_matching.py](source_quote_matching.py) | Tolerates layout whitespace while mapping quotes back to untouched source text; rejects changed words and ambiguous locations. Shared with claim review. |
| [candidate_claim_storage.py](candidate_claim_storage.py) | Saves proposed claims and evidence; writes the readable candidates.md file. Does not publish facts. |
| [extraction_call_tracing.py](extraction_call_tracing.py) | Records private extraction prompts and replies for inspection, separate from operational logs. |
| [extraction_inspection.py](extraction_inspection.py) | Displays those saved calls and results without asking the model again. |
| `__init__.py` | Marks this folder as a Python package; it starts no work. |

[Back to the application map](../README.md) · [Project overview](../../README.md)
