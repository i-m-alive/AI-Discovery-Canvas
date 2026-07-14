"""
Agent routes.

    GET  /api/agents/ping    PUBLIC (whitelisted in app/__init__.py).
                             { ok: true, service: "..." } — pre-login
                             reachability probe.

    POST /api/agents/ping    AUTH-GATED smoke test from Phase 1: calls
                             llm_service.complete() with a trivial prompt.

    POST /api/agents/run     AUTH-GATED. The real agent pipeline.
                             body: { agent_id, context, extra?, workshop_id }
                               context: { zone, scope, board, transcript[],
                                          files[{name,text}] } — built by
                               the frontend canvas from live board state.
                               workshop_id scopes Prepare-zone documents,
                               RAG retrieval, GraphRAG entities, and where
                               the generated draft is persisted (see
                               app/services/agent_catalog.py).
                             -> { ok: true, draft: {...} }   (see
                                app/services/agent_catalog.py for the
                                draft shape)
                             -> { ok: false, error } with HTTP 200 on any
                                failure (Bedrock unconfigured, model
                                returned garbage, ...) so the assistant
                                panel renders a readable failure card
                                instead of a crashed fetch.

    POST /api/agents/chat    AUTH-GATED. Free-form assistant turn.
                             body: { message, context, workshop_id } -> { ok, kind,
                             reply, sources? } (reply is plain text; frontend
                             escapes it). Automatically pulls recent
                             conversation history (see copilot/messages
                             below) into the model call so follow-up
                             questions resolve correctly.

    GET    /api/agents/copilot/messages?workshop_id=<id>   AUTH-GATED.
                             -> { ok, messages } — the persisted Copilot
                             conversation for this workshop (one thread
                             per workshop, not per user).

    POST   /api/agents/copilot/messages   AUTH-GATED.
                             body: { workshop_id, message } -> { ok } —
                             appends one message (called by the frontend
                             after every user/assistant turn).

    DELETE /api/agents/copilot/messages?workshop_id=<id>   AUTH-GATED.
                             -> { ok } — clears this workshop's Copilot
                             conversation.

    POST /api/agents/upload  AUTH-GATED. multipart/form-data, fields
                             'file' + 'workshop_id'. Extracts text
                             server-side via the RAG file extractor
                             (PDF/DOCX/XLSX/PPTX/CSV/HTML/TXT/ZIP),
                             registers it PERMANENTLY in
                             app.services.prepare_docs, scoped to that
                             workshop, and returns it so the frontend can
                             also attach it as immediate agent context.
                             -> { ok, name, chars, text, doc_id }.

    GET    /api/agents/prepare-docs?workshop_id=<id>    AUTH-GATED.
                             -> { ok, docs: [{doc_id,name,chars,
                             uploaded_by,uploaded_at}] } — every document
                             ever uploaded to this workshop's Prepare zone.

    GET    /api/agents/document/<doc_id>?workshop_id=<id>   AUTH-GATED.
                             -> { ok, name, text } — full extracted text,
                             for the frontend's click-to-preview. Checks
                             prepare_docs (uploads) first, then
                             generated_docs (approved agent drafts).

    DELETE /api/agents/document/<doc_id>?workshop_id=<id>   AUTH-GATED.
                             -> { ok } — removes a document (called when
                             its canvas node is deleted). Tries both
                             registries.

    GET    /api/agents/document/<doc_id>/word?workshop_id=<id>   AUTH-GATED.
                             -> a .docx file (Content-Disposition:
                             attachment) — exports a GENERATED document
                             (research brief, risk assessment, workflow
                             write-up, ...) as Word. See
                             app/services/docx_export.py.

    GET    /api/agents/document/<doc_id>/diagram?workshop_id=<id>   AUTH-GATED.
                             -> { ok, xml, diagrams } — the persisted
                             workflow diagram for a generated doc (see
                             generated_docs.get_diagram), fetched lazily
                             for the "View diagram" affordance.

Design note: run/chat return HTTP 200 with ok:false on functional
failures (only auth failures are non-200). The route being reached and
auth passing vs. the model call failing are different problems — keeping
them distinguishable in the response body is what makes the frontend's
error rendering (and debugging) sane.
"""

from __future__ import annotations

import re

from flask import Blueprint, Response, jsonify, request, stream_with_context

from app.auth import auth_required, current_user
from app.core.logging import log, log_exc
from app.services import llm_service
from app.services import agent_catalog
from app.services import prepare_docs

bp = Blueprint('agents', __name__)

_DEFAULT_PROMPT = 'Reply with the single word: ready'

# Upload guards: a client SOP/deck is a few MB; refuse absurd sizes and
# cap the extracted text so one attachment can't blow the prompt budget
# (agent_catalog re-caps per-file and in total when building the prompt).
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024
_MAX_EXTRACT_CHARS = 60_000


@bp.route('/api/agents/ping', methods=['GET'])
def ping_public():
    """No auth — a trivial reachability check for the login page."""
    return jsonify({'ok': True, 'service': 'ai-discovery-canvas-backend'})


@bp.route('/api/agents/ping', methods=['POST'])
@auth_required
def ping_llm():
    """Auth-gated. Exercises the real backbone: Next.js -> rewrite ->
    Flask -> auth -> llm_service -> AWS Bedrock."""
    body = request.get_json(silent=True) or {}
    prompt = (body.get('prompt') or '').strip() or _DEFAULT_PROMPT
    try:
        reply = llm_service.complete(prompt, tag='[AGENTS/PING]')
        return jsonify({'ok': True, 'reply': reply})
    except Exception as e:
        log.warning('[AGENTS/PING] llm_service.complete failed (%s): %s',
                    e.__class__.__name__, e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/agents/run', methods=['POST'])
@auth_required
def run_agent():
    body = request.get_json(silent=True) or {}
    agent_id = (body.get('agent_id') or '').strip()
    if agent_id not in agent_catalog.AGENT_SPECS:
        return jsonify({'ok': False, 'error': f'unknown agent: {agent_id or "(missing)"}'}), 400
    context = body.get('context') if isinstance(body.get('context'), dict) else {}
    extra = body.get('extra')
    workshop_id = body.get('workshop_id')
    try:
        workshop_id = int(workshop_id) if workshop_id else None
    except (TypeError, ValueError):
        workshop_id = None
    user = current_user() or {}
    author = user.get('name') or user.get('email') or ''
    options = body.get('options') if isinstance(body.get('options'), dict) else None
    try:
        draft = agent_catalog.run_agent(agent_id, context, extra=extra, workshop_id=workshop_id,
                                        author=author, options=options)
        return jsonify({'ok': True, 'draft': draft})
    except Exception as e:
        log_exc(f'[AGENTS/RUN/{agent_id}]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


def _user_key() -> str:
    """Stable per-user key for Copilot threads — the auth subsystem's user
    id, falling back to email. Empty string only for the (shouldn't
    happen behind @auth_required) anonymous case."""
    user = current_user() or {}
    return str(user.get('id') or user.get('email') or '')


@bp.route('/api/agents/chat', methods=['POST'])
@auth_required
def chat():
    """The copilot turn. Either a grounded reply, OR — when the message is
    really an agent request ("use the SOP in Prepare to create a
    workflow") — a dispatch the frontend runs through the normal
    draft-card flow: { ok, kind:'dispatch', agent_id, extra }."""
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'message is required'}), 400
    context = body.get('context') if isinstance(body.get('context'), dict) else {}
    workshop_id = body.get('workshop_id')
    try:
        workshop_id = int(workshop_id) if workshop_id else None
    except (TypeError, ValueError):
        workshop_id = None
    try:
        from app.services import copilot_thread
        user_key = _user_key()
        history = copilot_thread.recent_for_model(workshop_id, user_key) if workshop_id else None
        if workshop_id:
            # Fold turns that have aged out of the verbatim window into the
            # rolling summary — on a daemon thread, never in this turn's
            # latency path.
            copilot_thread.kickoff_summary_update(workshop_id, user_key)
        out = agent_catalog.route_chat(message, context, workshop_id=workshop_id, history=history)
        if out['kind'] == 'dispatch':
            return jsonify({'ok': True, 'kind': 'dispatch',
                            'agent_id': out['agent_id'], 'extra': out.get('extra')})
        if not out.get('reply'):
            raise RuntimeError('the model returned an empty reply — try again')
        return jsonify({'ok': True, 'kind': out.get('kind', 'reply'), 'reply': out['reply'],
                        'sources': out.get('sources') or []})
    except Exception as e:
        log_exc('[AGENTS/CHAT]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/agents/chat/stream', methods=['POST'])
@auth_required
def chat_stream():
    """Streaming variant of /api/agents/chat — NDJSON frames, one JSON
    object per line:
        {"type":"meta", "kind":"dispatch"|"reply", ...}   first frame
        {"type":"delta", "text":"..."}                    reply text chunks
        {"type":"done", "reply": "<full text>"}           terminal frame
        {"type":"error", "error":"..."}                   terminal on failure
    The frontend falls back to the blocking route if this one fails, so
    an environment that buffers/breaks streaming loses latency, not
    functionality."""
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'message is required'}), 400
    context = body.get('context') if isinstance(body.get('context'), dict) else {}
    workshop_id = body.get('workshop_id')
    try:
        workshop_id = int(workshop_id) if workshop_id else None
    except (TypeError, ValueError):
        workshop_id = None

    from app.services import copilot_thread
    user_key = _user_key()
    history = copilot_thread.recent_for_model(workshop_id, user_key) if workshop_id else None
    if workshop_id:
        copilot_thread.kickoff_summary_update(workshop_id, user_key)

    def _frames():
        import json as _json
        try:
            for event, payload in agent_catalog.route_chat_stream(
                    message, context, workshop_id=workshop_id, history=history):
                if event == 'meta':
                    yield _json.dumps({'type': 'meta', **payload}) + '\n'
                elif event == 'delta':
                    yield _json.dumps({'type': 'delta', 'text': payload}) + '\n'
                elif event == 'done':
                    yield _json.dumps({'type': 'done', **payload}) + '\n'
        except Exception as e:
            log_exc('[AGENTS/CHAT/STREAM]', e)
            yield _json.dumps({'type': 'error', 'error': str(e)}) + '\n'

    resp = Response(stream_with_context(_frames()), mimetype='application/x-ndjson')
    # Defensive: tell any intermediary (Next.js dev proxy, nginx) not to
    # buffer the stream into one big flush at the end.
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@bp.route('/api/agents/copilot/messages', methods=['GET'])
@auth_required
def get_copilot_history():
    """Restores CopilotPanel.jsx's thread on open — one conversation per
    (workshop, signed-in user); see app/services/copilot_thread.py."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import copilot_thread
    return jsonify({'ok': True, 'messages': copilot_thread.list_messages(workshop_id, _user_key())})


@bp.route('/api/agents/copilot/messages', methods=['POST'])
@auth_required
def append_copilot_message():
    """Called by CopilotPanel.jsx after every user/assistant turn so the
    conversation survives closing the panel or reloading the page."""
    body = request.get_json(silent=True) or {}
    workshop_id = body.get('workshop_id')
    try:
        workshop_id = int(workshop_id) if workshop_id else None
    except (TypeError, ValueError):
        workshop_id = None
    message = body.get('message')
    if not workshop_id or not isinstance(message, dict):
        return jsonify({'ok': False, 'error': 'workshop_id and message are required'}), 400
    from app.services import copilot_thread
    copilot_thread.append_message(workshop_id, _user_key(), message)
    return jsonify({'ok': True})


@bp.route('/api/agents/copilot/messages', methods=['DELETE'])
@auth_required
def clear_copilot_history():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import copilot_thread
    copilot_thread.clear(workshop_id, _user_key())
    return jsonify({'ok': True})


@bp.route('/api/agents/copilot/meta', methods=['GET'])
@auth_required
def copilot_meta():
    """Header/chip data for CopilotPanel: how many source documents ground
    it, the corpus intent line, and 3 corpus-specific suggested questions.
    Cache-only (workshop_contexts — kept warm by the post-upload refresh);
    an empty result just means nothing has been ingested yet."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import workshop_context
    return jsonify({'ok': True, **workshop_context.get_meta(workshop_id)})


@bp.route('/api/agents/search', methods=['GET'])
@auth_required
def search_artifacts():
    """The header search bar: one query across this workshop's source
    documents AND generated artifacts, two ways at once —
      * name match (substring, no LLM/embedding cost), and
      * semantic content match (one query embedding + FAISS lookup over
        the same scoped index Copilot grounds on).
    -> {ok, results: [{doc_id, name, origin: 'source'|'generated',
        match: 'name'|'content', snippet?}]} — name matches first,
    deduped by doc_id."""
    workshop_id = request.args.get('workshop_id', type=int)
    q = (request.args.get('q') or '').strip()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    if len(q) < 2:
        return jsonify({'ok': True, 'results': []})

    from app.services import generated_docs
    results: list[dict] = []
    seen: set[str] = set()
    ql = q.lower()

    for d in prepare_docs.list_docs(workshop_id):
        if ql in (d['name'] or '').lower():
            results.append({'doc_id': d['doc_id'], 'name': d['name'], 'origin': 'source', 'match': 'name'})
            seen.add(d['doc_id'])
    for d in generated_docs.list_docs(workshop_id):
        if ql in (d['name'] or '').lower():
            results.append({'doc_id': d['doc_id'], 'name': d['name'], 'origin': 'generated', 'match': 'name'})
            seen.add(d['doc_id'])

    try:
        from app.services import rag
        if rag.is_enabled():
            for h in rag.retrieve(q, k=6, workflow_id=str(workshop_id), tag='[AGENTS/SEARCH]'):
                raw_id = h.get('doc_id') or ''
                if raw_id.startswith('prepare-doc:'):
                    doc_id, origin = raw_id[len('prepare-doc:'):], 'source'
                elif raw_id.startswith('gdoc:'):
                    doc_id, origin = raw_id[len('gdoc:'):], 'generated'
                else:
                    continue
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                meta = h.get('meta') or {}
                results.append({'doc_id': doc_id, 'name': meta.get('label') or 'document',
                                'origin': origin, 'match': 'content',
                                'snippet': (h.get('text') or '')[:140].strip()})
    except Exception as e:
        log.info('[AGENTS/SEARCH] semantic search skipped (%s)', e.__class__.__name__)

    return jsonify({'ok': True, 'results': results[:10]})


@bp.route('/api/agents/upload', methods=['POST'])
@auth_required
def upload():
    workshop_id = request.form.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'ok': False, 'error': "multipart field 'file' is required"}), 400
    data = f.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return jsonify({'ok': False, 'error': 'file too large (max 15 MB)'}), 413
    ext = f.filename.rsplit('.', 1)[-1] if '.' in f.filename else ''
    try:
        from app.services.rag.file_extractor import extract_text_from_bytes
        text = extract_text_from_bytes(data, mime_type=f.mimetype or '', file_ext=ext) or ''
    except Exception as e:
        log.warning('[AGENTS/UPLOAD] extraction failed (%s): %s', e.__class__.__name__, e)
        text = ''
    text = text.strip()
    truncated = len(text) > _MAX_EXTRACT_CHARS
    if truncated:
        text = text[:_MAX_EXTRACT_CHARS]
    if not text:
        return jsonify({
            'ok': False,
            'error': f'could not extract any text from "{f.filename}" '
                     '(supported: PDF, DOCX, XLSX, PPTX, CSV, HTML, TXT, MD, ZIP)',
        }), 200
    log.info('[AGENTS/UPLOAD] %s -> %d chars%s', f.filename, len(text),
             ' (truncated)' if truncated else '')

    user = current_user() or {}
    record = prepare_docs.register(workshop_id, f.filename, text,
                                   uploaded_by=user.get('name') or user.get('email') or '',
                                   file_bytes=data)
    if not record:
        return jsonify({'ok': False, 'error': 'could not register the document (database unavailable)'}), 200

    # Copilot memory: index the extracted text into the RAG corpus (vector)
    # AND the graph (entity/relationship extraction) — retrieval then
    # grounds every later agent/chat call. Best-effort: degrades to a
    # no-op when Bedrock/FAISS/Neo4j are absent, and neither ever fails
    # the upload itself. Run sequentially in ONE background thread (not
    # two independent ones) so there's a single, unambiguous point to
    # flip prepare_docs.status to 'ingested'/'failed' once BOTH finish —
    # this is what backs the Pre-Workshop "Source Artifacts" status pill
    # (Queued -> Parsing -> Ingested/Failed), which had no real state to
    # read before this.
    def _index_both(name: str, doc_id: str, body: str):
        vector_ok = graph_ok = False
        try:
            from app.services import rag
            if rag.is_enabled():
                # workflow_id reused as "workshop id" — the RAG subsystem's
                # existing scoping dimension, so retrieval (_rag_block) can
                # filter to THIS workshop's documents only.
                n = rag.index_document(doc_id=f'prepare-doc:{doc_id}', text=body,
                                       metadata={'label': name, 'kind': 'prepare_document',
                                                'workflow_id': str(workshop_id), 'doc_id': doc_id},
                                       tag='[AGENTS/UPLOAD/RAG]')
                log.info('[AGENTS/UPLOAD/RAG] %s -> %d chunks indexed', name, n)
            vector_ok = True
        except Exception as e:
            log.info('[AGENTS/UPLOAD/RAG] vector indexing skipped for %s (%s)',
                     name, e.__class__.__name__)
        try:
            from app.services import graph_rag
            n = graph_rag.extract_and_store(board_id=str(workshop_id), doc_id=doc_id, name=name, text=body)
            log.info('[AGENTS/UPLOAD/GRAPH] %s -> %d entities extracted', name, n)
            graph_ok = True
        except Exception as e:
            log.info('[AGENTS/UPLOAD/GRAPH] graph indexing skipped for %s (%s)',
                     name, e.__class__.__name__)
        # Best-effort either way — a subsystem being unreachable (Neo4j
        # down, no Bedrock creds) degrades the doc to 'failed' rather than
        # leaving it stuck on 'parsing' forever, but never raises.
        prepare_docs.set_status(workshop_id, doc_id, 'ingested' if (vector_ok or graph_ok) else 'failed')
        # Warm the workshop-context cache (per-document distillation +
        # corpus intent) NOW, on this same background thread's tail, so
        # deepresearch/workflow/Copilot find it already built instead of
        # paying the distillation on their first run. Incremental: only
        # this new document gets an LLM call (see workshop_context.ensure).
        try:
            from app.services import workshop_context
            workshop_context.refresh_async(workshop_id)
        except Exception as e:
            log.info('[AGENTS/UPLOAD] workshop-context refresh skipped (%s)', e.__class__.__name__)

    prepare_docs.set_status(workshop_id, record['doc_id'], 'parsing')
    import threading
    threading.Thread(target=_index_both, args=(f.filename, record['doc_id'], text),
                     name='doc-index-upload', daemon=True).start()

    return jsonify({'ok': True, 'name': f.filename, 'chars': len(text),
                    'truncated': truncated, 'text': text, 'doc_id': record['doc_id']})


@bp.route('/api/agents/prepare-docs', methods=['GET'])
@auth_required
def list_prepare_docs():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    return jsonify({'ok': True, 'docs': prepare_docs.list_docs(workshop_id)})


@bp.route('/api/agents/generated-docs', methods=['GET'])
@auth_required
def list_generated_docs():
    """For the Pre-Workshop 'Artifacts' card grid — status/completion/
    author/description/tags per generated draft, scoped to one workshop."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    return jsonify({'ok': True, 'docs': generated_docs.list_docs(workshop_id)})


@bp.route('/api/agents/analysis-progress', methods=['GET'])
@auth_required
def analysis_progress():
    """Live step trace for the Pre-Workshop Analysis agent — the 'analyze'
    counterpart of /research-chain (same research_runs ledger, separated
    by agent_id so the two progress UIs never clobber each other).
    Polled by the dashboard while a run is in flight."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.postgres import session_scope
    from app.postgres.repositories import research_runs as repo
    with session_scope() as s:
        if s is None:
            return jsonify({'ok': True, 'run': None})
        row = repo.get_latest_for_workshop(s, workshop_id, agent_id='analyze')
        if row is None:
            return jsonify({'ok': True, 'run': None})
        run = {'run_id': row.run_id, 'status': row.status, 'steps': row.steps}
    return jsonify({'ok': True, 'run': run})


@bp.route('/api/agents/document/<doc_id>/analysis', methods=['GET'])
@auth_required
def get_generated_doc_analysis(doc_id):
    """{ok, gaps, readiness, research_topics} for a persisted Pre-Workshop
    Analysis document — backs the Artifacts grid's scorecard modal after
    a reload. 404 when this doc has no analysis payload."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    payload = generated_docs.get_analysis(workshop_id, doc_id)
    if payload is None:
        return jsonify({'ok': False, 'error': 'no analysis for this document'}), 404
    return jsonify({'ok': True, **payload})


@bp.route('/api/agents/document/<doc_id>/diagram', methods=['GET'])
@auth_required
def get_generated_doc_diagram(doc_id):
    """{ok, xml, diagrams} for a persisted generated doc's workflow
    diagram (see generated_docs.get_diagram) — fetched lazily so the
    Artifacts grid's list payload stays light. 404 if this doc has no
    diagram, isn't found, or belongs to another workshop."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    diagram = generated_docs.get_diagram(workshop_id, doc_id)
    if diagram is None:
        return jsonify({'ok': False, 'error': 'no diagram for this document'}), 404
    return jsonify({'ok': True, **diagram})


@bp.route('/api/agents/research-chain', methods=['GET'])
@auth_required
def research_chain():
    """For the Pre-Workshop 'Research Chain' timeline — the most recent
    deepresearch run's step-by-step progress, insights and confidence for
    this workshop. Polled by the dashboard (~2s) while status=='running'."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.postgres import session_scope
    from app.postgres.repositories import research_runs as repo
    with session_scope() as s:
        if s is None:
            return jsonify({'ok': True, 'run': None})
        row = repo.get_latest_for_workshop(s, workshop_id)
        if row is None:
            return jsonify({'ok': True, 'run': None})
        run = {'run_id': row.run_id, 'status': row.status, 'steps': row.steps,
              'insights': row.insights, 'confidence': row.confidence,
              'doc_count': row.doc_count, 'web_count': row.web_count,
              'diagram': row.diagram, 'next_steps': row.next_steps}
    return jsonify({'ok': True, 'run': run})


@bp.route('/api/agents/document/<doc_id>', methods=['GET'])
@auth_required
def get_prepare_doc(doc_id):
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    text = prepare_docs.get_text(workshop_id, doc_id)
    if text is not None:
        name = next((d['name'] for d in prepare_docs.list_docs(workshop_id) if d['doc_id'] == doc_id), '')
        return jsonify({'ok': True, 'name': name, 'text': text})

    from app.services import generated_docs
    html = generated_docs.get_html(workshop_id, doc_id)
    if html is not None:
        from app.services.rag.chunking import html_to_text
        name = generated_docs.get_name(workshop_id, doc_id) or ''
        return jsonify({'ok': True, 'name': name, 'text': html_to_text(html)})

    return jsonify({'ok': False, 'error': 'document not found'}), 404


_VIEW_MIME_BY_EXT = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'csv': 'text/csv', 'txt': 'text/plain', 'md': 'text/markdown', 'html': 'text/html', 'htm': 'text/html',
}


@bp.route('/api/agents/document/<doc_id>/file', methods=['GET'])
@auth_required
def get_prepare_doc_file(doc_id):
    """Raw original bytes of an uploaded source document — the document
    viewer's PDF <iframe> src and DOCX-via-mammoth fetch both hit this.
    Only ever the ORIGINAL uploaded file (see prepare_docs.register's
    file_bytes); generated docs have no "original file", only HTML."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    data = prepare_docs.get_original_bytes(workshop_id, doc_id)
    if data is None:
        return jsonify({'ok': False, 'error': 'original file not available for this document '
                                              '(uploaded before viewing was added, or not a source document)'}), 404
    name = next((d['name'] for d in prepare_docs.list_docs(workshop_id) if d['doc_id'] == doc_id), doc_id)
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    resp = Response(data, mimetype=_VIEW_MIME_BY_EXT.get(ext, 'application/octet-stream'))
    resp.headers['Content-Disposition'] = f'inline; filename="{name}"'
    return resp


@bp.route('/api/agents/document/<doc_id>/view', methods=['GET'])
@auth_required
def view_document(doc_id):
    """Tells the document-viewer modal HOW to render a document, so the
    frontend doesn't need its own per-format rules: -> {ok, kind, name,
    ...}. `kind` drives the frontend:
      'pdf'/'docx'  -> fetch raw bytes from the /file route above
                       (native <iframe> for pdf, mammoth.js for docx).
      'html'        -> pre-rendered HTML already in the response
                       (server-side xlsx_to_html, or a generated doc's
                       own body_html — never a client-side xlsx parser;
                       see file_extractor.xlsx_to_html's docstring for why).
      'text'        -> plain-text fallback (PPTX and anything else with
                       no real renderer) — still the extracted text, not
                       an error.
    """
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400

    docs_by_id = {d['doc_id']: d for d in prepare_docs.list_docs(workshop_id)}
    if doc_id in docs_by_id:
        name = docs_by_id[doc_id]['name']
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        file_url = f'/api/agents/document/{doc_id}/file?workshop_id={workshop_id}'
        if ext == 'pdf':
            return jsonify({'ok': True, 'kind': 'pdf', 'name': name, 'file_url': file_url, 'origin': 'source'})
        if ext == 'docx':
            return jsonify({'ok': True, 'kind': 'docx', 'name': name, 'file_url': file_url, 'origin': 'source'})
        if ext == 'xlsx':
            data = prepare_docs.get_original_bytes(workshop_id, doc_id)
            if data is not None:
                from app.services.rag.file_extractor import xlsx_to_html
                try:
                    return jsonify({'ok': True, 'kind': 'html', 'name': name, 'html': xlsx_to_html(data), 'origin': 'source'})
                except Exception as e:
                    log.warning('[AGENTS/VIEW] xlsx render failed for %s (%s)', doc_id, e.__class__.__name__)
            # No original bytes (pre-dates this feature) or render failed — fall back to extracted text below.
        text = prepare_docs.get_text(workshop_id, doc_id)
        return jsonify({'ok': True, 'kind': 'text', 'name': name, 'text': text or '', 'origin': 'source'})

    from app.services import generated_docs
    html = generated_docs.get_html(workshop_id, doc_id)
    if html is not None:
        name = generated_docs.get_name(workshop_id, doc_id) or ''
        return jsonify({'ok': True, 'kind': 'html', 'name': name, 'html': html, 'origin': 'generated'})

    return jsonify({'ok': False, 'error': 'document not found'}), 404


@bp.route('/api/agents/document/<doc_id>/word', methods=['GET'])
@auth_required
def download_generated_doc_word(doc_id):
    """Export a generated document (research brief, risk assessment,
    workflow write-up, ...) as a .docx — the "download as Word" affordance
    on the Pre-Workshop Artifacts grid / document viewer. Only ever a
    GENERATED doc (see app.services.docx_export) — source uploads already
    have their own original file, served by the /file route above."""
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    from app.services.docx_export import html_to_docx_bytes
    record = generated_docs.get(workshop_id, doc_id)
    html = generated_docs.get_html(workshop_id, doc_id)
    if record is None or html is None:
        return jsonify({'ok': False, 'error': 'document not found'}), 404
    meta = [
        ('Author', record.get('author') or ''),
        ('Category', record.get('category') or ''),
        ('Status', (record.get('status') or '').replace('_', ' ').title()),
    ]
    data = html_to_docx_bytes(record['name'], html, meta=meta)
    resp = Response(data, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    safe_name = re.sub(r'[^\w\-. ]+', '_', record['name']) or doc_id
    if not safe_name.lower().endswith('.docx'):
        safe_name += '.docx'
    resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return resp


@bp.route('/api/agents/document/<doc_id>', methods=['DELETE'])
@auth_required
def delete_prepare_doc(doc_id):
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    was_source = prepare_docs.delete(workshop_id, doc_id)
    ok = was_source
    if not ok:
        from app.services import generated_docs
        ok = generated_docs.delete(workshop_id, doc_id)
    try:
        from app.services import graph_rag
        graph_rag.delete_document(board_id=str(workshop_id), doc_id=doc_id)
    except Exception as e:
        log.info('[AGENTS/DOCUMENT/DELETE] graph cleanup skipped (%s)', e.__class__.__name__)
    if was_source:
        # A source document left the corpus — refresh the workshop-context
        # cache so its distillation (and the corpus intent/suggestions)
        # drop out. Incremental: removal costs no distill calls, only the
        # small intent/suggestions recompute.
        try:
            from app.services import workshop_context
            workshop_context.refresh_async(workshop_id)
        except Exception as e:
            log.info('[AGENTS/DOCUMENT/DELETE] context refresh skipped (%s)', e.__class__.__name__)
    return jsonify({'ok': ok})


def install(app) -> None:
    app.register_blueprint(bp)
