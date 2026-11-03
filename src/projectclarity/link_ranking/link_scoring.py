"""Score shortlisted titles and choose the most promising next links.

The application has already combined repeated destinations and chosen a bounded
shortlist. This file checks that the AI rated every requested title, adds those
ratings to source contribution points, and selects up to ten links with at most
three attributed to one source page. It does not assess excluded links or fetch
any destination. A defensive duplicate check also protects standalone callers.
"""
from collections import Counter
from typing import Literal
from pydantic import BaseModel, ConfigDict

TITLE_POINTS = {'none': 0, 'low': 5, 'medium': 10, 'high': 25}


class LinkRating(BaseModel):
    """One title-only usefulness estimate bound to a supplied occurrence ID."""
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str
    rating: Literal['none', 'low', 'medium', 'high']
    reason: str


class LinkRatings(BaseModel):
    """Complete response for one batch, checked against exact supplied IDs."""
    model_config = ConfigDict(extra='forbid', strict=True)
    ratings: list[LinkRating]


def validate_ratings(text, candidates):
    """Reject incomplete, duplicate or invented ratings before assigning scores.

    Args:
        text: Raw model JSON response.
        candidates: Exact occurrence records sent in this batch.
    Returns:
        Validated ratings or ValueError; no partial response is accepted.
    """
    result = LinkRatings.model_validate_json(text)
    ids = [item.id for item in result.ratings]
    if len(ids) != len(set(ids)) or set(ids) != {item['id'] for item in candidates}:
        raise ValueError('Ratings must cover every supplied link exactly once.')
    if any(not item.reason.strip() for item in result.ratings):
        raise ValueError('Every rating needs a reason.')
    return result


def select_links(candidates):
    """Select diverse destinations using deterministic additive scores.

    Args:
        candidates: Occurrences with source_document_id, source_url, URL and both scores;
            only rated occurrences are eligible. Input records are not mutated.
    Returns:
        Up to ten copied records with total_score, limited to three per final source URL (redirect aliases share a quota).
        Ties use destination then occurrence ID. A duplicate's best occurrence
        owns its quota even if another source could have supplied that URL.
    """
    ranked = sorted(({**row, 'total_score': row['document_score'] + row['title_score']}
                     for row in candidates if row.get('status') == 'ranked'),
                    key=lambda row: (-row['total_score'], row['url'], row['id']))
    selected, seen, counts = [], set(), Counter()
    for row in ranked:
        if row['url'] in seen:
            continue
        seen.add(row['url'])
        source = row['source_url']
        if counts[source] >= 3:
            continue
        selected.append(row)
        counts[source] += 1
        if len(selected) == 10:
            break
    return selected
