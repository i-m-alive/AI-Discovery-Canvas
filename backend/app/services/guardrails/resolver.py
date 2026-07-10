"""
Resolve the effective guardrails config for a (project, workflow) pair.

Hierarchy
---------

    Org (singleton org_settings row)
        |
        v   inherited when project.guardrails == null or {inherit: true}
    Project (projects_metadata.guardrails JSONB)
        |
        v   inherited when workflow.guardrails == null or {inherit: true}
    Workflow (workflows_metadata.workflow_metadata.guardrails JSONB)

Whichever level supplies the active config also drives the "source"
field returned to the UI, so the panel can render the
"Inherited from ..." hint accurately.

Implementation routes everything through the store facade so the
resolver works the same whether Postgres is configured or not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing      import Dict, Optional

from app.services.guardrails.modes      import Mode, mode_to_config
from app.services.guardrails            import store as _store


log = logging.getLogger('app.guardrails.resolver')


@dataclass(frozen=True)
class EffectiveConfig:
    """The fully-resolved guardrails configuration applied at runtime."""
    mode:       str
    categories: Dict[str, bool] = field(default_factory=dict)
    controls:   Dict[str, bool] = field(default_factory=dict)
    source:     str             = 'org'    # 'org' | 'project' | 'workflow' | 'default'

    @property
    def enabled_categories(self) -> list[str]:
        return [c for c, on in self.categories.items() if on]

    @property
    def block_llm(self) -> bool:
        return bool(self.controls.get('block_llm'))

    @property
    def hide_in_graph(self) -> bool:
        return bool(self.controls.get('hide_in_graph'))

    @property
    def redact_documents(self) -> bool:
        return bool(self.controls.get('redact_documents'))

    @property
    def mask_logs(self) -> bool:
        return bool(self.controls.get('mask_logs'))

    @property
    def block_export(self) -> bool:
        return bool(self.controls.get('block_export'))

    @property
    def is_open(self) -> bool:
        return self.mode == Mode.OPEN.value

    def to_json(self) -> Dict:
        return {
            'mode':       self.mode,
            'categories': dict(self.categories),
            'controls':   dict(self.controls),
            'source':     self.source,
        }


def _from_payload(payload: Dict, source: str) -> EffectiveConfig:
    cfg = mode_to_config(
        payload.get('mode') or Mode.STANDARD.value,
        custom_categories=payload.get('categories'),
        controls=payload.get('controls'),
    )
    return EffectiveConfig(
        mode=cfg['mode'],
        categories=cfg['categories'],
        controls=cfg['controls'],
        source=source,
    )


def _is_inherit(payload: Optional[Dict]) -> bool:
    """A scope inherits from its parent when:
       * it has no row, OR
       * the persisted JSONB has inherit == True, OR
       * the persisted JSONB is empty / missing a mode.
    """
    if not payload:
        return True
    if payload.get('inherit'):
        return True
    if not payload.get('mode'):
        return True
    return False


def resolve_effective_config(project_kg_id: Optional[str] = None,
                             workflow_kg_id: Optional[str] = None) -> EffectiveConfig:
    """Resolve the effective config for a workflow run.

    Both args are optional. Always returns a valid EffectiveConfig —
    never raises. Falls through Org -> Project -> Workflow, taking
    the first one that doesn't have `inherit: true`.
    """
    try:
        workflow_payload = _store.load_workflow(workflow_kg_id)[0] if workflow_kg_id else None
        project_payload  = _store.load_project(project_kg_id)       if project_kg_id  else None
        org_payload      = _store.load_org()
    except Exception as e:
        log.warning('resolve_effective_config store read failed (%s) - falling back to neutral', e)
        return _from_payload({'mode': Mode.STANDARD.value}, source='default')

    if workflow_kg_id and not _is_inherit(workflow_payload):
        return _from_payload(workflow_payload, source='workflow')

    if project_kg_id and not _is_inherit(project_payload):
        return _from_payload(project_payload, source='project')

    if org_payload:
        return _from_payload(org_payload, source='org')

    return _from_payload({'mode': Mode.STANDARD.value}, source='default')
