"""Prepare source metadata that accompanies passages sent for model analysis.

Extraction, claim review and coverage all need to know where evidence came from.
source_context returns the registered URL or local filename, redirect destination,
acquisition time, content hashes and metadata stated by the document itself.
For example, a page's declared publication date is labeled source-stated rather
than treated as verified currency. Local absolute paths and document bodies are
not included. This helper reads the supplied session only; it neither fetches a
source nor validates the truth of its labels.
"""
from pathlib import Path


def source_context(session, source_id):
    """Return the known acquisition and document-declared metadata for one source.

    Model tasks use this alongside quotations to identify their source and timing.
    Missing values remain missing; a stated publisher or publication date is not
    independently verified by this function.

    Args:
        session: Run containing registered sources and acquisition receipts.
        source_id: Identifier of the document owning the evidence passage.
    Returns:
        JSON-compatible acquisition facts and separately labelled source-stated
        metadata. Missing values remain null; no source access or state change occurs.
        Local sources expose a filename only, never their absolute path.
    """
    document = next((d for d in session.documents if d.source_id == source_id), None)
    source = next((s for s in session.sources if s.source_id == source_id), None)
    kind = document.source_kind if document else source.kind if source else None
    location = document.original_location if document else (
        source.url if kind == 'url' else source.path if source else None)
    return dict(source_id=source_id, kind=kind,
        url=location if kind == 'url' else None,
        filename=Path(location).name if kind == 'file' and location else None,
        final_url=document.final_url if document else None,
        acquired_at=document.acquired_at if document else None,
        text_sha256=document.text_sha256 if document else None,
        raw_sha256=document.raw_sha256 if document else None,
        source_stated_metadata=document.source_metadata if document else {})
