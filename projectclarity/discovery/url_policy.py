"""Load operator defaults and apply domain rules before selection, DNS and redirects.

The whitelist is a starter catalogue unless whitelist-only mode is requested.
It does not certify evidence, bypass deny rules or grant access to private networks.
Rules are frozen into each request so edits cannot silently change an active run.
The terminal interface loads both files even when web search is disabled. Python API callers must
load/pass a policy explicitly; constructing URLPolicy alone does not read files.
Only source acquisition is controlled here, not all network traffic from DDGS.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

from projectclarity.research_schemas import URLPolicy
from projectclarity.discovery.search_engines import SUPPORTED_SEARCH_PROVIDERS

CONFIG_DIRECTORY = Path(__file__).resolve().parents[2] / "config"


def normalize_domain(value):
    """Validate a domain-only rule so URL paths and misleading suffixes cannot widen access.

    Args:
        value: Domain name, not a URL, path, port or wildcard. Case and a trailing
            DNS dot are normalized; international names use ASCII IDNA form.

    Returns:
        Lowercase IDNA host for exact/subdomain comparisons; no DNS lookup or
        reachability check. Invalid input raises ValueError or UnicodeError.
    """
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("Domain rules must be domain names")
    host = value.rstrip('.').lower().encode('idna').decode('ascii')
    if not host or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for c in host) or any(
            not part or part.startswith('-') or part.endswith('-') for part in host.split('.')):
        raise ValueError("Domain rules must not contain schemes, paths, ports or wildcards")
    return host


def load_url_configuration(blacklist, whitelist, *, whitelist_only=False):
    """Load editable policy files and provider/seed defaults without accessing their websites.

    Keeping the catalogue separate from the domain policy allows useful starting
    points without confining every investigation to those sites. All configured
    seed URLs are validated even when the caller does not opt to add them to a run.
    Provider selection and adding seeds remain the terminal interface's responsibility.

    Args:
        blacklist: Local path to the domain-per-line deny file. Blank lines and
            whole-line # comments are ignored; inline comments are not supported.
        whitelist: Local path to a strict JSON catalogue with domains, default_sources,
            search_providers and default_search_provider. Relative paths use the
            process working directory; neither argument is a remote URL.
        whitelist_only: If True, source and redirect hosts must match listed domains.
            If False (the default), only deny rules restrict domains. This does not
            confine the search adapter's internal requests to the listed domains.

    Returns:
        A pair: (URLPolicy holding normalized deny/allow rules and the mode flag,
        catalogue dictionary holding provider choices/default and seed URLs).
        This only reads files; it does not register sources or contact a provider.
        File errors raise OSError; invalid JSON, domains or catalogue values raise
        ValueError (including UnicodeError). The terminal interface reports these before research.
    """
    blocked = [normalize_domain(line.strip()) for line in Path(blacklist).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith('#')]
    catalogue = json.loads(Path(whitelist).read_text())
    if set(catalogue) != {'domains', 'default_sources', 'search_providers', 'default_search_provider'}:
        raise ValueError("Whitelist must define domains, default_sources, search_providers and default_search_provider")
    for key in ('domains', 'default_sources', 'search_providers'):
        if not isinstance(catalogue[key], list) or not all(isinstance(x, str) for x in catalogue[key]):
            raise ValueError("Whitelist lists must contain strings")
    providers = set(catalogue['search_providers'])
    if not providers or providers - SUPPORTED_SEARCH_PROVIDERS or catalogue['default_search_provider'] not in providers:
        raise ValueError("Configure supported search providers and a listed default: " + ', '.join(sorted(SUPPORTED_SEARCH_PROVIDERS)))
    policy = URLPolicy(blocked_domains=blocked,
        allowed_domains=[normalize_domain(d) for d in catalogue['domains']], whitelist_only=whitelist_only)
    if whitelist_only and not policy.allowed_domains:
        raise ValueError("Whitelist-only mode requires at least one domain")
    for url in catalogue['default_sources']:
        if urlsplit(url).scheme not in {'http', 'https'} or url_denial(url, policy):
            raise ValueError("Default sources must be HTTP(S) URL references permitted by domain policy")
    return policy, catalogue


def url_denial(url, policy):
    """Check exact hosts and proper subdomains without substring-based overblocking.

    Args:
        url: Candidate source or redirect URL; this check performs no DNS or network I/O.
        policy: Run's snapshotted URLPolicy. allowed_domains is ignored unless
            whitelist_only is True; an enabled empty allowlist denies every host.

    Returns:
        'invalid_url_host' for an unparseable host, 'blacklisted_domain' for a deny
        match (even if also allowed), 'outside_whitelist' for a missing required
        allow match, or None when permitted by this layer. Exact hosts and their
        subdomains match; unrelated substring matches do not. Public-address,
        credential, scheme, robots and transport checks still belong to retrieval.
    """
    try:
        host = normalize_domain(urlsplit(url).hostname or '')
    except (ValueError, UnicodeError):
        return 'invalid_url_host'
    if any(host == d or host.endswith('.' + d) for d in policy.blocked_domains):
        return 'blacklisted_domain'
    if policy.whitelist_only and not any(host == d or host.endswith('.' + d) for d in policy.allowed_domains):
        return 'outside_whitelist'
    return None
