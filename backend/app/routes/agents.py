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
                             body: { message, context, workshop_id } -> { ok, reply }
                             (reply is plain text; frontend escapes it).

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

Design note: run/chat return HTTP 200 with ok:false on functional
failures (only auth failures are non-200). The route being reached and
auth passing vs. the model call failing are different problems — keeping
them distinguishable in the response body is what makes the frontend's
error rendering (and debugging) sane.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

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
    try:
        draft = agent_catalog.run_agent(agent_id, context, extra=extra, workshop_id=workshop_id)
        return jsonify({'ok': True, 'draft': draft})
    except Exception as e:
        log_exc(f'[AGENTS/RUN/{agent_id}]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


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
        out = agent_catalog.route_chat(message, context, workshop_id=workshop_id)
        if out['kind'] == 'dispatch':
            return jsonify({'ok': True, 'kind': 'dispatch',
                            'agent_id': out['agent_id'], 'extra': out.get('extra')})
        if not out.get('reply'):
            raise RuntimeError('the model returned an empty reply — try again')
        return jsonify({'ok': True, 'kind': 'reply', 'reply': out['reply']})
    except Exception as e:
        log_exc('[AGENTS/CHAT]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


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
                                   uploaded_by=user.get('name') or user.get('email') or '')
    if not record:
        return jsonify({'ok': False, 'error': 'could not register the document (database unavailable)'}), 200

    # Copilot memory: index the extracted text into the RAG corpus (vector)
    # AND the graph (entity/relationship extraction) on daemon threads —
    # retrieval then grounds every later agent/chat call. Best-effort:
    # degrades to a no-op when Bedrock/FAISS/Neo4j are absent, and neither
    # ever fails the upload itself.
    def _index_vector(name: str, doc_id: str, body: str):
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
        except Exception as e:
            log.info('[AGENTS/UPLOAD/RAG] vector indexing skipped for %s (%s)',
                     name, e.__class__.__name__)

    def _index_graph(name: str, doc_id: str, body: str):
        try:
            from app.services import graph_rag
            n = graph_rag.extract_and_store(board_id=str(workshop_id), doc_id=doc_id, name=name, text=body)
            log.info('[AGENTS/UPLOAD/GRAPH] %s -> %d entities extracted', name, n)
        except Exception as e:
            log.info('[AGENTS/UPLOAD/GRAPH] graph indexing skipped for %s (%s)',
                     name, e.__class__.__name__)

    import threading
    threading.Thread(target=_index_vector, args=(f.filename, record['doc_id'], text),
                     name='rag-index-upload', daemon=True).start()
    threading.Thread(target=_index_graph, args=(f.filename, record['doc_id'], text),
                     name='graph-index-upload', daemon=True).start()

    return jsonify({'ok': True, 'name': f.filename, 'chars': len(text),
                    'truncated': truncated, 'text': text, 'doc_id': record['doc_id']})


@bp.route('/api/agents/prepare-docs', methods=['GET'])
@auth_required
def list_prepare_docs():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    return jsonify({'ok': True, 'docs': prepare_docs.list_docs(workshop_id)})


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


@bp.route('/api/agents/document/<doc_id>', methods=['DELETE'])
@auth_required
def delete_prepare_doc(doc_id):
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    ok = prepare_docs.delete(workshop_id, doc_id)
    if not ok:
        from app.services import generated_docs
        ok = generated_docs.delete(workshop_id, doc_id)
    try:
        from app.services import graph_rag
        graph_rag.delete_document(board_id=str(workshop_id), doc_id=doc_id)
    except Exception as e:
        log.info('[AGENTS/DOCUMENT/DELETE] graph cleanup skipped (%s)', e.__class__.__name__)
    return jsonify({'ok': ok})


def install(app) -> None:
    app.register_blueprint(bp)
