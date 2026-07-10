"""
ActiveGuardrails - a contextvars-based sentinel so any code path
inside a workflow run can fetch the effective config + alias vault
without threading args through 40 LLM call sites.

Usage at the run boundary
-------------------------

    cfg   = resolve_effective_config(project_kg_id, workflow_kg_id)
    vault = AliasVault.for_run(run_id)
    with ActiveGuardrails.enter(cfg, vault, run_id=run_id):
        ... existing pipeline ...

Usage inside run_llm()
----------------------

    active = ActiveGuardrails.current()
    if active and active.config.block_llm:
        prompt, report = active.mask(prompt)

ContextVar is picked over thread-local on purpose: the codebase uses
ThreadPoolExecutor for video frame analysis (`workflow_assets.py`
fans out frame summarisation). contextvars.copy_context() carries the
sentinel into child threads when they're started via `run_in_executor`
patterns; for raw `threading.Thread(target=...)` calls the masker also
falls back gracefully (returns text unchanged when current() is None).
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing     import Iterator, List, Optional, Tuple

from app.services.guardrails.masker   import mask, mask_dict, MaskReport
from app.services.guardrails.resolver import EffectiveConfig
from app.services.guardrails.vault    import AliasVault, prefix_for


log = logging.getLogger('app.guardrails.runtime')


_ACTIVE: contextvars.ContextVar[Optional['ActiveGuardrails']] = contextvars.ContextVar(
    'guardrails_active', default=None,
)


class ActiveGuardrails:
    """Bound (EffectiveConfig, AliasVault, run_id) for one workflow run."""

    def __init__(self, config: EffectiveConfig, vault: AliasVault,
                 run_id: Optional[str] = None):
        self.config  = config
        self.vault   = vault
        self.run_id  = run_id

    # ---- contextvar plumbing --------------------------------------------

    @classmethod
    def current(cls) -> Optional['ActiveGuardrails']:
        return _ACTIVE.get()

    @classmethod
    @contextmanager
    def enter(cls, config: EffectiveConfig, vault: AliasVault,
              run_id: Optional[str] = None) -> Iterator['ActiveGuardrails']:
        instance = cls(config, vault, run_id=run_id)
        token = _ACTIVE.set(instance)
        try:
            yield instance
        finally:
            _ACTIVE.reset(token)

    # ---- masking ---------------------------------------------------------

    def mask(self, text: str, *, use_ner: bool = True,
             tag: str = '') -> Tuple[str, MaskReport]:
        """Mask `text` through the active config + vault."""
        if not self.config.enabled_categories:
            return text, MaskReport()
        masked, report = mask(
            text,
            enabled_categories=self.config.enabled_categories,
            vault=self.vault,
            use_ner=use_ner,
        )
        return masked, report

    def mask_dict(self, payload, *, use_ner: bool = False):
        if not self.config.enabled_categories:
            return payload
        return mask_dict(
            payload,
            enabled_categories=self.config.enabled_categories,
            vault=self.vault,
            use_ner=use_ner,
        )

    def mask_entity_label(self, name: str, kind_hint: Optional[str] = None) -> str:
        """Replace a single proper-noun label with its alias when the
        category that owns `kind_hint` is enabled and hide_in_graph is on.

        Used by knowledge_graph.add_or_get_node so atlas nodes get
        anonymised consistently with the masked summary text.
        """
        if not name or not self.config.hide_in_graph:
            return name
        # Map Neo4j node label hints to our category ids.
        cat = (kind_hint or '').lower()
        if cat in ('person', 'people'):
            cat = 'person'
        elif cat in ('company', 'organization', 'org'):
            cat = 'company'
        elif cat in ('client',):
            cat = 'client'
        elif cat in ('vendor',):
            cat = 'vendor'
        elif cat in ('team', 'group'):
            cat = 'team'
        elif cat in ('project',):
            cat = 'project'
        else:
            return name

        if cat not in self.config.enabled_categories:
            return name

        existing = self.vault.known(cat, name)
        if existing:
            return existing
        return self.vault.alias_for(cat, name)
