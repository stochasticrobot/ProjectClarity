"""Dispatch python -m evaluation to the research-case command-line interface.

The runner supports inspecting dated cases, starting a separately requested live
research experiment, and scoring saved results. This wrapper only calls its main
function and returns the exit status to the shell. Importing the module does not
start research. The offline full-suite command is evaluation.regression_suite,
not this evaluation-case entry point.
"""
from evaluation.research_case_runner import main

if __name__ == '__main__':
    raise SystemExit(main())
