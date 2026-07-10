"""
Compiled regex detectors for structured PII.

Each detector returns a list of (start, end, category, raw) spans for
a given text. The masker merges these with the LLM NER results, sorts
by start offset, drops overlaps (longest wins), then walks the text in
reverse to produce the masked string in a single pass.

Patterns are conservative on purpose. False positives in this module
turn into masked content that confuses downstream LLM reasoning, so
we err on the precise side and let the NER pre-pass catch the
ambiguous cases.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

Span = Tuple[int, int, str, str]


_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
)

# E.164 ish + common North-American + Indian formats. Avoids matching
# arbitrary 7+ digit numbers (which would catch ticket ids, line numbers,
# stack traces) by requiring either a + prefix, parentheses, or a hyphen
# separator within the run.
_PHONE_RE = re.compile(
    r'(?<![\w.])('
    r'\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}'
    r'|\(\d{3,4}\)[\s.-]?\d{3,4}[\s.-]?\d{3,4}'
    r')(?!\w)'
)

# Employee IDs - typical enterprise shapes: EMP12345, E-12345, NK-12345,
# 6+ contiguous digits prefixed by a known token. Conservative to avoid
# matching FR-001 (FRD section ids) or ticket ids handled separately.
_EMPLOYEE_ID_RE = re.compile(
    r'\b(EMP|EID|E|NK|EMP_NO|EMPLID)[-_]?\d{4,8}\b',
    re.IGNORECASE,
)

# Standalone URLs. Excludes trailing punctuation that's almost always
# sentence punctuation rather than part of the URL.
_URL_RE = re.compile(
    r'\b(?:https?://|www\.)[^\s<>"\'`]+'
)

# IPv4. IPv6 is intentionally skipped - the patterns alias many things
# that look like hex-coded ids and the false-positive cost is high.
_IP_RE = re.compile(
    r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b'
)

# API keys / tokens: capture obvious provider shapes (sk-, ghp_, AIza,
# AKIA-prefixed AWS, bearer tokens). Generic high-entropy strings are
# excluded - too many false positives in code blocks.
_API_KEY_RE = re.compile(
    r'\b('
    r'sk-[A-Za-z0-9]{20,}'                   # OpenAI
    r'|ghp_[A-Za-z0-9]{20,}'                 # GitHub PAT
    r'|gho_[A-Za-z0-9]{20,}'                 # GitHub OAuth
    r'|AIza[A-Za-z0-9_-]{30,}'               # Google
    r'|AKIA[A-Z0-9]{16}'                     # AWS access key
    r'|xox[baprs]-[A-Za-z0-9-]{10,}'         # Slack
    r')\b'
)
# Bearer header tokens (handled separately for log lines).
_BEARER_RE = re.compile(
    r'(?i)Bearer\s+[A-Za-z0-9._\-]{20,}'
)

# Contract IDs - common forms: CTR-2024-001, CON_12345, SOW-1234.
_CONTRACT_ID_RE = re.compile(
    r'\b(?:CTR|CON|MSA|SOW|NDA|PO)[-_/]?\d{3,8}(?:[-_/]\d{1,4})?\b'
)

# Ticket numbers - Jira, ServiceNow, Linear style.
_TICKET_RE = re.compile(
    r'\b('
    r'[A-Z][A-Z0-9]{1,9}-\d{1,7}'      # PROJ-1234, ABC123-5
    r'|INC\d{6,10}'                    # ServiceNow INC...
    r'|TASK-\d{1,7}'
    r')\b'
)

# Repository names - org/repo from GitHub / GitLab / Azure DevOps URLs
# OR bare org/repo references in prose.
_REPO_RE = re.compile(
    r'\b([A-Za-z0-9_.-]{1,40}/[A-Za-z0-9_.-]{1,80})(?:\.git)?\b'
)


# Each (category -> regex) pair. The masker iterates this map only
# for categories that are switched on in the active config.
DETECTORS = [
    ('email',       _EMAIL_RE),
    ('phone',       _PHONE_RE),
    ('employee_id', _EMPLOYEE_ID_RE),
    ('url',         _URL_RE),
    ('ip',          _IP_RE),
    ('api_key',     _API_KEY_RE),
    ('api_key',     _BEARER_RE),
    ('contract_id', _CONTRACT_ID_RE),
    ('ticket',      _TICKET_RE),
    ('repo',        _REPO_RE),
]


def detect(text: str, enabled_categories: Iterable[str]) -> List[Span]:
    """Return every span matched by any enabled regex detector.

    Spans are returned unsorted and may overlap; the masker dedups
    them. Detectors disabled in the config are skipped entirely so a
    workflow with only 'email' on doesn't pay the cost of every regex.
    """
    enabled = set(enabled_categories or ())
    out: List[Span] = []
    if not enabled or not text:
        return out

    for category, rx in DETECTORS:
        if category not in enabled:
            continue
        for m in rx.finditer(text):
            out.append((m.start(), m.end(), category, m.group(0)))
    return out
