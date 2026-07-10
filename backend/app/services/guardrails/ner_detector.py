"""
LLM-based Named Entity Recognition for unstructured guardrail categories
(person, company, client, vendor, team, project) that regex can't catch.

Design notes
------------

* Runs ONLY when at least one of the unstructured categories is on AND
  controls.block_llm is on. Otherwise it's a no-op so workflows with
  Open mode pay nothing.

* One pass per unique input text per workflow run. Keyed by SHA256
  of (text, sorted(enabled_categories)) and cached on the AliasVault
  for the run. The result for a 200 KB merged summary is reused across
  every subsequent run_llm() call inside the same context window.

* MUST NOT recurse into this module from inside run_llm() (infinite
  loop). Calls the underlying llm_service.complete() directly with the
  guardrails tag so it stays observable in operator logs but skips the
  masking wrapper.

* Response is parsed defensively: bad JSON, missing fields, hallucinated
  categories all degrade to "no spans" rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

from app.services.guardrails.regex_detectors import Span


log = logging.getLogger('app.guardrails.ner')


# Maps the unstructured category id -> the label we want the LLM to
# tag with. Keeping the label space tight (one word, lowercase) cuts
# hallucination dramatically vs free-text labels.
_NER_CATEGORIES = {
    'person':  'person',
    'company': 'company',
    'client':  'client',
    'vendor':  'vendor',
    'team':    'team',
    'project': 'project',
}


_PROMPT_TEMPLATE = """You are a privacy classifier. Extract every literal mention of the
following entity kinds from the INPUT TEXT and return STRICT JSON.

Kinds to extract: {kinds}

Rules:
- Return ONLY the JSON object - no prose, no markdown, no code fences.
- For each match, return the EXACT substring as it appears in INPUT TEXT.
- Do not invent entities that are not literally present.
- Do not include common words ("user", "system", "engineer") unless
  they are clearly a proper noun reference.
- Deduplicate identical surface strings within a kind.

Schema:
{{
  "entities": [
    {{"kind": "<one of {kinds}>", "value": "<exact substring>"}}
  ]
}}

INPUT TEXT:
\"\"\"
{text}
\"\"\"
"""


# Tight cap on how much of a single text we send to the NER LLM. The
# full merged summaries reach ~200 KB and the NER pass doesn't need
# every token - a representative slice catches the entity inventory
# and the regex detectors handle the rest of the document anyway.
_NER_MAX_CHARS = 60_000


def _ner_cache_key(text: str, categories: Iterable[str]) -> str:
    h = hashlib.sha256()
    h.update(text.encode('utf-8', errors='ignore'))
    h.update(b'|')
    h.update(','.join(sorted(categories)).encode('ascii'))
    return h.hexdigest()


def _enabled_ner_categories(enabled: Iterable[str]) -> List[str]:
    s = set(enabled or ())
    return [k for k in _NER_CATEGORIES if k in s]


def _truncate(text: str) -> str:
    if len(text) <= _NER_MAX_CHARS:
        return text
    # head + tail keeps both the front-matter ("Project: X by Acme")
    # and the trailing references / signature ("- John Smith").
    half = _NER_MAX_CHARS // 2
    return text[:half] + '\n\n... [truncated for NER pass] ...\n\n' + text[-half:]


def _call_llm(prompt: str) -> Optional[str]:
    """Call the underlying llm_service.complete() directly so we don't
    re-enter the masking wrapper installed at run_llm()."""
    try:
        from app.services import llm_service
    except Exception as e:                                  # pragma: no cover
        log.warning('NER llm_service import failed: %s', e)
        return None

    try:
        return llm_service.complete(
            prompt,
            tag='[GUARDRAILS-NER]',
            timeout=90,
        )
    except Exception as e:
        # LLM hiccups must NEVER block the underlying workflow run.
        # Degrade silently: regex spans alone get applied this turn.
        log.warning('NER call failed (%s) - falling back to regex-only masking', e)
        return None


def _parse_response(raw: str) -> List[Tuple[str, str]]:
    """Return [(kind, value), ...] from the model's JSON. Defensive."""
    if not raw:
        return []

    # Strip accidental code fences before json.loads.
    stripped = raw.strip()
    if stripped.startswith('```'):
        stripped = re.sub(r'^```(?:json)?\s*', '', stripped)
        stripped = re.sub(r'\s*```\s*$', '', stripped)

    try:
        payload = json.loads(stripped)
    except Exception:
        # Last-ditch: pluck the first {...} block.
        m = re.search(r'\{.*\}', stripped, re.S)
        if not m:
            return []
        try:
            payload = json.loads(m.group(0))
        except Exception:
            return []

    items = payload.get('entities') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    out: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        kind  = str(it.get('kind') or '').strip().lower()
        value = str(it.get('value') or '').strip()
        if not kind or not value or kind not in _NER_CATEGORIES:
            continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        out.append((kind, value))
    return out


def detect(text: str, enabled_categories: Iterable[str],
           cache: Optional[Dict[str, List[Tuple[str, str]]]] = None) -> List[Span]:
    """Run NER (or read from cache) and return regex-detector-style spans.

    `cache` is the AliasVault.ner_cache dict; pass it through so repeat
    calls inside one workflow run reuse the previous LLM result.
    """
    if not text or not text.strip():
        return []

    kinds = _enabled_ner_categories(enabled_categories)
    if not kinds:
        return []

    key = _ner_cache_key(text, kinds)
    if cache is not None and key in cache:
        entities = cache[key]
    else:
        truncated = _truncate(text)
        prompt = _PROMPT_TEMPLATE.format(kinds=', '.join(kinds), text=truncated)
        raw = _call_llm(prompt)
        entities = _parse_response(raw) if raw else []
        if cache is not None:
            cache[key] = entities

    # Project the (kind, value) list into spans by searching the
    # ORIGINAL untruncated text for every literal occurrence.
    spans: List[Span] = []
    for kind, value in entities:
        if not value:
            continue
        # Escape so 'C++' or 'A.B' aren't treated as regex meta.
        pat = re.compile(r'(?<![\w])' + re.escape(value) + r'(?![\w])')
        for m in pat.finditer(text):
            spans.append((m.start(), m.end(), kind, m.group(0)))
    return spans
