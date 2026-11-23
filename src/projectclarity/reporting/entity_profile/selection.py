"""Request and check source-linked contributions for one named subject.

The request carries the saved identity scope, complete batch evidence and all
answer requirements. Validation rejects unknown references and findings based on
ambiguous identity. It does not establish real-world identity or factual truth.
"""
from projectclarity.model_access.model_client import request_for
from projectclarity.reporting.report_payloads import compact_packet
from projectclarity.reporting.entity_profile.contracts import ProfileBatch


def profile_request(packet, facts, model):
    """Build the profile's own request while reusing lossless evidence compaction.

    Args:
        packet: Frozen report plan, question, requirements and coverage.
        facts: Whole reviewed records assigned to this batch.
        model: Selected backend's model identifier.
    Returns:
        Complete schema-bound request with 16384 requested output tokens and a
        180-second timeout. Adapters apply their declared effective caps. No I/O.
    """
    request = request_for('prepare_entity_profile_batch.txt', compact_packet(packet, facts), ProfileBatch, model)
    schema = request['response_schema']
    ids = [fact['id'] for fact in facts]
    schema['$defs']['IdentityAssessment']['properties']['fact_id']['enum'] = ids
    schema['$defs']['ProfileFinding']['properties']['fact_ids']['items']['enum'] = ids
    schema['$defs']['ProfileSection']['properties']['requirement_id']['enum'] = [r['id'] for r in packet['requirements']]
    schema['properties']['unused_fact_ids']['items']['enum'] = ids
    request.update(max_tokens=16384, timeout=180)
    return request


def validate_profile_batch(text, packet, facts):
    """Check independent contributions and expose unresolved facts for recovery.

    Args:
        text: Original model JSON; no citations or prose are repaired.
        packet: Frozen requirement catalogue and subject scope.
        facts: Exact whole records offered in this request.
    Returns:
        Checked fragment with field-level quarantine diagnostics and missing IDs.
        Invalid envelopes or replies with no usable contributions raise; invalid
        contributions cannot erase valid siblings.
        Mechanical citation checks do not establish semantic correctness.
    """
    from projectclarity.reporting.entity_profile.recovery import inspect_profile
    result = inspect_profile(text, packet, facts)
    if facts and len(result['_recovery']['missing_fact_ids']) == len(facts):
        raise ValueError('No usable profile contributions; all facts remain unresolved.')
    return result
