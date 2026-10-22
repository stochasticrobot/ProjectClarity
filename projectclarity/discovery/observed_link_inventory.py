"""Turn observed HTML anchors into an inspectable, bounded first-hop inventory.

An anchor's existence is not evidence that its destination supports a claim.
Python resolves relative hrefs against the fetched final URL, records the parent
source/version, and rejects disallowed/duplicate targets before any model chooses.
Only the exact origins of original supplied URLs are permitted in this increment;
subdomains and redirect destinations do not silently enlarge that permission.
"""
import ipaddress
from urllib.parse import urljoin, urlsplit, urlunsplit


def canonical_url(value):
    """Normalize scheme/host/default port and remove fragments without guessing query semantics.

    This is conservative URL deduplication, not semantic page identity: alternate
    query strings and server aliases can still identify the same content.
    """
    try:
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
    """Return the scheme/host/port permission boundary for a normalized HTTP URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def seed_origins(session):
    """Derive permissions only from user-supplied URLs, never from model output or redirects."""
    return {origin(url) for source in session.sources if source.kind == "url" and source.depth == 0
        if (url := canonical_url(source.url))}


def inventory_links(session):
    """Record first-hop candidates with ancestry and explicit exclusion reasons.

    At most 80 unique eligible links reach the selection prompt, in source/DOM
    order. The saved inventory discloses this restriction and all bounded anchors.
    A source with zero extracted claims can still contribute useful links.
    """
    permitted = seed_origins(session)
    seed_ids = {s.source_id for s in session.sources if s.kind == "url" and s.depth == 0}
    seen = {canonical_url(s.url) for s in session.sources if s.kind == "url"}
    seen |= {canonical_url(d.final_url) for d in session.documents if d.final_url}
    links, eligible = [], 0
    for doc in session.documents:
        if doc.source_id not in seed_ids or doc.status != "retrieved" or not doc.final_url:
            continue
        for index, anchor in enumerate(doc.links, 1):
            url = canonical_url(urljoin(doc.final_url, anchor["href"]))
            reason = None
            if not url:
                reason = "invalid_or_nonpublic_url"
            elif origin(url) not in permitted:
                reason = "outside_seed_origins"
            elif url in seen:
                reason = "already_known_or_duplicate"
            elif eligible >= 80:
                reason = "selection_input_cap"
            if url:
                seen.add(url)
            if reason is None:
                eligible += 1
            links.append({"link_id": f"{doc.source_id}_L{index}", "parent_source_id": doc.source_id,
                "parent_raw_sha256": doc.raw_sha256, "parent_text_sha256": doc.text_sha256,
                "parent_final_url": doc.final_url, "url": url, "label": anchor["label"],
                "eligible": reason is None, "exclusion_reason": reason})
    return links
