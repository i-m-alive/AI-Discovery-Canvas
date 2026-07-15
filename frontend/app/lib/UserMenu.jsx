'use client';

import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from './api';
import { Icon } from './icons';
import { toggleTheme, useTheme } from './theme';

// The avatar dropdown on every shell page: identity, AI-model picker,
// theme switch and sign-out. `avatarClassName` lets each shell keep its
// own avatar look (.pw-avatar on the workshop topbar, .av on projects).
//
// AI model: GET /api/settings/llm lists both backends (AWS Bedrock /
// Azure OpenAI) with their configured state; picking one POSTs the
// choice, which the backend persists per user (users.llm_provider) and
// applies to every LLM call from their next request on. Unconfigured
// backends render disabled with the reason in the tooltip.
//
// Sign-out: POST /auth/logout revokes the server session AND clears the
// cookie (see backend/app/auth/routes.py) — then a hard navigation to
// /login so no in-memory state survives.
export default function UserMenu({ user, avatarClassName = 'pw-avatar' }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [llm, setLlm] = useState(null);          // {provider, choice, providers[]}
  const [llmBusy, setLlmBusy] = useState(false);
  const [llmError, setLlmError] = useState(null);
  const theme = useTheme();
  const ref = useRef(null);

  const label = (user && (user.name || user.email)) || '';
  const initials = (label || '?').trim().slice(0, 1).toUpperCase();

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Fetch the provider list when the menu opens (fresh each open — the
  // configured state can change when the operator edits .env).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet('/api/settings/llm');
        if (!cancelled && data && data.ok) { setLlm(data); setLlmError(null); }
      } catch {
        if (!cancelled) setLlmError('Could not load AI model settings');
      }
    })();
    return () => { cancelled = true; };
  }, [open]);

  async function pickProvider(id) {
    if (llmBusy || !llm || llm.provider === id) return;
    setLlmBusy(true);
    setLlmError(null);
    try {
      const data = await apiPost('/api/settings/llm', { provider: id });
      if (data && data.ok) setLlm((s) => ({ ...s, provider: data.provider, choice: data.choice }));
      else setLlmError((data && data.error) || 'Could not switch model');
    } catch (err) {
      setLlmError(err.message || 'Could not switch model');
    } finally {
      setLlmBusy(false);
    }
  }

  async function signOut() {
    if (busy) return;
    setBusy(true);
    try { await apiPost('/auth/logout'); } catch { /* cookie may already be dead — still leave */ }
    window.location.assign('/login');
  }

  if (!user) return null;

  return (
    <div className="um" ref={ref}>
      <button
        type="button" className="um-trigger" onClick={() => setOpen((o) => !o)}
        title={label} aria-haspopup="menu" aria-expanded={open}
      >
        <span className={avatarClassName}>{initials}</span>
      </button>
      {open && (
        <div className="um-menu" role="menu">
          <div className="um-id">
            <div className="um-name">{user.name || 'Signed in'}</div>
            {user.email && <div className="um-email">{user.email}</div>}
          </div>

          <div className="um-sec">AI model</div>
          {llmError && <div className="um-note um-note-err">{llmError}</div>}
          {!llm && !llmError && <div className="um-note">Loading…</div>}
          {llm && (llm.providers || []).map((p) => {
            const active = llm.provider === p.id;
            const disabled = !p.configured || llmBusy;
            return (
              <button
                key={p.id} type="button" role="menuitemradio" aria-checked={active}
                className={'um-item um-radio' + (active ? ' on' : '')}
                disabled={disabled}
                title={p.configured
                  ? `${p.label} — ${p.model}${p.embedding_model ? ` · embeddings: ${p.embedding_model}` : ''}`
                  : (p.errors || []).join('; ') || 'Not configured'}
                onClick={() => pickProvider(p.id)}
              >
                <span className={'um-radio-dot' + (active ? ' on' : '')} />
                <span className="um-radio-txt">
                  {p.label}
                  <span className="um-radio-model">{p.configured ? p.model : 'not configured'}</span>
                </span>
                {active && <span className="um-item-hint"><Icon name="check" /></span>}
              </button>
            );
          })}

          <div className="um-sep" />
          <button type="button" className="um-item" role="menuitem" onClick={toggleTheme}>
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
            {theme === 'dark' ? 'Light theme' : 'Dark theme'}
            <span className="um-item-hint">{theme === 'dark' ? 'ON' : ''}</span>
          </button>
          <div className="um-sep" />
          <button type="button" className="um-item danger" role="menuitem" onClick={signOut} disabled={busy}>
            <Icon name="logout" />
            {busy ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      )}
    </div>
  );
}
