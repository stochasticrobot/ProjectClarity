# Choose one useful follow-up hop

This folder looks for useful next steps among links actually found on retrieved
pages. The model can explain what information is missing and suggest links;
Python decides whether those suggestions are allowed.

**Start with `select_followup_sources_node.py`.** This runs only with
`--follow-links`. It is not a search engine: only one hop on the original supplied
URL origins is allowed. Earlier work and budgets are shared, not restarted.

## Files

| File | What it does |
| --- | --- |
| [select_followup_sources_node.py](select_followup_sources_node.py) | Examines evidence gaps and eligible links, asks the model to select leads, then checks and registers them or stops. |
| [observed_link_inventory.py](observed_link_inventory.py) | Builds the list of links seen in source pages, including ancestry, duplicates and permission exclusions. |
| [followup_selection_contracts.py](followup_selection_contracts.py) | Defines the selection answer: existing link IDs, research goals and a reason to investigate. |
| `__init__.py` | Marks this folder as a Python package; it starts no work. |

[Back to the application map](../README.md) · [Project overview](../../README.md)
