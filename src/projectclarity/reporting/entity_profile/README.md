# Single-entity profiles

Build a cited overview and question-specific sections for one requested company,
person or organization. Identity uncertainty and unknown sections remain visible.
This package does not collect sources, resolve aliases or prove identity/truth.

Reading order:

1. `contracts.py`: identity assessments, cited findings and section contributions.
2. `selection.py`: request creation; `recovery.py`: independent source/reference checks and field quarantine.
3. `assembly.py`: deterministic aggregation, identity-conflict withholding and final checks.
4. `builder.py`: real shared bounded scheduling, retries, cancellation and accounting.
5. `export.py`: section-oriented CSV with citations, gaps and formula protection.

The saved profile plan is intent, not evidence. Only matching identity assessments
can support findings; ambiguous/nonmatching facts stay out of the profile's claims.
Semantic attribution remains a model judgment. See [scope and limitations](../../../../docs/REPORT_TYPES.md).


Malformed profile contributions no longer invalidate independently checked
neighbours. Unresolved facts in partially usable replies enter one bounded subset recovery
pass; failed recovery retains the parent findings. Invalid JSON/envelopes and
wholly unusable replies use the existing single output retry. Invalid quotations, uncertain citations and
invented references are never repaired into accepted evidence. Diagnostics remain
in task receipts and withheld-field counts; unresolved IDs remain explicit.
