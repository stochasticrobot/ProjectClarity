"""Read outbound links from saved HTML and PDF bytes without network access.

LinkRankingStage calls this reader on acquisition artifacts. Anchor text is the
link title; an HTML title attribute or image alt text is a fallback. PDF annotation
contents provide titles when available. Plain text has no hyperlink metadata.
"""
from io import BytesIO
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from pypdf import PdfReader
from projectclarity.documents.web_acquisition import canonical_url


def document_links(body, content_type, source_url, fallback_title):
    """Recover labeled destinations while preserving their source-page context.

    Args:
        body: Complete saved source bytes, never downloaded here.
        content_type: Saved acquisition MIME hint.
        source_url: Final source URL used to resolve relative destinations.
        fallback_title: Search/source title if the document has no title.
    Returns:
        Page title and unique (destination, title) dictionaries. Unsupported or
        credential-bearing URLs and same-page fragments are excluded. PDF parse
        failures propagate; missing link titles remain empty, not invented.
    """
    title = fallback_title or source_url
    pairs = []
    if body.startswith(b'%PDF-') or 'application/pdf' in content_type:
        reader = PdfReader(BytesIO(body))
        title = str((reader.metadata or {}).get('/Title') or title)
        for page in reader.pages:
            for ref in page.get('/Annots', []):
                annotation = ref.get_object()
                action = annotation.get('/A')
                if action:
                    action = action.get_object()
                    if action.get('/URI'):
                        pairs.append((str(action['/URI']), str(annotation.get('/Contents') or '')))
    elif 'html' in content_type or not content_type:
        soup = BeautifulSoup(body, 'html.parser')
        if soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(' ', strip=True)
        base = soup.find('base', href=True)
        resolution_url = urljoin(source_url, base['href']) if base else source_url
        for anchor in soup.find_all('a', href=True):
            label = anchor.get_text(' ', strip=True) or anchor.get('title', '')
            if not label:
                label = ' '.join(image.get('alt', '') for image in anchor.find_all('img')).strip()
            pairs.append((urljoin(resolution_url, anchor['href']), label))
    links, seen = [], set()
    for destination, label in pairs:
        try:
            destination = canonical_url(urljoin(source_url, destination))
            if destination == canonical_url(source_url):
                continue
        except ValueError:
            continue
        identity = (destination, label)
        if identity not in seen:
            links.append({'url': destination, 'link_title': label})
            seen.add(identity)
    return title, links
