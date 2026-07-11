"""
Deep web search — Tavily API.

Confirmed before building this: no web search API exists anywhere in this
codebase or NaviCore's (grepped both — nothing). This is a genuinely new
external dependency, unlike Bedrock/Teams which reused existing access.

One function, one job: take a query, return a handful of real web results
with enough text to summarize. Same style as graph_teams.py — plain
urllib + a certifi SSL context (macOS python.org builds fail TLS to
external hosts without it, same root cause fixed there and in
token_validation.py), no SDK dependency for one POST call.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from app.core.logging import log

_ENDPOINT = 'https://api.tavily.com/search'

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:                                    # pragma: no cover
    _SSL_CTX = ssl.create_default_context()


def is_configured() -> bool:
    return bool(os.environ.get('TAVILY_API_KEY', '').strip())


def search(query: str, *, max_results: int = 5) -> dict:
    """Returns {results: [{title, url, content}], answer} or {error: str}.
    NEVER raises — every failure mode (no key, network, bad response)
    comes back as a clear string so callers can surface it in a draft
    card instead of crashing."""
    query = (query or '').strip()
    if not query:
        return {'error': 'empty search query'}
    api_key = os.environ.get('TAVILY_API_KEY', '').strip()
    if not api_key:
        return {'error': 'web search is not configured — set TAVILY_API_KEY in backend/.env '
                         '(https://tavily.com — free tier available)'}

    body = json.dumps({
        'api_key': api_key,
        'query': query,
        'search_depth': 'advanced',
        'max_results': max(1, min(max_results, 10)),
        'include_answer': False,
    }).encode('utf-8')
    req = urllib.request.Request(
        _ENDPOINT, data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:300]
        log.warning('[WEB_SEARCH] Tavily HTTP %s: %s', e.code, detail)
        if e.code == 401:
            return {'error': 'Tavily rejected the API key (401) — check TAVILY_API_KEY'}
        if e.code == 429:
            return {'error': 'Tavily rate limit hit (429) — try again shortly'}
        return {'error': f'Tavily error (HTTP {e.code}): {detail}'}
    except Exception as e:
        log.warning('[WEB_SEARCH] request failed (%s): %s', e.__class__.__name__, e)
        return {'error': f'web search request failed: {e.__class__.__name__}: {e}'}

    results = []
    for r in (data.get('results') or [])[:max_results]:
        results.append({
            'title': str(r.get('title') or '')[:200],
            'url': str(r.get('url') or ''),
            'content': str(r.get('content') or '')[:3000],
        })
    if not results:
        return {'error': f'no web results found for "{query[:80]}"'}
    log.info('[WEB_SEARCH] "%s" -> %d results', query[:80], len(results))
    return {'results': results, 'answer': data.get('answer') or ''}
