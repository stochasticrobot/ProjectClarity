"""Group tests that type into the real command-line menus and review prompts.

The pseudo-terminal driver and child-process fixture make input()/isatty() behave
like a person's terminal while substituting remote services. Run
``python -m unittest discover -s tests/interactive -t . -v`` offline.
Importing this package opens no terminal and starts no research.
"""
