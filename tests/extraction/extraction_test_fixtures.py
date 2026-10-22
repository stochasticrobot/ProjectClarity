"""Provide a small invented source and the structured reply we expect to inspect.

Alice and Lab X are fixture names, not real research findings. Tests may alter
one field or quote to exercise a failure. Returning a fresh dictionary prevents
one test's intentionally malformed answer from contaminating the next test.
"""


SOURCE_TEXT = "Alice works at Lab X. Alice studies adversarial attacks.\n"


def proposed_batch():
    """Return one atomic affiliation claim with local entity IDs and an exact quote."""
    return {
        "entities": [
            {"entity_id": "E1", "name": "Alice", "entity_type": "Person"},
            {"entity_id": "E2", "name": "Lab X", "entity_type": "Organization"},
        ],
        "claims": [{"claim_id": "C1", "statement": "Alice works at Lab X.", "goal_ids": ["G1"],
                    "entity_ids": ["E1", "E2"],
                    "relationship": {"subject_id": "E1", "predicate": "WORKS_FOR", "object_id": "E2"},
                    "qualifications": [],
                    "evidence": [{"passage_id": "P1", "quote": "Alice works at Lab X."}]}],
    }
