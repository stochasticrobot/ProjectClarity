"""Load user-owned task instructions without embedding fallback prompts in code.

Task modules load their adjacent prompts/*.txt files when imported. Resolving
from this module, rather than the working directory, keeps CLI and inspection
paths consistent. Restart the Python process after editing a prompt file.
"""
from pathlib import Path


PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"


def load_prompt(filename: str) -> str:
    """Read one UTF-8 prompt verbatim so user wording reaches the model unchanged.

    Args:
        filename: Basename of a .txt file within the application's prompts directory;
            directory components and other file extensions are not accepted.

    Returns:
        Exact decoded file contents, including whitespace and line endings. Invalid
        names raise ValueError; missing/unreadable files and invalid UTF-8 propagate
        their filesystem/decoding errors. No fallback, formatting, network call or
        file write occurs.
    """
    if Path(filename).name != filename or not filename.endswith(".txt"):
        raise ValueError("Expected a prompt .txt basename")
    return (PROMPT_DIRECTORY / filename).read_bytes().decode("utf-8")
