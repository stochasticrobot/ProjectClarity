# Tests: interactive

Check the terminal menus and question/answer flow, including real pseudo-terminal behavior.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [interactive_research_process.py](interactive_research_process.py) | Launch the real CLI under a terminal, substituting only Anthropic HTTP. |
| [pseudo_terminal_driver.py](pseudo_terminal_driver.py) | Drive a real terminal child process without installing a UI-testing framework. |
| [test_guided_terminal_integration.py](test_guided_terminal_integration.py) | Exercise the actual user-facing menus through a real pseudo-terminal. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/interactive -t . -v
```

[Back to the test guide](../README.md)
