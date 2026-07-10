"""
Semantic chunking.

Splits a document into retrieval-sized chunks that respect structure
(headings, paragraphs, list blocks) instead of slicing mid-sentence at a
fixed byte offset. The packer then groups those semantic units up to a
token budget with a small overlap so a fact that straddles a boundary is
still retrievable from either side.

Token counting reuses the same ``cl100k_base`` encoder ``llm_service``
uses; if tiktoken is unavailable it falls back to a chars/4 heuristic so
this module never hard-fails.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.services.rag import config

try:
    import tiktoken
    _ENC = tiktoken.get_encoding('cl100k_base')
except Exception:                                   # pragma: no cover
    _ENC = None


_TAG_RE       = re.compile(r'<[^>]+>')
_WS_RE        = re.compile(r'[ \t ]+')
_MULTINL_RE   = re.compile(r'\n{3,}')
# A "semantic break": a blank line, or a line that looks like a heading
# (markdown ###, HTML headings already stripped to text, ALL-CAPS lines,
# or numbered/bulleted section starts).
_PARA_SPLIT   = re.compile(r'\n\s*\n')


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is None:
        return max(1, len(text) // 4)
    try:
        return len(_ENC.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def html_to_text(html: str) -> str:
    """Cheap HTML → text for generated documents (FRD/Technical/SOP).
    Drops tags, collapses whitespace, keeps paragraph breaks so the
    semantic splitter still sees structure. Not a full parser — good
    enough to embed."""
    if not html:
        return ''
    # Turn block-level closers into paragraph breaks before stripping tags
    # so headings/paragraphs/list items don't collapse into one line.
    s = re.sub(r'(?i)</(p|div|li|h[1-6]|tr|section|article|br\s*/?)>', '\n\n', html)
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = _TAG_RE.sub('', s)
    # Unescape the handful of entities that actually show up.
    for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                 ('&quot;', '"'), ('&#39;', "'"), ('&nbsp;', ' ')):
        s = s.replace(a, b)
    s = _WS_RE.sub(' ', s)
    s = _MULTINL_RE.sub('\n\n', s)
    return s.strip()


def _truncate_tokens(text: str, max_tokens: int) -> str:
    """Hard cap a single unit so one giant paragraph can't exceed the
    per-input embedding ceiling."""
    if _ENC is None:
        return text[: max_tokens * 4]
    ids = _ENC.encode(text)
    if len(ids) <= max_tokens:
        return text
    return _ENC.decode(ids[:max_tokens])


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0 or not text:
        return ''
    if _ENC is None:
        return text[-overlap_tokens * 4:]
    ids = _ENC.encode(text)
    return _ENC.decode(ids[-overlap_tokens:]) if ids else ''


def chunk_text(text: str,
               *,
               target_tokens: int | None = None,
               overlap_tokens: int | None = None,
               max_input_tokens: int | None = None) -> list[str]:
    """Split ``text`` into a list of semantic chunks.

    Algorithm: split into paragraphs on blank lines, then greedily pack
    paragraphs into a chunk until adding the next would exceed
    ``target_tokens``. Start the next chunk with a token-overlap tail of
    the previous one. Oversized single paragraphs are hard-split.
    """
    target  = target_tokens  or config.CHUNK_TARGET_TOKENS
    overlap = overlap_tokens  if overlap_tokens is not None else config.CHUNK_OVERLAP_TOKENS
    cap     = max_input_tokens or config.EMBED_MAX_INPUT_TOKENS
    text = (text or '').strip()
    if not text:
        return []

    paras = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    if not paras:
        return []

    chunks: list[str] = []
    cur: list[str] = []
    cur_tokens = 0

    def _flush():
        nonlocal cur, cur_tokens
        if cur:
            joined = '\n\n'.join(cur).strip()
            if joined:
                chunks.append(_truncate_tokens(joined, cap))
            cur, cur_tokens = [], 0

    for para in paras:
        ptoks = count_tokens(para)
        # A single paragraph larger than the target becomes its own
        # (possibly multiple) chunk(s).
        if ptoks > target:
            _flush()
            for piece in _split_oversized(para, target, cap):
                chunks.append(piece)
            continue
        if cur_tokens + ptoks > target and cur:
            tail = _overlap_tail('\n\n'.join(cur), overlap)
            _flush()
            if tail:
                cur.append(tail)
                cur_tokens += count_tokens(tail)
        cur.append(para)
        cur_tokens += ptoks
    _flush()

    # Drop chunks too small to carry signal (unless it's the only one).
    if len(chunks) > 1:
        chunks = [c for c in chunks if count_tokens(c) >= config.CHUNK_MIN_TOKENS]
    return chunks


def _split_oversized(para: str, target: int, cap: int) -> Iterable[str]:
    """Sentence-pack an oversized paragraph into target-sized pieces."""
    sentences = re.split(r'(?<=[.!?])\s+', para)
    cur, cur_t = [], 0
    for s in sentences:
        st = count_tokens(s)
        if st > target:                       # a monster sentence — hard cut
            if cur:
                yield _truncate_tokens(' '.join(cur), cap)
                cur, cur_t = [], 0
            yield _truncate_tokens(s, cap)
            continue
        if cur_t + st > target and cur:
            yield _truncate_tokens(' '.join(cur), cap)
            cur, cur_t = [], 0
        cur.append(s)
        cur_t += st
    if cur:
        yield _truncate_tokens(' '.join(cur), cap)
