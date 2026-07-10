"""
Write per-run guardrails stats onto the matching pipeline_runs row.

Embeds into `pipeline_runs.extra` JSONB:

    "guardrails": {
        "mode":          "strict",
        "source":        "project",
        "controls":      {"block_llm": true, ...},
        "masked":        {"person": 3, "email": 2, ...},
        "ner_cache_hits": 4
    }

Best-effort - any write failure is logged and swallowed so an audit
hiccup never aborts the run.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.guardrails.runtime import ActiveGuardrails


log = logging.getLogger('app.guardrails.audit')


def write_run_summary(workflow_run_id: Optional[str] = None) -> None:
    """Emit a per-run guardrails summary.

    Always logs the summary at INFO so operators have an easy trace.
    Additionally tries to attach the payload to a matching pipeline_runs
    row (best-effort - the analytics layer may have logged the row
    against a different workflow id, in which case the DB write is a
    no-op).
    """
    active = ActiveGuardrails.current()
    if not active:
        return

    payload = {
        'mode':           active.config.mode,
        'source':         active.config.source,
        'controls':       dict(active.config.controls),
        'masked':         active.vault.summary(),
        'ner_cache_hits': len(active.vault.ner_cache),
    }

    log.info('[GUARDRAILS] run_summary mode=%s source=%s masked=%s ner_cache=%d',
             payload['mode'], payload['source'],
             payload['masked'], payload['ner_cache_hits'])

    if not workflow_run_id:
        return

    try:
        from app.postgres import is_ready, session_scope
        if not is_ready():
            return
        from sqlalchemy import select
        from app.postgres.models.pipeline_run import PipelineRun
        with session_scope() as session:
            row = session.execute(
                select(PipelineRun).where(PipelineRun.workflow_kg_id == workflow_run_id)
                                   .order_by(PipelineRun.created_at.desc())
                                   .limit(1)
            ).scalar_one_or_none()
            if not row:
                return
            extra = dict(row.extra or {})
            extra['guardrails'] = payload
            row.extra = extra
    except Exception as e:
        log.warning('guardrails audit write failed: %s', e)
