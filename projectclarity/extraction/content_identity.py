"""Reuse passage-extraction work when two acquired sources have identical saved text.

Different URLs can return the same document. The extraction step can record one
source as an alias of another source's extraction work after checking both files.
content_aliases validates those saved links against the acquisition records: both
sources must still have the same text hash, and alias chains are not accepted.
Original URLs and acquisition records remain distinct; duplicated content is not
independent corroboration. This helper checks saved metadata without rereading
files, so evidence consumers must still verify the actual text bytes.
"""


def content_aliases(session):
    """Return valid links from duplicate sources to the source whose extraction work they share.

    Both acquired document records must still have the same saved text hash. Reject
    changed versions, missing documents, self-links and alias chains rather than
    using them to hide unread or different content.

    Args:
        session: Run containing extraction aliases and acquired document receipts.
    Returns:
        Alias source ID to canonical source ID for matching retrieved text hashes.
        Chains, missing documents and changed versions are excluded. No disk I/O
        or evidence approval occurs; actual evidence consumers still verify bytes.
    """
    documents = {d.source_id: d for d in session.documents if d.status == 'retrieved'}
    aliases = (session.extraction or {}).get('source_aliases', {})
    result = {}
    for sid, row in aliases.items():
        canonical = row.get('source_id')
        left, right = documents.get(sid), documents.get(canonical)
        if (sid != canonical and canonical not in aliases and left and right
                and left.text_sha256 and left.text_sha256 == right.text_sha256 == row.get('text_sha256')):
            result[sid] = canonical
    return result
