# Review what the evidence supports

This folder gives extracted claims a second look. A separate model task reads
the original passage and assesses support. Python then applies the rules that
assign a claim’s status. “Supported” is a model assessment, not a guarantee of truth.

**Start with `review_claims_node.py`, then `adjudicate_claims_node.py`.**
Nothing here publishes a claim or replaces a human review process.

## Files

| File | What it does |
| --- | --- |
| [review_claims_node.py](review_claims_node.py) | Runs the separate model review and checks that its reply refers to the right claims and real quotations. |
| [claim_review_messages.py](claim_review_messages.py) | Contains the review instructions and builds the information sent to the model. |
| [claim_review_contracts.py](claim_review_contracts.py) | Defines what a review answer must contain: verdict, relevance, reason and quotations. |
| [adjudicate_claims_node.py](adjudicate_claims_node.py) | Rechecks evidence and assigns statuses such as supported, insufficient evidence or needs review using Python. |
| `__init__.py` | Marks this folder as a Python package; it starts no work. |

[Back to the application map](../README.md) · [Project overview](../../README.md)
