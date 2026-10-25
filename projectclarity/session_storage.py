"""Save the local files that let you inspect one research run.

For example, session.json records which steps ran and which sources succeeded.
These helpers replace a complete file in one operation, so an interrupted write
is less likely to leave half a JSON document. The session includes the workflow scheduler journal used by the resume command;
workflow_checkpoints.py separately owns the native checkpoint store for review. The graph and retrieval pipeline call
these helpers; this file does not decide what the next research step should be.

Gotchas: replacement is atomic for ONE file, not a transaction across artifacts
or a guarantee of power-loss durability. Normal process permissions apply here;
owner-only extraction traces use extraction_call_tracing.write_private_text().
Keep run data out of version control; an ignored folder is not encryption.
"""

from pathlib import Path
from projectclarity.research_schemas import ResearchSession


def write_text(path: Path, text: str) -> None:
    """Replace a snapshot atomically so an interrupted write does not truncate it.

    Args:
        path: Local destination file Path; its parent directory must already exist.
        text: Complete UTF-8 snapshot text to write.

    Returns:
        None. Replace the destination atomically via a temporary sibling file.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_session(session: ResearchSession, output_root: Path) -> None:
    """Save the session's JSON projection so users can inspect progress and candidates.

    The caller must create the run directory first. This writes typed application data, not
    an SDK object or credential configuration; it does not scan arbitrary research text for
    secrets. Native execution checkpoints are stored separately.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        None. Replace this run's session.json with the model's JSON-compatible projection.
    """
    path = output_root / session.session_id / "session.json"
    write_text(path, session.model_dump_json(indent=2) + "\n")
