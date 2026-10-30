"""Prefer explicitly recognized government or institution domains when choosing sources.

These helpers rank a source's acquired destination or registered URL against a
small built-in government-domain list and the user's configured institution domains.
The scheduler uses the ranking as one input when choosing which document to read.
A matching hostname is a prioritization hint, not proof of authority, factual
support or permission to fetch. Unknown institutions remain ordinary sources, and
page titles or search snippets cannot grant themselves the preferred ranking.
"""
from urllib.parse import urlsplit


OFFICIAL_DOMAINS = ('gc.ca', 'canada.ca', 'ourcommons.ca', 'parl.ca', 'elections.ca',
                    'gov', 'gov.uk', 'gov.au', 'govt.nz', 'europa.eu')


def source_priority(session, source_id):
    """Rank an acquired destination or registered URL by explicit domain hints.

    Args:
        session: Run containing source references, final retrieval URLs and operator hints.
        source_id: Source to rank; missing sources and local files remain ordinary.
    Returns:
        Zero for a recognized official host, one otherwise. Exact hosts or proper
        subdomains match; lookalike suffixes and redirects away lose priority.
        No network request, trust upgrade or source-permission change occurs.
    """
    source = next((s for s in getattr(session, 'sources', []) if s.source_id == source_id), None)
    if source is None or source.kind != 'url':
        return 1
    doc = next((d for d in session.documents if d.source_id == source_id), None)
    url = getattr(doc, 'final_url', None) or source.url
    try:
        host = (urlsplit(url).hostname or '').rstrip('.').lower().encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        return 1
    return 0 if any(host == domain or host.endswith('.' + domain)
        for domain in (*OFFICIAL_DOMAINS, *getattr(session.request, 'official_domains', []))) else 1


def task_source_priority(session, task):
    """Resolve a scheduler task's source before applying official-domain hints.

    Args:
        session: Run owning passage and source inventories.
        task: Retrieval or passage task; other tasks retain ordinary priority.
    Returns:
        The source's scheduling rank, with one for source-independent operations.
    """
    sid = task.get('source_id')
    if not sid and task.get('batch_id'):
        sid = next((a['source_id'] for a in (session.extraction or {}).get('attempts', [])
            if a['batch_id'] == task['batch_id']), None)
    return source_priority(session, sid)
