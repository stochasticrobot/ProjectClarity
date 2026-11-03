"""Keep the list of document paths to process, regardless of where they came from.

Search can supply addresses, but it is only one possible source of paths. This
file accepts a supplied list, combines exact duplicates and keeps any accompanying
notes about their origin. It does not search, download, interpret those notes or
assume that a path is a web address. The document processor decides how to read it.
"""
from copy import deepcopy


class DocumentPool:
    """An ordered, source-independent collection of paths and optional origin notes."""

    def __init__(self, paths=()):
        """Collect supplied paths once while preserving their order and origin notes.

        Args:
            paths: Iterable of nonblank strings or dictionaries with path, optional
                title and optional provenance. Provenance may be any JSON-safe
                value; it is copied, not interpreted. Strings are treated as paths,
                never queries. Pass a list even when supplying a single path.
        Returns:
            None. Stores copied entries in memory without files, network or model
            calls. Exact duplicate paths share an entry with distinct origin notes.
            Invalid path/title values raise ValueError before processing begins.
        """
        if isinstance(paths, (str, bytes)):
            raise ValueError('Supply a list of document paths, not one string.')
        entries = {}
        for item in paths:
            entry = {'path': item} if isinstance(item, str) else item
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path'].strip():
                raise ValueError('Each document needs a nonblank path.')
            path = entry['path']
            title = entry.get('title') or path
            if not isinstance(title, str):
                raise ValueError('Document titles must be text.')
            saved = entries.setdefault(path, {'path': path, 'title': title, 'provenance': []})
            if 'provenance' in entry and entry['provenance'] not in saved['provenance']:
                saved['provenance'].append(deepcopy(entry['provenance']))
        self.entries = list(entries.values())
