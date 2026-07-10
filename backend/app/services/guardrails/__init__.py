"""
Guardrails / Privacy Control service.

This package is the single cross-cutting concern that prevents sensitive
content from reaching downstream stages (LLM prompts, embeddings, graph
writes, generated documents, logs, exports). It is structured around
three ideas:

  * A hierarchical configuration model (Global -> Project -> Workflow)
    resolved into an immutable EffectiveConfig per run.
  * A detection pipeline combining regex (structured PII) and an
    LLM-based NER pre-pass (unstructured entities like person / company
    names) with a per-run alias vault so masked tokens stay stable
    (John Smith -> Person_1) across every downstream call inside the
    same workflow run.
  * A contextvars-based ActiveGuardrails sentinel that the existing
    run_llm() chokepoint reads, so the 40+ LLM call sites need no
    individual changes.

Public surface (everything else is internal):

    resolve_effective_config(project_kg_id, workflow_kg_id)
        -> EffectiveConfig + the source level it came from.

    ActiveGuardrails
        Context manager that pins an EffectiveConfig + AliasVault to
        the current thread / async task. Use:
            with ActiveGuardrails.enter(cfg, vault, run_id=run_id):
                ...

    mask(text)
        Convenience wrapper that masks via the currently-active
        guardrails (or returns text unchanged when none are active).
"""

from __future__ import annotations

from app.services.guardrails.modes      import Mode, mode_to_config, MODE_PRESETS
from app.services.guardrails.categories import (
    CATEGORIES, CATEGORY_GROUPS, AI_KG_CONTROLS, default_categories, default_controls,
)
from app.services.guardrails            import store
from app.services.guardrails.resolver   import resolve_effective_config, EffectiveConfig
from app.services.guardrails.runtime    import ActiveGuardrails
from app.services.guardrails.vault      import AliasVault
from app.services.guardrails.masker     import mask, mask_dict


__all__ = [
    'AI_KG_CONTROLS',
    'ActiveGuardrails',
    'AliasVault',
    'CATEGORIES',
    'CATEGORY_GROUPS',
    'EffectiveConfig',
    'MODE_PRESETS',
    'Mode',
    'default_categories',
    'default_controls',
    'mask',
    'mask_dict',
    'mode_to_config',
    'resolve_effective_config',
]
