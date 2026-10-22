# Tests: research

Check shared research state, CLI outcomes, saved files, graph display and workflow failures.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [research_test_fixtures.py](research_test_fixtures.py) | Build small, artificial research sessions for focused component tests. |
| [test_command_line_outcomes.py](test_command_line_outcomes.py) | Check the exit codes and error messages that scripts and operators depend on. |
| [test_plan_rendering.py](test_plan_rendering.py) | Check what a person sees when reviewing a plan in a terminal or Markdown. |
| [test_research_initialization.py](test_research_initialization.py) | Unit tests for deterministic request, source-list, and session initialization. |
| [test_run_graph_visualization.py](test_run_graph_visualization.py) | Verify text graph views against real research execution, without live services. |
| [test_session_storage.py](test_session_storage.py) | Check saved research state and what happens when a disk operation fails. |
| [test_workflow_failure_handling.py](test_workflow_failure_handling.py) | Check graph-node decisions and interrupted work without calling external services. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/research -t . -v
```

[Back to the test guide](../README.md)
