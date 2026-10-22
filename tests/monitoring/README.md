# Tests: monitoring

Check live status, logging, cancellation and worker ownership, including separate-process tests.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [monitored_worker_fixture.py](monitored_worker_fixture.py) | Run the real research CLI in a child process with a deliberately slow source. |
| [test_fetch_monitoring_cli_integration.py](test_fetch_monitoring_cli_integration.py) | Check monitoring along the actual research CLI, graph, parser, and file-save path. |
| [test_friendly_research_console.py](test_friendly_research_console.py) | Check readable node/source progress, actual prompt/reply display, privacy boundaries and display options through the real CLI. |
| [test_run_monitoring.py](test_run_monitoring.py) | Check operational records and controls without a model, website, or real run. |
| [test_worker_control_integration.py](test_worker_control_integration.py) | Exercise real status, watch, cancellation, and worker-exit detection across processes. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/monitoring -t . -v
```

[Back to the test guide](../README.md)
