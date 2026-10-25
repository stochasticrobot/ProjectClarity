"""Turn saved documents into inspectable candidate claims, not accepted facts.

Start with nodes/extract_knowledge to follow the work. extraction_contracts.py
defines the records; saved_source_passages.py reads evidence; knowledge_extraction_messages.py
owns the model prompt; nodes/validate_evidence checks quotations; and
nodes/persist_candidates saves the results. agentic_workflow.py connects these steps.
extraction_call_tracing.py privately captures each prompt/answer, while
extraction_inspection.py explains those records in the CLI without another call.
No module in this package searches the web or publishes accepted knowledge.
"""
