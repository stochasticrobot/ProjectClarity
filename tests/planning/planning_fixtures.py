"""Translate stored-plan fixtures into the initial model-reply format for offline transports.

Persisted-plan tests keep the legacy fixture; provider tests supply only the fields
the planner is actually asked for. This helper does not validate or repair replies.
"""
from copy import deepcopy


def planning_reply(plan):
    """Represent a saved-plan fixture as the initial planner's wire response.

    Args:
        plan: Handwritten persisted-format plan dictionary, possibly mutated to
            exercise validation failures. Missing fields remain missing.

    Returns:
        A deep copy with scope evidence bookkeeping removed and bounds nested in
        range (null only when both bounds are null), and stored target metadata
        represented as one target value. Invalid non-user counts remain extra
        fields for rejection; the original fixture is never mutated or validated.
    """
    reply = deepcopy(plan)
    for criterion in reply.get("completion_criteria", []):
        if "target_basis" not in criterion:
            continue
        basis = criterion.pop("target_basis")
        if basis == "user_request":
            criterion["target"] = {"requested_count": criterion.pop("target_count")} if "target_count" in criterion else {}
        else:
            criterion["target"] = basis
            if criterion.get("target_count") is None:
                criterion.pop("target_count", None)
    for estimate in reply.get("scope_estimates", []):
        estimate.pop("basis", None)
        estimate.pop("candidate_ids", None)
        bounds = {key: estimate.pop(key) for key in ("lower", "upper") if key in estimate}
        estimate["range"] = None if bounds == {"lower": None, "upper": None} else bounds
    return reply
