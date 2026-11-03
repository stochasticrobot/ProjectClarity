"""Keep large collections of discovered links affordable to assess.

The application can find thousands of links in saved pages. This file combines
repeated destinations, retains where each was found and removes clear non-content
links or pages already downloaded. It then chooses at most 500 destinations using
source-page contribution scores, with at most 50 attributed to the same page.
Links outside that shortlist remain visible as not assessed, not irrelevant.
No model calls or downloads happen here.
"""
from collections import Counter
from urllib.parse import urlsplit, unquote
from projectclarity.documents.web_acquisition import canonical_url

MAX_SHORTLIST = 500
MAX_SOURCE_SHORTLIST = 50
TITLE_BATCH_SIZE = 25
NON_CONTENT_PATHS = frozenset({
    'login', 'log-in', 'signin', 'sign-in', 'signup', 'sign-up', 'logout',
    'log-out', 'register', 'privacy', 'privacy-policy', 'terms',
    'terms-of-service', 'terms-and-conditions', 'cookie-policy', 'cookies',
})
ASSET_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico',
                    '.css', '.js', '.woff', '.woff2', '.ttf', '.mp3', '.mp4',
                    '.wav', '.avi', '.mov', '.zip', '.gz', '.exe', '.dmg')


def exclusion_reason(url, processed_urls):
    """Explain narrow URL-based exclusions without guessing page content.

    Args:
        url: Normalized HTTP(S) destination.
        processed_urls: Set of normalized original and final downloaded URLs.
    Returns:
        Human-readable exclusion reason, or empty text if eligible. Only exact
        final path names and listed asset extensions are filtered; About, Contact,
        PDFs and ambiguous paths remain eligible. Makes no network requests.
    """
    if url in processed_urls:
        return 'Already downloaded in this research run.'
    path = unquote(urlsplit(url).path).rstrip('/').lower()
    if path.endswith(ASSET_EXTENSIONS):
        return 'Non-document asset URL.'
    leaf = path.rsplit('/', 1)[-1]
    for suffix in ('.html', '.htm', '.php', '.aspx'):
        if leaf.endswith(suffix):
            leaf = leaf[:-len(suffix)]
            break
    if leaf in NON_CONTENT_PATHS:
        return 'Account, privacy, cookie or terms page.'
    return ''


def shortlist_links(occurrences, sources):
    """Deduplicate before model work and allocate the fixed source-based shortlist.

    Args:
        occurrences: Saved link occurrences with source URL/title, document score,
            link title and stable ID. Input records are not mutated.
        sources: Acquisition source records; downloaded original/final URLs are
            excluded even if parsing or extraction subsequently failed.
    Returns:
        Unique destination records and counts. Each record preserves every source
        association. The highest-contributing occurrence owns its score and quota;
        ties prefer a nonempty title, then source URL, title and occurrence ID.
        Eligible records sort by descending source score then destination/ID.
        At most 500 are shortlisted and at most 50 belong to one final source URL.
        Others are excluded or not_assessed with a reason. Untitled shortlisted
        links get zero title points without a model request. No backfill after
        failed ratings or reassignment of duplicate ownership is performed.
    """
    processed = set()
    for source in sources:
        if source.get('downloaded'):
            for value in (source.get('url'), source.get('final_url')):
                try:
                    processed.add(canonical_url(value or ''))
                except ValueError:
                    pass
    grouped = {}
    for row in occurrences:
        grouped.setdefault(row['url'], []).append(row)
    rows = []
    for url, group in grouped.items():
        ordered = sorted(group, key=lambda row: (-row['document_score'],
            not bool(row['link_title'].strip()), row['source_url'], row['link_title'], row['id']))
        row = dict(ordered[0])
        row['source_associations'] = [{key: item[key] for key in
            ('source_document_id', 'source_url', 'source_title', 'link_title', 'document_score')}
            for item in ordered]
        reason = exclusion_reason(url, processed)
        row.update(status='excluded' if reason else 'not_assessed', reason=reason,
                   title_score=None, rating=None, shortlisted=False)
        rows.append(row)
    rows.sort(key=lambda row: (-row['document_score'], row['url'], row['id']))
    counts = Counter()
    shortlisted = 0
    for row in rows:
        if row['status'] == 'excluded':
            continue
        source = row['source_url']
        if counts[source] >= MAX_SOURCE_SHORTLIST:
            row['reason'] = f'Not assessed: source-page shortlist limit ({MAX_SOURCE_SHORTLIST}).'
        elif shortlisted >= MAX_SHORTLIST:
            row['reason'] = f'Not assessed: overall shortlist limit ({MAX_SHORTLIST}).'
        else:
            shortlisted += 1
            counts[source] += 1
            titled = bool(row['link_title'].strip())
            row.update(shortlisted=True, status='pending' if titled else 'ranked',
                       title_score=None if titled else 0, rating=None if titled else 'none',
                       reason='' if titled else 'No link title available; no title bonus.')
    return rows, {'discovered_occurrences': len(occurrences),
                  'unique_destinations': len(rows),
                  'duplicates_combined': len(occurrences)-len(rows),
                  'excluded': sum(row['status'] == 'excluded' for row in rows),
                  'shortlisted': shortlisted,
                  'not_assessed': sum(row['status'] == 'not_assessed' for row in rows)}
