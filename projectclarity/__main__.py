"""Module-execution entry point for ``python -m projectclarity``.

Purpose:
    This is the small starting point Python runs when you type
    ``python -m projectclarity``. It hands your command to command_line.py and tells the
    shell whether the command succeeded. For example, a missing required
    question produces a nonzero exit code that a shell script can detect.
Responsibilities:
    Call command_line.main() and turn its return value into the process exit status.
    Argument parsing and research behavior belong to command_line.py and agentic_workflow.py;
    keeping them out of this wrapper lets tests exercise the same implementation.
Typical use:
    ``python -m projectclarity`` opens the guided menu in a terminal.
    ``python -m projectclarity --question "..." --urls source_urls.txt`` starts
    new research immediately, without the menu or input setup questions.
    ``python -m projectclarity research --question "..." --urls source_urls.txt``
    is the equivalent explicit-subcommand form. Both retain guided plan review.
    Importing this module does not start a run because execution is guarded by
    the standard __name__ check below.
"""

from projectclarity.command_line import main

if __name__ == "__main__":
    raise SystemExit(main())
