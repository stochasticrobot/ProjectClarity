"""Make `python -m evaluation` use the documented evaluation command line.

Importing the runner does nothing; the explicit subcommand chooses whether to
inspect local data or start a paid research run.
"""
from evaluation.research_case_runner import main

if __name__ == '__main__':
    raise SystemExit(main())
