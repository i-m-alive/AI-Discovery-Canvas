"""
Privacy & Guardrails REST endpoints.

Carved into its own blueprint (separate from legacy_routes.py) so that
*any* runtime issue inside the legacy monolith - import failures, route
collisions, partial registration - cannot take this surface down. The
Privacy panel must always be reachable so an operator can see and toggle
the policy at a glance.

All seven routes return the same envelope and never 500:

    {
      "config":    <persisted payload at this scope, may be null>,
      "effective": <fully-resolved EffectiveConfig as a dict>,
      "source":    "org" | "project" | "workflow" | "default",
      "warning":   <optional advisory string>
    }

Hierarchy: Org -> Project -> Workflow. A scope inherits from its parent
when its persisted payload is missing OR carries `inherit: true`.

Persistence: the underlying store ([guardrails/store.py]) writes to
Postgres when available AND always to an in-process dict, so the panel
works the moment the page loads - before any migration has been run.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request


log = logging.getLogger('rd.guardrails.api')


bp = Blueprint('guardrails', __name__, url_prefix='/guardrails')


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _current_user() -> dict | None:
    """Best-effort lookup of the active user. Imported lazily so this
    module remains usable even if the auth subsystem has issues."""
    try:
        from app.auth import current_user
        return current_user()
    except Exception:
        return None


def _is_mock_auth() -> bool:
    try:
        from app.auth import AUTH_MODE
        return (AUTH_MODE or '').lower() == 'mock'
    except Exception:
        return False


def _admin_required():
    """Permission stub - kept for the call sites that used to enforce
    admin-only writes on /guardrails/org. By product decision the
    feature is open to every signed-in user (per the v2 spec) so this
    now returns None unconditionally. The function is retained so a
    future tightening is a one-line change."""
    return None


def _pg_warning() -> str | None:
    """Surface a warning when settings will not persist past restart."""
    try:
        from app.postgres import is_ready
        if not is_ready():
            return ('Postgres unavailable - settings are stored in memory '
                    'and will not persist across restarts.')
    except Exception:
        return ('Persistence layer unavailable - settings are stored in '
                'memory only.')
    return None


def _envelope(persisted, effective, source, warning=None):
    payload = {
        'config':    persisted,
        'effective': effective.to_json(),
        'source':    source,
    }
    if warning:
        payload['warning'] = warning
    return payload


def _payload_from_request():
    """Pluck (mode, categories, controls, inherit) from the JSON body.
    Defaults to standard mode on anything weird so a malformed PUT never
    blows up the API."""
    body = request.get_json(silent=True) or {}
    mode_raw = (body.get('mode') or '').strip().lower() or 'standard'
    if mode_raw not in {'open', 'standard', 'strict', 'custom'}:
        mode_raw = 'standard'
    categories = body.get('categories') or {}
    controls   = body.get('controls')   or {}
    inherit    = bool(body.get('inherit'))
    if not isinstance(categories, dict):
        categories = {}
    if not isinstance(controls, dict):
        controls = {}
    return mode_raw, categories, controls, inherit


def _safe_resolve(project_kg_id=None, workflow_kg_id=None):
    """Resolver that NEVER raises. Returns a usable EffectiveConfig
    even when the resolver / store / Postgres all fall over."""
    try:
        from app.services.guardrails import resolve_effective_config
        return resolve_effective_config(
            project_kg_id=project_kg_id,
            workflow_kg_id=workflow_kg_id,
        )
    except Exception as e:
        log.warning('[guardrails] safe-resolve fallback: %s', e)
        try:
            from app.services.guardrails import EffectiveConfig, mode_to_config
            cfg = mode_to_config('standard')
            return EffectiveConfig(
                mode=cfg['mode'], categories=cfg['categories'],
                controls=cfg['controls'], source='default',
            )
        except Exception:
            # Absolute last-resort empty config so the JSON serialiser
            # always has something to write.
            from types import SimpleNamespace
            return SimpleNamespace(
                mode='standard', categories={}, controls={},
                source='default',
                to_json=lambda: {'mode': 'standard', 'categories': {},
                                 'controls': {}, 'source': 'default'},
            )


def _log_unexpected(tag, exc):
    """Centralised logger so the operator always sees the *real* error
    that drove an endpoint into its safe-default fallback."""
    log.warning('[guardrails/%s] %s: %s', tag, type(exc).__name__, exc)


# ─────────────────────────────────────────────────────────────────────
# 1. Categories catalog (static, but served via API so the frontend
#    doesn't need to hard-code the list)
# ─────────────────────────────────────────────────────────────────────

@bp.route('/categories', methods=['GET'])
def categories():
    try:
        from app.services.guardrails.categories import CATEGORY_GROUPS, AI_KG_CONTROLS
        return jsonify({
            'groups':   CATEGORY_GROUPS,
            'controls': AI_KG_CONTROLS,
            'modes': [
                {'id': 'open',     'label': 'Open Mode',
                 'description': 'No masking applied. Use only when the entire workspace is trusted.'},
                {'id': 'standard', 'label': 'Standard Privacy',
                 'description': 'Masks emails, phone numbers, and employee IDs. Safe default.'},
                {'id': 'strict',   'label': 'Strict Privacy',
                 'description': 'Masks every identity, organization, and project entity. Anonymises the knowledge graph.'},
                {'id': 'custom',   'label': 'Custom Privacy',
                 'description': 'Choose exactly what to hide.'},
            ],
        })
    except Exception as e:
        _log_unexpected('categories', e)
        return jsonify({'groups': [], 'controls': [], 'modes': []})


# ─────────────────────────────────────────────────────────────────────
# 2. Org scope (singleton org_settings row + in-memory mirror)
# ─────────────────────────────────────────────────────────────────────

@bp.route('/org', methods=['GET', 'PUT'])
def org():
    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('org/import', e)
        return jsonify(_envelope(None, _safe_resolve(), 'default',
                                  warning='Guardrails store unavailable.')), 200

    if request.method == 'PUT':
        denied = _admin_required()
        if denied is not None:
            return denied
        mode, categories, controls, _ = _payload_from_request()
        u = _current_user() or {}
        try:
            gr_store.save_org(
                {'mode': mode, 'categories': categories, 'controls': controls},
                by_email=u.get('email'),
            )
        except Exception as e:
            _log_unexpected('org/save', e)

    try:
        persisted = gr_store.load_org()
    except Exception as e:
        _log_unexpected('org/load', e)
        persisted = None

    eff = _safe_resolve()
    return jsonify(_envelope(persisted, eff, eff.source, warning=_pg_warning()))


# ─────────────────────────────────────────────────────────────────────
# 3. Project scope
# ─────────────────────────────────────────────────────────────────────

@bp.route('/project/<project_kg_id>', methods=['GET', 'PUT'])
def project(project_kg_id):
    if not project_kg_id:
        return jsonify({'error': 'project_kg_id is required'}), 400

    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('project/import', e)
        return jsonify(_envelope(None, _safe_resolve(project_kg_id=project_kg_id),
                                  'default', warning='Guardrails store unavailable.')), 200

    if request.method == 'PUT':
        mode, categories, controls, inherit = _payload_from_request()
        payload = {
            'inherit':    inherit,
            'mode':       mode,
            'categories': categories,
            'controls':   controls,
        }
        try:
            gr_store.save_project(project_kg_id, payload)
        except Exception as e:
            _log_unexpected('project/save', e)

    try:
        persisted = gr_store.load_project(project_kg_id)
    except Exception as e:
        _log_unexpected('project/load', e)
        persisted = None

    eff = _safe_resolve(project_kg_id=project_kg_id)
    return jsonify(_envelope(persisted, eff, eff.source, warning=_pg_warning()))


# ─────────────────────────────────────────────────────────────────────
# 4. Workflow scope (per-workflow override of the project policy)
# ─────────────────────────────────────────────────────────────────────

@bp.route('/workflow/<workflow_kg_id>', methods=['GET', 'PUT'])
def workflow(workflow_kg_id):
    if not workflow_kg_id:
        return jsonify({'error': 'workflow_kg_id is required'}), 400

    # Project hint can come from the body (PUT) or the query string (GET).
    body = request.get_json(silent=True) or {}
    hinted_project = (body.get('project_kg_id')
                      or request.args.get('project_kg_id')
                      or None)

    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('workflow/import', e)
        return jsonify(_envelope(None,
                                  _safe_resolve(project_kg_id=hinted_project,
                                                workflow_kg_id=workflow_kg_id),
                                  'default', warning='Guardrails store unavailable.')), 200

    if request.method == 'PUT':
        mode, categories, controls, inherit = _payload_from_request()
        payload = {
            'inherit':    inherit,
            'mode':       mode,
            'categories': categories,
            'controls':   controls,
        }
        try:
            gr_store.save_workflow(workflow_kg_id, payload,
                                    project_kg_id=hinted_project)
        except Exception as e:
            _log_unexpected('workflow/save', e)

    try:
        persisted, parent_hint = gr_store.load_workflow(workflow_kg_id)
    except Exception as e:
        _log_unexpected('workflow/load', e)
        persisted, parent_hint = None, None

    parent_project = parent_hint or hinted_project
    eff = _safe_resolve(project_kg_id=parent_project,
                         workflow_kg_id=workflow_kg_id)
    return jsonify(_envelope(persisted, eff, eff.source, warning=_pg_warning()))


# ─────────────────────────────────────────────────────────────────────
# 5. Batch apply - "Selected Projects" scope in the UI.
#
#    PUT /guardrails/projects/batch
#       body: { project_kg_ids: [str, ...], mode, categories,
#               controls, inherit }
#
#    Applies the SAME guardrails payload to every project in the list.
#    Designed to be tolerant: per-project failures are collected and
#    reported in the response rather than aborting the batch. The whole
#    call returns 200 with a summary so the UI can render a toast
#    counting successes / failures.
# ─────────────────────────────────────────────────────────────────────

@bp.route('/projects/batch', methods=['PUT'])
def projects_batch():
    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('batch/import', e)
        return jsonify({
            'applied': 0, 'failed': 0, 'errors': [],
            'warning': 'Guardrails store unavailable.',
        }), 200

    body = request.get_json(silent=True) or {}
    project_ids = body.get('project_kg_ids') or []
    if not isinstance(project_ids, list):
        project_ids = []
    project_ids = [str(p).strip() for p in project_ids if str(p).strip()]

    if not project_ids:
        return jsonify({
            'applied': 0, 'failed': 0, 'errors': [],
            'warning': 'No project_kg_ids supplied.',
        }), 200

    mode_raw, categories, controls, inherit = _payload_from_request()
    payload = {
        'inherit':    inherit,
        'mode':       mode_raw,
        'categories': categories,
        'controls':   controls,
    }

    applied = 0
    errors  = []
    for pid in project_ids:
        try:
            gr_store.save_project(pid, payload)
            applied += 1
        except Exception as e:
            _log_unexpected(f'batch/save/{pid[:8]}', e)
            errors.append({'project_kg_id': pid, 'error': str(e)})

    return jsonify({
        'applied': applied,
        'failed':  len(errors),
        'errors':  errors,
        'warning': _pg_warning(),
    })


# ─────────────────────────────────────────────────────────────────────
# 6. Health probe so a frontend or operator can confirm the blueprint
#    is reachable independent of the Postgres state.
# ─────────────────────────────────────────────────────────────────────

@bp.route('/health', methods=['GET'])
def health():
    pg_state = 'unknown'
    try:
        from app.postgres import is_ready
        pg_state = 'ready' if is_ready() else 'disabled'
    except Exception:
        pg_state = 'unreachable'
    return jsonify({
        'ok':       True,
        'postgres': pg_state,
    })


# ─────────────────────────────────────────────────────────────────────
# 7. Unified /guardrails/settings endpoints (v2 spec)
#
# GET  /guardrails/settings?project_id=X&workflow_id=Y
#    Returns effective settings + raw config at the tightest scope
#    that has an override. Resolution: workflow → project → org → default.
#
# POST /guardrails/settings
#    Upsert guardrails JSON. Detects whether settings were previously
#    saved and returns notify_rerun: true so the frontend can show the
#    "Re-run to apply" banner.
#
# GET  /guardrails/settings/effective?project_id=X&workflow_id=Y
#    Returns the single resolved policy used by the pipeline.
# ─────────────────────────────────────────────────────────────────────

@bp.route('/settings', methods=['GET'])
def settings_get():
    """Unified settings fetch. Returns effective config for the given
    project_id / workflow_id pair and the raw persisted payload at the
    tightest scope that has an override (the 'config' field)."""
    project_id  = (request.args.get('project_id')  or '').strip() or None
    workflow_id = (request.args.get('workflow_id') or '').strip() or None

    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('settings_get/import', e)
        eff = _safe_resolve(project_kg_id=project_id, workflow_kg_id=workflow_id)
        return jsonify(_envelope(None, eff, eff.source,
                                  warning='Guardrails store unavailable.')), 200

    # Load the raw config at the tightest scope that has an explicit
    # override (inherit != True AND mode is set). The 'config' field in
    # the envelope always comes from the same level that 'source' names,
    # so the frontend can pre-fill the panel without a mismatch.
    eff = _safe_resolve(project_kg_id=project_id, workflow_kg_id=workflow_id)
    persisted = None
    try:
        if eff.source == 'workflow' and workflow_id:
            wf_payload, _ = gr_store.load_workflow(workflow_id)
            persisted = wf_payload
        elif eff.source == 'project' and project_id:
            persisted = gr_store.load_project(project_id)
        else:
            persisted = gr_store.load_org()
    except Exception as e:
        _log_unexpected('settings_get/load', e)

    return jsonify(_envelope(persisted, eff, eff.source, warning=_pg_warning()))


@bp.route('/settings', methods=['POST'])
def settings_post():
    """Unified save endpoint. Writes to the scope indicated by the body's
    'scope' field and returns notify_rerun: true when this project /
    workflow already had saved settings (i.e. settings changed on a run
    that the user may want to re-execute to pick up the new policy)."""
    try:
        from app.services.guardrails import store as gr_store
    except Exception as e:
        _log_unexpected('settings_post/import', e)
        return jsonify({'ok': False, 'warning': 'Guardrails store unavailable.'}), 200

    body = request.get_json(silent=True) or {}
    scope       = (body.get('scope') or 'project').strip().lower()
    project_id  = (body.get('project_id')  or '').strip() or None
    workflow_id = (body.get('workflow_id') or '').strip() or None

    # Extract mode / categories / controls from the unified payload.
    mode_raw = (body.get('privacy_mode') or body.get('mode') or '').strip().lower() or 'standard'
    if mode_raw not in {'open', 'standard', 'strict', 'custom'}:
        mode_raw = 'standard'

    controls = {}
    for key in ('hide_kg_entities', 'prevent_llm_data', 'redact_documents',
                'mask_logs', 'prevent_export'):
        # Accept both the spec's UI key names and the internal names.
        internal = {
            'hide_kg_entities': 'hide_in_graph',
            'prevent_llm_data': 'block_llm',
            'prevent_export':   'block_export',
        }.get(key, key)
        # Accept either the UI name or the internal name in the body.
        val = body.get(key)
        if val is None:
            val = body.get(internal)
        if val is not None:
            controls[internal] = bool(val)
    # Also accept a nested 'controls' dict (old format).
    if 'controls' in body and isinstance(body['controls'], dict):
        for k, v in body['controls'].items():
            if k not in controls:
                controls[k] = bool(v)

    custom_fields = body.get('custom_fields') or body.get('categories') or {}
    if not isinstance(custom_fields, dict):
        custom_fields = {}

    u = _current_user() or {}
    import datetime
    now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'

    payload = {
        'inherit':    False,
        'mode':       mode_raw,
        'categories': custom_fields,
        'controls':   controls,
        'updated_at': now_iso,
        'updated_by': u.get('email'),
    }

    # Detect whether we are overwriting existing settings (→ notify_rerun).
    notify_rerun = False
    try:
        if scope == 'org':
            existing = gr_store.load_org()
            prev_mode = existing.get('mode') if existing else None
            gr_store.save_org(payload, by_email=u.get('email'))
            notify_rerun = False  # org changes are global, no re-run to notify

        elif scope == 'workflow' and workflow_id:
            existing, _ = gr_store.load_workflow(workflow_id)
            notify_rerun = bool(existing and existing.get('mode'))
            gr_store.save_workflow(workflow_id, payload, project_kg_id=project_id)

        elif scope == 'selected':
            ids = body.get('selected_project_ids') or []
            if not isinstance(ids, list):
                ids = []
            applied, errors = 0, []
            for pid in ids:
                pid = (pid or '').strip()
                if not pid:
                    continue
                try:
                    gr_store.save_project(pid, payload)
                    applied += 1
                except Exception as ex:
                    errors.append({'project_id': pid, 'error': str(ex)})
            eff = _safe_resolve(project_kg_id=project_id, workflow_kg_id=workflow_id)
            return jsonify({
                'ok': True,
                'applied': applied,
                'failed': len(errors),
                'errors': errors,
                'notify_rerun': False,
                'effective': eff.to_json(),
                'source': eff.source,
                'warning': _pg_warning(),
            })

        else:  # default: project scope
            pid = project_id
            if not pid:
                return jsonify({'ok': False, 'error': 'project_id is required for project scope.'}), 400
            existing = gr_store.load_project(pid)
            notify_rerun = bool(existing and existing.get('mode'))
            gr_store.save_project(pid, payload)

    except Exception as e:
        _log_unexpected('settings_post/save', e)
        return jsonify({'ok': False, 'error': str(e), 'warning': _pg_warning()}), 200

    eff = _safe_resolve(project_kg_id=project_id, workflow_kg_id=workflow_id)
    return jsonify({
        'ok': True,
        'notify_rerun': notify_rerun,
        'config': payload,
        'effective': eff.to_json(),
        'source': eff.source,
        'warning': _pg_warning(),
    })


@bp.route('/settings/effective', methods=['GET'])
def settings_effective():
    """Return the single resolved policy the pipeline will use at runtime
    for the given project_id / workflow_id. Intended for the pipeline
    execution entry point to read before starting a run."""
    project_id  = (request.args.get('project_id')  or '').strip() or None
    workflow_id = (request.args.get('workflow_id') or '').strip() or None
    eff = _safe_resolve(project_kg_id=project_id, workflow_kg_id=workflow_id)
    return jsonify({
        'mode':       eff.mode,
        'categories': eff.categories,
        'controls':   eff.controls,
        'source':     eff.source,
        'warning':    _pg_warning(),
    })


def install(app):
    """Idempotent blueprint installer. Called from the app factory."""
    if 'guardrails' in app.blueprints:
        return
    app.register_blueprint(bp)
    # Register the prefix with the auth gate's allowlist? No - guardrails
    # endpoints REQUIRE auth (same as everything else under the gate).
    log.info('[STARTUP] Guardrails blueprint mounted at /guardrails')
