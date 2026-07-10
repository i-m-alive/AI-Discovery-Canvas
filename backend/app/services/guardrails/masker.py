"""
Composition layer that turns the regex + NER detectors plus an alias
vault into a single mask() call.

Span resolution rule
--------------------

Detectors can overlap (e.g. an email is also matched by the URL
regex). The masker:

  1. Sorts spans by start offset, then by length descending.
  2. Walks once, accepting the first span for each position and
     skipping any span that starts inside an already-accepted span
     ("longest-wins, leftmost-first").
  3. Replaces accepted spans back-to-front using the vault, so
     offsets in the original string remain valid until each rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing      import Dict, Iterable, List, Optional, Tuple

from app.services.guardrails.regex_detectors import Span, detect as detect_regex
from app.services.guardrails.ner_detector    import detect as detect_ner
from app.services.guardrails.vault           import AliasVault


@dataclass
class MaskReport:
    """Per-call summary the audit layer + log filter consume."""
    counts: Dict[str, int] = field(default_factory=dict)

    def add(self, category: str, n: int = 1) -> None:
        self.counts[category] = self.counts.get(category, 0) + n

    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return 'no masks applied'
        parts = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        return ', '.join(parts)


def _accept_spans(spans: List[Span]) -> List[Span]:
    if not spans:
        return []
    # leftmost-first, longest first
    spans = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
    accepted: List[Span] = []
    cursor = -1
    for s in spans:
        if s[0] < cursor:
            continue
        accepted.append(s)
        cursor = s[1]
    return accepted


def _apply(text: str, spans: List[Span], vault: AliasVault, report: MaskReport) -> str:
    if not spans:
        return text
    # Walk back-to-front so earlier offsets stay valid.
    out = text
    for start, end, category, raw in reversed(spans):
        alias = vault.alias_for(category, raw)
        out = out[:start] + alias + out[end:]
        report.add(category)
    return out


def mask(text: str,
         *,
         enabled_categories: Iterable[str],
         vault: AliasVault,
         use_ner: bool = True,
         ner_cache: Optional[Dict] = None) -> Tuple[str, MaskReport]:
    """Return the masked string + a per-category MaskReport.

    `use_ner` lets callers skip the LLM-based NER pass for cheap
    log-line masking (where a regex-only sweep is more than enough and
    a per-line LLM call would be unacceptable).
    """
    report = MaskReport()
    if not text or not enabled_categories:
        return text, report

    spans: List[Span] = []
    spans.extend(detect_regex(text, enabled_categories))
    if use_ner:
        spans.extend(detect_ner(text, enabled_categories,
                                cache=ner_cache if ner_cache is not None else vault.ner_cache))

    accepted = _accept_spans(spans)
    return _apply(text, accepted, vault, report), report


def mask_dict(obj, *, enabled_categories: Iterable[str], vault: AliasVault,
              use_ner: bool = False) -> object:
    """Recursively mask every string value in a JSON-ish structure.

    Used by the entity-extraction post-processing path: the LLM has
    already produced a {technologies: [...], people: [...]} dict and
    we want to mask the leaf strings before they reach the graph.

    Defaults `use_ner` to False because the dict has already been
    constructed from a masked prompt - the NER inventory is in the
    vault and a second pass would burn another LLM call for nothing.
    """
    if isinstance(obj, str):
        masked, _ = mask(obj, enabled_categories=enabled_categories,
                         vault=vault, use_ner=use_ner)
        return masked
    if isinstance(obj, list):
        return [mask_dict(x, enabled_categories=enabled_categories,
                          vault=vault, use_ner=use_ner) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_dict(v, enabled_categories=enabled_categories,
                             vault=vault, use_ner=use_ner) for k, v in obj.items()}
    return obj
