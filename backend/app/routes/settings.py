"""
User-level platform settings.

Currently one setting: which LLM backend serves this user's calls
('bedrock' | 'azure_openai'). Chosen from the header avatar menu
(frontend/app/lib/UserMenu.jsx), persisted on users.llm_provider,
resolved per-call by app/services/llm_service.py.

    GET  /api/settings/llm
        → { ok, provider,          # effective backend for this user
             choice,               # their stored pick, or null (default)
             default,              # the platform default (LLM_PROVIDER env)
             providers: [ {id, label, model, configured, errors[]} ] }

    POST /api/settings/llm   {"provider": "azure_openai" | "bedrock" | null}
        null clears the choice back to the platform default. Picking a
        backend that isn't configured is a 400 — the UI shows those
        options disabled, this is just the honest server-side guard.

Both routes sit behind the global auth gate (install_auth's
before_request) — no anonymous access.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from app.auth.middleware import current_user
from app.services import llm_prefs, llm_service

log = logging.getLogger("app.routes.settings")


def _provider_list() -> list[dict]:
    status = llm_service.providers_status()
    return [
        {"id": pid, **info}
        for pid, info in status.items()
    ]


def install(flask_app: Flask) -> None:

    @flask_app.get("/api/settings/llm")
    def get_llm_settings():
        user = current_user() or {}
        email = user.get("email") or ""
        choice = llm_prefs.get_for(email) if email else None
        effective = choice or llm_service.DEFAULT_PROVIDER
        return jsonify({
            "ok": True,
            "provider": effective,
            "choice": choice,
            "default": llm_service.DEFAULT_PROVIDER,
            "providers": _provider_list(),
        })

    @flask_app.post("/api/settings/llm")
    def set_llm_settings():
        user = current_user() or {}
        email = user.get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "no user email on session"}), 400

        body = request.get_json(silent=True) or {}
        provider = body.get("provider")
        if provider is not None:
            provider = str(provider).strip().lower()
            if provider not in llm_service.VALID_PROVIDERS:
                return jsonify({"ok": False, "error": f"unknown provider '{provider}'"}), 400
            info = llm_service.providers_status().get(provider) or {}
            if not info.get("configured"):
                return jsonify({
                    "ok": False,
                    "error": f"{info.get('label', provider)} is not configured: "
                             + "; ".join(info.get("errors") or ["missing credentials"]),
                }), 400

        if not llm_prefs.set_for(email, provider):
            return jsonify({"ok": False, "error": "could not save preference"}), 500

        effective = provider or llm_service.DEFAULT_PROVIDER
        log.info("[SETTINGS] %s switched LLM backend → %s", email, effective)
        return jsonify({"ok": True, "provider": effective, "choice": provider})
