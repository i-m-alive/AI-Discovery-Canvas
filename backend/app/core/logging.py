"""
Logging configuration.

Lifted out of the old monolithic server.py so every module gets a
consistent logger. `log` is the shared logger named 'rd' (Document
Generator / NaviCORE) — match the prefix used throughout the legacy
routes module so existing log lines render identically.

Also forces UTF-8 on stdout/stderr at process boot. Windows default
console code page (cp1252) cannot encode the em-dash / check-mark /
cross-mark glyphs used in some log lines, and an unconfigured Python
crashes with UnicodeEncodeError before the Flask app even binds the
port. errors='replace' is the safety net for any glyph the active
encoding still can't represent after reconfigure.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback


def _force_utf8_streams() -> None:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except (AttributeError, ValueError):
        pass
    os.environ.setdefault('PYTHONUNBUFFERED', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')


_force_utf8_streams()


log = logging.getLogger('rd')


def configure_logging() -> None:
    """Idempotent — safe to call from the app factory + main.py."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stdout,
        force=True,
    )
    # Werkzeug's per-request access log is noisy; the pipeline logs are
    # what the operator wants to see.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    _install_guardrails_log_filter()


def _install_guardrails_log_filter() -> None:
    """Install a logging.Filter that masks log records via the active
    workflow's guardrails policy when controls.mask_logs is on.

    Imports are local so circular imports during package boot can't
    deadlock the logging subsystem. The filter is attached to the
    ROOT logger so every getLogger() call inherits it - both the 'rd'
    log used by legacy_routes and the package-named loggers used by
    the guardrails service.
    """
    try:
        from app.services.guardrails.runtime         import ActiveGuardrails
        from app.services.guardrails.regex_detectors import detect as _regex_detect
    except Exception:
        return

    class _GuardrailsLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                active = ActiveGuardrails.current()
                if active is None or not active.config.mask_logs:
                    return True
                cats = active.config.enabled_categories
                if not cats:
                    return True
                msg = record.getMessage()
                if not msg:
                    return True
                spans = _regex_detect(msg, cats)
                if not spans:
                    return True
                # Single-pass back-to-front rewrite. Reuse the alias
                # vault so log lines and prompts share the same
                # masked tokens (operator can correlate by alias).
                spans_sorted = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
                accepted, cursor = [], -1
                for s in spans_sorted:
                    if s[0] < cursor:
                        continue
                    accepted.append(s)
                    cursor = s[1]
                out = msg
                for start, end, cat, raw in reversed(accepted):
                    alias = active.vault.alias_for(cat, raw)
                    out = out[:start] + alias + out[end:]
                record.msg  = out
                record.args = None
            except Exception:
                # Logging MUST NEVER fail because of masking.
                pass
            return True

    root = logging.getLogger()
    # Idempotent install: skip if already attached.
    for f in root.filters:
        if f.__class__.__name__ == '_GuardrailsLogFilter':
            return
    root.addFilter(_GuardrailsLogFilter())


def log_exc(tag: str, ex: BaseException) -> None:
    """Log an exception with full traceback. Use everywhere a caller
    converts an exception into an SSE error or swallows it — this
    keeps a server-side trail so failures stay debuggable."""
    log.error("%s %s: %s", tag, type(ex).__name__, ex)
    for line in traceback.format_exc().rstrip().splitlines():
        log.error("%s   %s", tag, line)
