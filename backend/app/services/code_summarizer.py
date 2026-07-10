"""
Whole-codebase + whole-sources understanding via a cached, hybrid rolling summary.

WHY THIS EXISTS
---------------
The agentic analysis/impact must reflect the ENTIRE application, not a skim. The
clone is ephemeral (it exists only inside ``stack_analyzer.analyze_from_url`` before
the temp dir is deleted), and a large repo (3,700+ files) does not fit in one prompt.
So we read everything that matters and distil it into ONE coherent understanding that
DOES fit, using a rolling summary that carries context forward while staying lean.

STRUCTURE — hybrid (chosen deliberately; see the note in CLAUDE.md):
  Phase A — per-CHUNK extraction (INDEPENDENT, content-addressed, CACHED).
      Files are grouped by module (top path segments) and packed into chunks of
      ~``_CHUNK_CHARS``. Each chunk is summarised on its own into a dense factual
      digest. Because the extraction depends ONLY on the chunk's bytes, it is cached
      by ``sha256(chunk)`` — a re-run re-processes ONLY changed chunks (i.e. changed
      files), reusing every unchanged one. This is the expensive part and it is the
      part that parallelises and caches.
  Phase B — rolling MERGE (sequential, cheap, prune-bounded).
      Chunk digests are rolled together within a module → a module summary, then the
      module summaries are rolled together → the application summary. Each merge feeds
      the RUNNING SUMMARY SO FAR + the next digests and asks for an updated, PRUNED
      summary, so context carries forward and the summary never grows unbounded.

Pure sequential rolling-summary (one call per chunk, each depending on the previous)
is leaner per call but fully serial AND uncacheable (a change early invalidates every
later step). The hybrid keeps the context-carrying benefit, parallelises Phase A,
and makes caching content-addressed — the right trade for repos of any size.

Sources (docs/tickets/telemetry the user attached) are folded into the SAME running
summary via ``extend_summary`` so the final understanding spans code + all sources.

Never raises — returns whatever it has (including '' if the LLM is unavailable); the
caller falls back to the raw key-excerpt sample and the existing signal logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("navicore.code_summarizer")

# ── Tunables (env-overridable; defaults sized for big repos) ──────────────────
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default

_CHUNK_CHARS      = _int_env("CODE_SUMMARY_CHUNK_CHARS", 28_000)   # ~7k tokens/chunk
_PER_FILE_CHARS   = _int_env("CODE_SUMMARY_PER_FILE_CHARS", 16_000)  # head of a huge file
_MAX_CHUNKS       = _int_env("CODE_SUMMARY_MAX_CHUNKS", 600)        # safety ceiling (~16 MB)
_MAX_WORKERS      = max(2, min(_int_env("CODE_SUMMARY_MAX_WORKERS", 8), 16))
_CHUNK_DIGEST_CHARS  = 1_000        # bound on a single chunk digest
_MODULE_SUMMARY_CHARS = 1_600       # bound on a module summary
_APP_SUMMARY_CHARS   = _int_env("CODE_SUMMARY_APP_CHARS", 9_000)    # the final running summary
_MERGE_FANIN        = 6             # digests merged per roll step

# Directories that never carry first-party application logic.
_SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target", ".git", ".idea",
    ".vscode", "__pycache__", ".pytest_cache", ".mypy_cache", ".next", ".nuxt",
    "venv", ".venv", "env", "site-packages", "bower_components", "coverage",
    "__snapshots__", ".terraform", "bin", "obj", ".gradle", ".cache", "deps",
    "third_party", "thirdparty", "external", "Pods", ".tox", ".eggs",
    # Low-signal for understanding what the app DOES: schema-evolution noise and
    # i18n catalogs (the models/handlers already convey the data model + behaviour).
    "migrations", "locale", "locales", "fixtures",
}
# Lockfiles / generated manifests — enumerate pinned versions, not logic.
_LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock",
    "composer.lock", "cargo.lock", "go.sum", "gemfile.lock", "manifest.toml",
    "packages.lock.json", "podfile.lock", "mix.lock", "flake.lock",
}
# Binary / asset / large-data extensions — skip (not text, or non-signal data).
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".pdf",
    ".zip", ".gz", ".tar", ".tgz", ".bz2", ".7z", ".rar", ".jar", ".war", ".class",
    ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a", ".lib", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi",
    ".pyc", ".pyo", ".whl", ".egg", ".db", ".sqlite", ".parquet", ".avro",
    ".csv", ".tsv", ".xlsx", ".xls", ".npz", ".npy", ".pkl", ".pt", ".pth",
    ".onnx", ".h5", ".bin", ".lock", ".map", ".min.js", ".min.css",
    ".po", ".pot", ".mo", ".snap", ".woff2",
}
_MAX_FILE_BYTES = 1_500_000  # skip individual files larger than this (data dumps)


# ── Disk cache (content-addressed) ────────────────────────────────────────────
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, str] | None = None
_CACHE_DIRTY = False
_MAX_CACHE_ENTRIES = 50_000


def _cache_path() -> Path:
    override = os.environ.get("RAG_DATA_DIR", "").strip()
    base = Path(override) if override else (Path(__file__).resolve().parents[2] / "data" / "rag")
    return base / "code_summary_cache.json"


def _cache_load() -> dict[str, str]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            p = _cache_path()
            if p.exists():
                _CACHE = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(_CACHE, dict):
                    _CACHE = {}
            else:
                _CACHE = {}
        except Exception as exc:  # noqa: BLE001
            log.warning("[CODE-SUMMARY] cache load failed: %s", exc)
            _CACHE = {}
    return _CACHE


def _cache_get(key: str) -> str | None:
    return _cache_load().get(key)


def _cache_put(key: str, val: str) -> None:
    global _CACHE_DIRTY
    c = _cache_load()
    with _CACHE_LOCK:
        c[key] = val
        _CACHE_DIRTY = True


def _cache_flush() -> None:
    global _CACHE_DIRTY
    with _CACHE_LOCK:
        if not _CACHE_DIRTY or _CACHE is None:
            return
        cache = _CACHE
        # Bound the cache — drop oldest insertions if it grows pathologically.
        if len(cache) > _MAX_CACHE_ENTRIES:
            for k in list(cache.keys())[: len(cache) - _MAX_CACHE_ENTRIES]:
                cache.pop(k, None)
        try:
            p = _cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
            _CACHE_DIRTY = False
        except Exception as exc:  # noqa: BLE001
            log.warning("[CODE-SUMMARY] cache flush failed: %s", exc)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()


# ── Project-level summary cache ───────────────────────────────────────────────
# The chunk cache above makes Phase A (per-file extraction) reuse unchanged files.
# This SECOND cache stores the FINAL whole-codebase summary keyed by project +
# source type, validated by a content fingerprint. When a project is re-run and
# its source content is unchanged, the ENTIRE pass (Phase A + the slow Phase B
# rollup) is skipped and the stored summary is reused. Lives on local disk next
# to the chunk cache: data/rag/project_summary_cache.json (override RAG_DATA_DIR).
_PROJ_CACHE_LOCK = threading.Lock()
_PROJ_CACHE: dict | None = None


def _proj_cache_path() -> Path:
    override = os.environ.get("RAG_DATA_DIR", "").strip()
    base = Path(override) if override else (Path(__file__).resolve().parents[2] / "data" / "rag")
    return base / "project_summary_cache.json"


def _proj_cache_load() -> dict:
    global _PROJ_CACHE
    if _PROJ_CACHE is not None:
        return _PROJ_CACHE
    with _PROJ_CACHE_LOCK:
        if _PROJ_CACHE is not None:
            return _PROJ_CACHE
        try:
            p = _proj_cache_path()
            _PROJ_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if not isinstance(_PROJ_CACHE, dict):
                _PROJ_CACHE = {}
        except Exception as exc:  # noqa: BLE001
            log.warning("[CODE-SUMMARY] project cache load failed: %s", exc)
            _PROJ_CACHE = {}
    return _PROJ_CACHE


def _proj_cache_get(key: str) -> dict | None:
    return _proj_cache_load().get(key)


def _proj_cache_put(key: str, entry: dict) -> None:
    c = _proj_cache_load()
    with _PROJ_CACHE_LOCK:
        c[key] = entry
        try:
            p = _proj_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            log.warning("[CODE-SUMMARY] project cache flush failed: %s", exc)


# ── Public project-cache API (used by modernize analyze + impact paths) ───────
def _normalize_project_name(project_name: str) -> str:
    """Stable, case/whitespace-insensitive form of a PROJECT name for cache
    keying, so the SAME project resolves to the SAME cache entry no matter how its
    name is spelled across subsystems — Modernization Analyze passes its in-process
    project name, the Disruption "Deep Dive" passes the Neo4j project node's name,
    and the two can differ only by case/whitespace (e.g. a canonical-deduped node
    stored as 'orders engine' vs an in-process 'Orders Engine'). Deliberately a
    LIGHT normalization (casefold + whitespace-collapse) — NOT the tech-entity
    `knowledge_graph.canonical_name`, which strips version tails and applies tech
    aliases (JS→javascript, 'Python 3.11'→'python') and would mangle/over-merge
    project names."""
    return re.sub(r"\s+", " ", (project_name or "unknown").strip()).lower()


def project_cache_key(project_name: str, source_type: str = "repository") -> str:
    """Canonical cache key — keyed by NORMALIZED PROJECT NAME + source type, so the
    same project reuses its stored summary across runs, restarts AND subsystems.
    Both Modernization (`modernize._capture_understanding` /
    `build_incremental_understanding`) and the Disruption Deep Dive
    (`modernize.deep_understanding_for_project`) build keys through THIS function,
    so they share entries bidirectionally. `source_type` is preserved verbatim
    (it carries the per-source content identity for non-repo sources)."""
    return f"{_normalize_project_name(project_name)}::{source_type}"


def load_project_summary(cache_key: str) -> dict | None:
    """Return the stored summary entry {summary, stats, skipped_note, fingerprint}
    for a project, or None. Logs HIT so reuse is visible in the terminal."""
    if not cache_key:
        return None
    ent = _proj_cache_get(cache_key)
    if ent and (ent.get("summary") or "").strip():
        print(f"[CODE-SUMMARY] project cache HIT for '{cache_key}' "
              f"({len(ent.get('summary') or '')} chars) at {_proj_cache_path()}", flush=True)
        log.info("[CODE-SUMMARY] project cache HIT for '%s'", cache_key)
        return ent
    print(f"[CODE-SUMMARY] project cache MISS for '{cache_key}'", flush=True)
    return None


def drop_project_summary(cache_key: str) -> bool:
    """Remove a stored project/source summary (used when a source is deleted from
    the project so its summary leaves the combined understanding). Returns True if
    an entry was removed. Never raises."""
    if not cache_key:
        return False
    c = _proj_cache_load()
    with _PROJ_CACHE_LOCK:
        if cache_key not in c:
            return False
        c.pop(cache_key, None)
        try:
            p = _proj_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            log.warning("[CODE-SUMMARY] project cache drop-flush failed: %s", exc)
    return True


def all_project_cache_keys() -> list[str]:
    """Snapshot of every key currently in the project summary cache (used to find
    summaries of sources that were removed from a project)."""
    try:
        return list(_proj_cache_load().keys())
    except Exception:
        return []


def store_project_summary(cache_key: str, summary: str, *, fingerprint: str = "",
                          stats: dict | None = None, skipped_note: str = "") -> None:
    """Persist a project's whole-codebase summary to the on-disk project cache,
    keyed by project name + source type. Idempotent; logs the write."""
    if not cache_key or not (summary or "").strip():
        return
    _proj_cache_put(cache_key, {"fingerprint": fingerprint or "", "summary": summary,
                                "stats": stats or {}, "skipped_note": skipped_note})
    print(f"[CODE-SUMMARY] STORED project summary '{cache_key}' ({len(summary)} chars) "
          f"at {_proj_cache_path()}", flush=True)
    log.info("[CODE-SUMMARY] stored project summary '%s' (%d chars)", cache_key, len(summary))


# ── Prompts ───────────────────────────────────────────────────────────────────
_EXTRACT_SYSTEM = (
    "You are a senior engineer reverse-understanding an application from its source. "
    "Given a set of source files (full or head-truncated), write a DENSE, factual "
    "digest of what THIS code actually does. Capture: the capabilities/features it "
    "implements; the workflows and MULTI-STEP processes (especially anything a human "
    "currently drives manually); decision points / branching / rules / approvals; "
    "external integrations / APIs / services it calls; data models and data flows; and "
    "any existing AI / LLM / embedding / vector usage. Reference concrete file names, "
    "functions/classes and endpoints. Be specific and compact; skip boilerplate, "
    "imports and license headers. No preamble, no markdown headings."
)
_MERGE_SYSTEM = (
    "You maintain ONE running understanding of an entire application as its parts are "
    "summarised. You are given the RUNNING SUMMARY SO FAR and NEW COMPONENT DIGESTS. "
    "Produce an UPDATED running summary that integrates the new information into a "
    "single coherent picture of the WHOLE application. PRUNE aggressively: drop "
    "redundancy and low-signal detail so the summary stays lean and within the size "
    "budget, but NEVER lose load-bearing facts about capabilities, workflows, "
    "decision points, integrations, data flows, or AI usage. Organise by "
    "capability/subsystem and keep concrete file/function/endpoint names. Output ONLY "
    "the updated summary text — no preamble, no markdown fences."
)


def _complete(prompt: str, system: str, max_tokens: int, tag: str) -> str:
    from app.services.llm_service import complete
    try:
        return (complete(prompt, tag=tag, system=system, max_output_tokens=max_tokens) or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("[CODE-SUMMARY] %s call failed: %s", tag, exc)
        return ""


# ── File selection ─────────────────────────────────────────────────────────────
def _is_signal_file(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(seg in _SKIP_DIRS for seg in parts[:-1]):
        return False
    base = parts[-1].lower()
    if base in _LOCKFILES:
        return False
    if ".min." in base:
        return False
    ext = os.path.splitext(base)[1]
    if ext in _BINARY_EXTS:
        return False
    return True


def _module_of(rel: str) -> str:
    """Group key — the top one/two path segments. Files at the repo root group
    under '(root)'. Two segments keep monorepo packages distinct."""
    segs = rel.replace("\\", "/").split("/")
    if len(segs) == 1:
        return "(root)"
    return "/".join(segs[:2]) if len(segs) > 2 else segs[0]


def _read_text(fp: Path) -> str:
    try:
        if not fp.is_file() or fp.stat().st_size > _MAX_FILE_BYTES:
            return ""
        raw = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    # Heuristic binary guard: lots of replacement chars => not real text.
    if raw.count("�") > max(40, len(raw) // 50):
        return ""
    return raw


def _pack_chunks(repo_root: Path, files: list[str]) -> list[str]:
    """Pack a module's files (sorted) into chunks of ~_CHUNK_CHARS, each file
    prefixed with its path. A single oversized file is head-truncated."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for rel in sorted(files):
        text = _read_text(repo_root / rel)
        if not text.strip():
            continue
        body = text[:_PER_FILE_CHARS]
        if len(text) > _PER_FILE_CHARS:
            body += "\n…(file truncated)…"
        piece = f"\n\n### FILE: {rel.replace(chr(92), '/')}\n{body}"
        if cur_len + len(piece) > _CHUNK_CHARS and cur:
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(piece)
        cur_len += len(piece)
    if cur:
        chunks.append("".join(cur))
    return chunks


# ── Phase A: per-chunk extraction (cached, parallel) ───────────────────────────
def _extract_chunk(chunk_text: str) -> str:
    key = _hash(chunk_text)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    prompt = ("Summarise what the following source files do, per the system "
              f"instruction. Keep it under {_CHUNK_DIGEST_CHARS} characters.\n" + chunk_text)
    digest = _complete(prompt, _EXTRACT_SYSTEM, max_tokens=600, tag="[CODE-SUMMARY/EXTRACT]")
    digest = digest[:_CHUNK_DIGEST_CHARS]
    if digest:
        _cache_put(key, digest)
    return digest


# ── Phase B: rolling merge (sequential, prune-bounded) ─────────────────────────
def _roll(running: str, digests: list[str], *, budget: int, tag: str) -> str:
    """Fold ``digests`` into ``running`` in fan-in batches, pruning to ``budget``."""
    summary = (running or "").strip()
    batch: list[str] = []

    def flush() -> None:
        nonlocal summary, batch
        if not batch:
            return
        new_block = "\n\n".join(f"- {d}" for d in batch if d.strip())
        if not new_block:
            batch = []
            return
        prompt = (
            f"RUNNING SUMMARY SO FAR:\n{summary or '(empty — this is the first input)'}\n\n"
            f"NEW COMPONENT DIGESTS:\n{new_block}\n\n"
            f"Return the updated running summary, integrated and pruned, under {budget} characters."
        )
        merged = _complete(prompt, _MERGE_SYSTEM, max_tokens=min(4096, budget // 3 + 256),
                           tag=tag)
        if merged:
            summary = merged[:budget]
        batch = []

    for d in digests:
        if not d.strip():
            continue
        batch.append(d)
        if len(batch) >= _MERGE_FANIN:
            flush()
    flush()
    return summary


# ── Public API ─────────────────────────────────────────────────────────────────
def summarize_codebase(repo_path, file_list: list[str], progress=None,
                       cache_key: str | None = None) -> dict:
    """Build a coherent whole-codebase understanding from REAL file contents.

    Returns ``{"summary", "stats", "skipped_note"}``:
      summary      — the application understanding (<= _APP_SUMMARY_CHARS), or ''
      stats        — {files_total, files_summarized, modules, chunks, cached_chunks,
                      chunks_capped}
      skipped_note — human-readable note of what was excluded from summarisation

    ``progress`` (optional) is a callable invoked with short human-readable status
    strings as the (multi-minute, on large repos) summarisation advances, so a
    caller streaming SSE can show live progress instead of a frozen-looking screen.
    Never raises."""
    def _emit(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass
    repo_root = Path(repo_path)
    total = len(file_list)
    signal = [f for f in file_list if _is_signal_file(f)]
    skipped = total - len(signal)

    # Group by module so we can summarise within a module then roll modules up.
    modules: dict[str, list[str]] = {}
    for rel in signal:
        modules.setdefault(_module_of(rel), []).append(rel)

    # Build chunks per module (preserve module grouping for the roll).
    module_chunks: dict[str, list[str]] = {}
    all_chunks: list[tuple[str, str]] = []  # (module, chunk_text)
    for mod, files in modules.items():
        chs = _pack_chunks(repo_root, files)
        module_chunks[mod] = chs
        for c in chs:
            all_chunks.append((mod, c))

    chunks_capped = 0
    if len(all_chunks) > _MAX_CHUNKS:
        chunks_capped = len(all_chunks) - _MAX_CHUNKS
        # Keep the first _MAX_CHUNKS in (module, order) form; trim the tail.
        kept = set(id(c) for _, c in all_chunks[:_MAX_CHUNKS])
        all_chunks = all_chunks[:_MAX_CHUNKS]
        module_chunks = {m: [c for c in chs if id(c) in kept]
                         for m, chs in module_chunks.items()}
        module_chunks = {m: chs for m, chs in module_chunks.items() if chs}

    if not all_chunks:
        return {"summary": "", "stats": {"files_total": total, "files_summarized": 0,
                                         "modules": 0, "chunks": 0, "cached_chunks": 0,
                                         "chunks_capped": 0},
                "skipped_note": f"{skipped} non-signal file(s) skipped; no source content to summarise."}

    cached_before = sum(1 for _, c in all_chunks if _cache_get(_hash(c)) is not None)
    n_chunks = len(all_chunks)

    # ── Project-level reuse ──────────────────────────────────────────────────
    # Fingerprint the WHOLE source content (hash of every chunk's content hash).
    # If this project's content is unchanged since the last run, reuse the stored
    # final summary and skip the entire (slow) pass.
    fingerprint = _hash("|".join(sorted(_hash(c) for _, c in all_chunks)))
    print(f"[CODE-SUMMARY] START project={cache_key or '(unkeyed)'} "
          f"files={total} signal={len(signal)} chunks={n_chunks} "
          f"cached_chunks={cached_before} fingerprint={fingerprint[:12]}", flush=True)
    if cache_key:
        ent = _proj_cache_get(cache_key)
        if ent and ent.get("fingerprint") == fingerprint and (ent.get("summary") or "").strip():
            msg = (f"REUSED cached summary for project '{cache_key}' — source content "
                   f"unchanged ({n_chunks} chunks); skipping summarization entirely.")
            print(f"[CODE-SUMMARY] {msg}", flush=True)
            log.info("[CODE-SUMMARY] %s", msg)
            _emit(msg)
            st = dict(ent.get("stats") or {})
            st["from_cache"] = True
            return {"summary": ent["summary"], "stats": st,
                    "skipped_note": ent.get("skipped_note", ""),
                    "fingerprint": fingerprint}
        why = "no prior summary cached" if not ent else "source content CHANGED since last run"
        print(f"[CODE-SUMMARY] project '{cache_key}': {why} -> summarizing fresh "
              f"(reusing {cached_before}/{n_chunks} unchanged chunk summaries)", flush=True)

    _emit(f"Summarizing {n_chunks} code chunk(s) across {len(module_chunks)} module(s) "
          f"({cached_before} already cached)…")

    # Phase A — extract every chunk (parallel; cached hits return instantly).
    digest_by_chunk: dict[str, str] = {}
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {ex.submit(_extract_chunk, c): c for _, c in all_chunks}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    digest_by_chunk[id(c)] = fut.result()
                except Exception:  # noqa: BLE001
                    digest_by_chunk[id(c)] = ""
                done += 1
                if done % 25 == 0 or done == n_chunks:
                    _emit(f"Read & summarized {done}/{n_chunks} code chunks…")
    except Exception as exc:  # noqa: BLE001
        log.warning("[CODE-SUMMARY] extraction pool failed: %s", exc)

    _emit("Merging module summaries into a whole-application understanding…")

    # Phase B — roll within each module, then roll modules into the app summary.
    module_summaries: list[str] = []
    for mod, chs in module_chunks.items():
        digests = [digest_by_chunk.get(id(c), "") for c in chs]
        digests = [d for d in digests if d.strip()]
        if not digests:
            continue
        if len(digests) == 1:
            mod_sum = digests[0][:_MODULE_SUMMARY_CHARS]
        else:
            mod_sum = _roll("", digests, budget=_MODULE_SUMMARY_CHARS,
                            tag="[CODE-SUMMARY/MODULE]")
        if mod_sum.strip():
            module_summaries.append(f"[module {mod}] {mod_sum}")

    app_summary = _roll("", module_summaries, budget=_APP_SUMMARY_CHARS,
                        tag="[CODE-SUMMARY/APP]")
    _cache_flush()

    note_bits = [f"{skipped} non-signal file(s) skipped (vendored deps / build output / "
                 f"lockfiles / binaries / large data)"]
    if chunks_capped:
        note_bits.append(f"{chunks_capped} chunk(s) beyond the {_MAX_CHUNKS}-chunk ceiling "
                         f"were NOT summarised (very large repo)")
    stats = {"files_total": total, "files_summarized": len(signal) - 0,
             "modules": len(module_chunks), "chunks": len(all_chunks),
             "cached_chunks": cached_before, "chunks_capped": chunks_capped}
    log.info("[CODE-SUMMARY] %s files (%s signal, %s skipped) → %s modules, %s chunks "
             "(%s cached) → %s-char summary",
             total, len(signal), skipped, stats["modules"], stats["chunks"],
             cached_before, len(app_summary))

    # Store the final summary for this project so the next run reuses it wholesale
    # (skips Phase A + B) when the source content is unchanged.
    skipped_note = "; ".join(note_bits)
    if cache_key and app_summary.strip():
        _proj_cache_put(cache_key, {"fingerprint": fingerprint, "summary": app_summary,
                                    "stats": stats, "skipped_note": skipped_note})
        print(f"[CODE-SUMMARY] STORED summary for project '{cache_key}' "
              f"({len(app_summary)} chars) at {_proj_cache_path()} -> reused on next run",
              flush=True)
    print(f"[CODE-SUMMARY] DONE project={cache_key or '(unkeyed)'} -> "
          f"{len(app_summary)}-char summary from {stats['chunks']} chunks "
          f"({cached_before} chunk-cached)", flush=True)
    return {"summary": app_summary, "stats": stats, "skipped_note": skipped_note,
            "fingerprint": fingerprint}


def extend_summary(prior_summary: str, items: list[tuple[str, str]]) -> str:
    """Roll additional content (e.g. attached sources) INTO an existing running
    summary so the final understanding spans code + all sources. ``items`` is a
    list of (label, text). Each item is chunk-extracted (cached) then merged.
    Returns the extended summary (<= _APP_SUMMARY_CHARS). Never raises."""
    digests: list[str] = []
    try:
        labelled_chunks: list[str] = []
        for label, text in items:
            text = (text or "").strip()
            if not text:
                continue
            # One source may itself be large — pack into chunks the same way.
            packed: list[str] = []
            cur, cur_len = [], 0
            piece_header = f"\n\n### SOURCE: {label}\n"
            for i in range(0, len(text), _PER_FILE_CHARS):
                seg = piece_header + text[i:i + _PER_FILE_CHARS]
                if cur_len + len(seg) > _CHUNK_CHARS and cur:
                    packed.append("".join(cur)); cur, cur_len = [], 0
                cur.append(seg); cur_len += len(seg)
            if cur:
                packed.append("".join(cur))
            labelled_chunks.extend(packed)

        if not labelled_chunks:
            return (prior_summary or "").strip()

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            for d in ex.map(_extract_chunk, labelled_chunks):
                if d.strip():
                    digests.append(d)
        _cache_flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("[CODE-SUMMARY] source extend failed: %s", exc)
        return (prior_summary or "").strip()

    if not digests:
        return (prior_summary or "").strip()
    return _roll((prior_summary or "").strip(), digests,
                 budget=_APP_SUMMARY_CHARS, tag="[CODE-SUMMARY/SOURCES]")