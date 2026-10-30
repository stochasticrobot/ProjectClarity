"""Record hyperlinks found in acquired HTML so later research can choose among them.

An observed link is a destination and label actually present in a downloaded page,
not a URL invented by the model. Helpers normalize HTTP URLs, apply access and
scope rules, and retain the pages and paths from which each link was found.
This set of possible next destinations is the link frontier. A destination can
have several incoming paths without becoming several independent sources.
Recording a link does not fetch it or establish that it contains useful evidence.
"""
import ipaddress
from urllib.parse import urljoin, urlsplit, urlunsplit
from projectclarity.discovery.url_policy import url_denial


def canonical_url(value):
    """Trim surrounding whitespace, normalize the host and remove URL fragments.

    This is conservative URL deduplication, not semantic page identity: alternate query
    strings and server aliases can still identify the same content.

    Args:
        value: Observed link or search-result string, possibly padded with Unicode
            whitespace by its source. Internal characters and escapes are preserved.

    Returns:
        A normalized HTTP(S) URL, or None for a value outside the accepted public-URL shape.
        Embedded whitespace and non-printable characters remain invalid after boundary
        trimming. This does not resolve DNS or grant fetch permission; callers retain
        the original URL separately where provenance is required.
    """
    try:
        value = value.strip()
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or len(value) > 2048
                or any(c.isspace() or not c.isprintable() for c in value)):
            return None
        host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
        port = parsed.port
        if port not in {None, 80 if parsed.scheme == "http" else 443}:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                return None
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        return None


def origin(url):
    """Return the scheme/host/port permission boundary for a normalized HTTP URL.

    Args:
        url: HTTP(S) address of the page to inspect or retrieve.

    Returns:
        The URL's scheme and network location, including a nondefault port when present.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def seed_origins(session):
    """Derive origins for the optional same-origin mode from registered depth-zero URLs.

    Include supplied, catalogue and search-selected seeds, not arbitrary model
    URLs or redirect destinations. The caller applies this set only when
    same_origin_links is enabled; the normal terminal interface permits cross-origin links.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        A set of normalized origins from registered depth-zero URL sources, including
        search-selected seeds.
    """
    return {origin(url) for source in session.sources if source.kind == "url" and source.depth == 0
        if (url := canonical_url(source.url))}


def inventory_links(session, progress=None):
    """Record a recursive URL frontier with ancestry, intersections and explicit exclusions.

    All unique policy-eligible destinations remain in the persistent inventory. Model
    context paging is separate from eligibility, never a source/DOM prefix. All retrieved
    depths contribute links. Known destinations remain recorded with their source IDs,
    but are not fetched again. Optional path-progress pruning pauses unknown analysis
    and stops exhausted branches without marking their sibling destinations as seen.
    A persisted exploration cycle temporarily defers links beyond its search-timing
    horizon; this is separate from absolute depth and no-progress pruning.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        progress: Optional precomputed branch ledger; None derives it from current receipts.

    Returns:
        A list of link dictionaries containing IDs, ancestry, target URL, label and
        eligibility/exclusion reasons and branch counters; no links are fetched.
    """
    permitted = seed_origins(session)
    if progress is None:
        from projectclarity.discovery.branch_progress import branch_progress
        progress = branch_progress(session)
    known = {canonical_url(s.url): s.source_id for s in session.sources if s.kind == "url"}
    known.update({canonical_url(d.final_url): d.source_id for d in session.documents if d.final_url})
    seen = set(known)
    links = []
    sources = {s.source_id: s for s in session.sources}
    for doc in session.documents:
        if doc.status != "retrieved" or not doc.final_url:
            continue
        depth = getattr(sources[doc.source_id], 'depth', 0) + 1
        branch = progress.get('sources', {}).get(doc.source_id, {})
        for index, anchor in enumerate(doc.links, 1):
            url = canonical_url(urljoin(doc.final_url, anchor["href"]))
            reason = None
            if not url:
                reason = "invalid_or_nonpublic_url"
            elif url_denial(url, session.request.url_policy):
                reason = url_denial(url, session.request.url_policy)
            elif session.request.same_origin_links and origin(url) not in permitted:
                reason = "outside_seed_origins"
            elif url in seen:
                reason = "already_known_or_duplicate"
            elif session.request.max_link_depth is not None and depth > session.request.max_link_depth:
                reason = "link_depth_limit"
            elif branch.get('expansion_block'):
                reason = branch['expansion_block']
            elif (session.request.explore_before_search_depth
                    and (cycle := session.workflow.get('exploration_cycle'))
                    and cycle['depths'].get(doc.source_id, 0) >= cycle['limit']):
                reason = 'search_exploration_boundary'
            if url and reason is None:
                seen.add(url)
            links.append({"link_id": f"{doc.source_id}_L{index}", "parent_source_id": doc.source_id,
                "parent_raw_sha256": doc.raw_sha256, "parent_text_sha256": doc.text_sha256,
                "parent_final_url": doc.final_url, "url": url, "label": anchor["label"],
                "depth": depth, "target_source_id": known.get(url),
                "eligible": reason is None, "exclusion_reason": reason,
                "parent_unproductive_count": branch.get('unproductive_count'),
                "parent_progress": branch.get('status')})
    return links


def discovery_connections(session, links):
    """Preserve all observed paths into registered destinations instead of losing intersections.

    Args:
        session: Research session with unique registered URLs, search provenance and selections.
        links: Observed anchor inventory, including duplicates and already-known destinations.

    Returns:
        URL-navigation connections (not factual knowledge edges) with parent/query ancestry,
        target source IDs and selection reasons when selected. Canonical URL equality merges
        fragments/default ports, not arbitrary content aliases or real-world entities.
    """
    targets = {canonical_url(s.url): s.source_id for s in session.sources if s.kind == 'url'}
    targets.update({canonical_url(d.final_url): d.source_id for d in session.documents if d.final_url})
    selected = {s['link_id']: s for s in session.discovery.get('selected_sources', [])}
    connections = []
    for link in links:
        target = targets.get(link['url'])
        if target:
            choice = selected.get(link['link_id'], {})
            connections.append({'kind': 'observed_link', 'link_id': link['link_id'],
                'parent_source_id': link['parent_source_id'], 'target_source_id': target,
                'url': link['url'], 'selected': bool(choice), 'goal_ids': choice.get('goal_ids', []),
                'reason': choice.get('reason'), 'exclusion_reason': link['exclusion_reason']})
    for result in session.search.get('results', []):
        if target := targets.get(result.get('url')):
            connections.append({'kind': 'search_result', 'query_id': result['query_id'],
                'result_id': result['result_id'], 'target_source_id': target, 'url': result['url']})
    return connections
