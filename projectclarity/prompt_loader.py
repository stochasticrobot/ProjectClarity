"""Read the text files containing instructions for each language-model task.

For example, load_prompt("design_answer.txt") returns the contents of
projectclarity/prompts/design_answer.txt. The path is resolved relative to this
Python file, so launching the program from another directory does not change
which instructions are read. Contents are returned exactly as UTF-8 text, without
formatting, trimming or replacement instructions if the file is missing.

Task modules normally load their instructions when imported. Restart the Python
process after editing a prompt file so those modules read the new text. This
module only loads instructions; it neither constructs model requests nor sends
them to a provider.
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
