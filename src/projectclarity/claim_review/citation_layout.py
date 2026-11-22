"""Map a model quotation to original source characters, allowing only layout changes.

Word choice, punctuation, case and passage identity are never corrected. An exact
quotation is retained. Otherwise a unique match differing only in whitespace can
be restored to its original source span, with an explicit audit record. This does
not establish that the quoted passage supports the claim.
"""
import re


def source_quote(quote, source):
    """Resolve a quotation to one unambiguous source span without guessing its text.

    Args:
        quote: Nonblank model-provided quotation.
        source: Text of the specific passage identified by the model.
    Returns:
        Pair of exact source quotation and optional conversion audit. Already exact
        quotes return unchanged with no audit. Whitespace-only matches must be
        unique; missing, ambiguous or changed-word/punctuation matches raise
        ValueError. No I/O or modification of the supplied strings occurs.
    """
    if not quote.strip():
        raise ValueError('Citation quote is blank.')
    if quote in source:
        return quote, None
    pattern=r'\s+'.join(re.escape(part) for part in quote.split())
    matches=list(re.finditer(pattern,source))
    if len(matches)!=1:
        raise ValueError('Citation is not an exact or unique whitespace-equivalent source excerpt.')
    match=matches[0]
    exact=source[match.start():match.end()]
    return exact, {'operation':'restore_source_whitespace','original':quote,
                   'source_start':match.start(),'source_end':match.end(),'normalized':exact}
