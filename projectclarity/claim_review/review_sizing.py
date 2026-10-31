"""Reserve response space before submitting claims for model review.

A claim count alone does not bound response size: one claim can contain several
report records, each requiring separate eligibility decisions and quotations.
The packer uses these estimates with the run's configured output-token ceiling.
They are deliberately conservative scheduling estimates, not tokenizer results or
a guarantee that a model will finish. Incomplete responses still fail validation;
no truncated JSON is salvaged and no output allowance is silently increased.
"""
import json
import math

from projectclarity.claim_review.qualification import qualification_work


def review_output_allowance(max_output_tokens):
    """Leave a quarter of the configured response limit for estimation error.

    Args:
        max_output_tokens: Positive response-token ceiling from the saved run budget.
    Returns:
        Integer scheduling allowance, never greater than the configured ceiling.
        This does not reserve additional requests or modify the provider settings.
    """
    return max(0, int(max_output_tokens * 0.75))


def review_output_estimate(candidate, catalog):
    """Estimate response space for one intact factual and inclusion assessment.

    Args:
        candidate: Typed extracted claim, including original quotes and report records.
        catalog: Goal-keyed inclusion checks that will actually be requested.
    Returns:
        Estimated output tokens: at least 512 for the factual/mapping assessment and
        512 per inclusion decision, increased for long evidence or many mapped fields.
        UTF-8 bytes provide a conservative size proxy, not provider token accounting.
        The original candidate is never shortened, split or modified.
    """
    proposal = candidate.proposal
    quote_bytes = max((len(e.quote.encode('utf-8')) for e in proposal.evidence), default=0)
    field_bytes = len(json.dumps([r.model_dump(mode='json') for r in proposal.result_records],
                                ensure_ascii=False).encode('utf-8'))
    factual = max(512, 256 + math.ceil((quote_bytes + field_bytes) / 2))
    inclusion = max(512, 384 + math.ceil(quote_bytes / 2))
    return factual + qualification_work(candidate, catalog) * inclusion
