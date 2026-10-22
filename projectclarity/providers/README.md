# Connect research tasks to a model

This folder handles talking to a model and inspecting selected calls. Planning,
extraction and review are tasks; the provider is the service that performs their
model calls. Keeping these separate makes a future backend change easier.

**Start with `structured_model_provider.py`.** Anthropic is the only implemented
backend today. A common adapter is not a claim that other providers already work.

## Files

| File | What it does |
| --- | --- |
| [structured_model_provider.py](structured_model_provider.py) | Builds the current Anthropic/LangChain adapter used by all model tasks; it does not decide research goals. |
| [structured_model_call_execution.py](structured_model_call_execution.py) | Handles shared request accounting, private traces and failures for review/discovery calls. |
| [model_call_inspection.py](model_call_inspection.py) | Reads saved review/discovery prompts and replies without making another model call. |
| `__init__.py` | Marks this folder as a Python package; it starts no work. |

[Back to the application map](../README.md) · [Project overview](../../README.md)
