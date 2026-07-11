'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiGet, apiPost } from '../lib/api';
import { useObTheme } from '../lib/useObTheme';
import { getMsalInstance, loginRequest, isAzureConfigured } from '../lib/msalConfig';
import '../shared.css';

// Two sign-in paths, both landing on the same backend session cookie:
//
//  1. Microsoft ("Sign in with Microsoft") — MSAL's loginPopup() runs the
//     whole OAuth2+PKCE handshake in the browser against NaviCore's real
//     Entra app registration (public client, no secret anywhere). The
//     resulting id_token is POSTed to /auth/login/azure, which VERIFIES
//     its signature server-side (app/auth/token_validation.py) before
//     minting a session — the browser is never trusted blindly.
//
//  2. Mock — unchanged from Phase 1, still the dev-only fallback (any
//     name/email). Kept side-by-side rather than replaced: useful for
//     quick local testing without a Microsoft account, and it's what the
//     backend's AUTH_MODE=mock default already wires up.
//
// The Microsoft button only renders once GET /auth/config confirms the
// backend actually has AZURE_TENANT_ID/AZURE_CLIENT_ID configured — no
// point offering a button that can only fail.
//
// Styled with app/shared.css — the same color tokens/button-card shapes
// as the canvas itself (app/canvas/canvas.css), so this doesn't read as
// a bootstrap form bolted onto a different-looking product.
export default function LoginPage() {
  const router = useRouter();
  const [theme, flipTheme] = useObTheme();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [azureReady, setAzureReady] = useState(false);
  const [msBusy, setMsBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await apiGet('/auth/config');
        if (!cancelled) setAzureReady(Boolean(cfg && cfg.azure_configured) && isAzureConfigured());
      } catch {
        /* backend unreachable — Microsoft button just stays hidden */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await apiPost('/auth/login/mock', { name, email });
      router.push('/projects');
    } catch (err) {
      setError(err.message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  }

  async function handleMicrosoftSignIn() {
    setError(null);
    setMsBusy(true);
    try {
      const msal = getMsalInstance();
      if (!msal) throw new Error('Microsoft sign-in is only available in the browser');
      await msal.ready;
      const result = await msal.instance.loginPopup(loginRequest);
      // result.account carries the MSAL profile; idToken is what the
      // backend verifies against Microsoft's JWKS (see azure_ad.py).
      await apiPost('/auth/login/azure', {
        name: result.account?.name || '',
        email: result.account?.username || '',
        id_token: result.idToken,
        home_account_id: result.account?.homeAccountId || '',
        tenant_id: result.account?.tenantId || '',
      });
      router.push('/projects');
    } catch (err) {
      // MSAL throws its own error shapes (interaction_in_progress,
      // popup_window_error, user_cancelled, ...) — surface the message
      // as-is rather than guessing a friendlier one that might be wrong.
      setError(err?.message || 'Microsoft sign-in failed');
    } finally {
      setMsBusy(false);
    }
  }

  return (
    <div
      className="app-shell"
      data-obtheme={theme}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backgroundImage: 'radial-gradient(circle at 50% 0%, var(--accent-weak), var(--bg) 60%)',
      }}
    >
      <button
        className="themebtn"
        onClick={flipTheme}
        title="Toggle theme"
        style={{ position: 'fixed', top: 16, right: 16 }}
      >{theme === 'dark' ? '☀' : '☾'}</button>
      <div style={{
        width: 'min(400px, 92vw)', background: 'var(--surface)', border: '1px solid var(--line)',
        borderRadius: 'var(--radius-xl)', boxShadow: 'var(--shadow-lg)', padding: '36px 32px',
        animation: 'app-fadeUp 260ms var(--ease) both',
      }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 28 }}>
          <span className="ob-mark" style={{
            width: 36, height: 36, borderRadius: 12, display: 'grid', placeItems: 'center',
            color: '#fff', fontWeight: 800, fontSize: 15,
            background: 'linear-gradient(135deg, var(--accent), var(--brand-2))',
            boxShadow: '0 4px 14px -4px var(--accent)',
          }}
          >N</span>
          <div style={{ lineHeight: 1.25 }}>
            <h1 style={{ fontSize: 17, fontWeight: 800, letterSpacing: '-0.01em', margin: 0 }}>
              Navi<b style={{ color: 'var(--accent)' }}>BA</b> Orbitz
            </h1>
            <div style={{ fontSize: 11, color: 'var(--faint)' }}>Engagement Intelligence</div>
          </div>
        </div>

        {azureReady && (
          <>
            <button
              type="button"
              onClick={handleMicrosoftSignIn}
              disabled={msBusy}
              className="btn"
              style={{ width: '100%', padding: '11px 14px', fontSize: 13.5 }}
            >
              <svg width="16" height="16" viewBox="0 0 21 21" aria-hidden="true">
                <rect x="1" y="1" width="9" height="9" fill="#f25022" />
                <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
                <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
                <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
              </svg>
              {msBusy ? 'Signing in…' : 'Sign in with Microsoft'}
            </button>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '20px 0', color: 'var(--muted)', fontSize: 12 }}>
              <div style={{ flex: 1, height: 1, background: 'var(--line)' }} />
              or
              <div style={{ flex: 1, height: 1, background: 'var(--line)' }} />
            </div>
          </>
        )}

        <p style={{ color: 'var(--muted)', fontSize: 12.5, marginBottom: 14 }}>
          Sign in (mock auth — dev only, any name/email works).
        </p>
        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="username"
            required
            className="field"
          />
          <input
            type="text"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="xyz@example.com"
            required
            className="field"
          />
          <button type="submit" disabled={busy} className="btn solid" style={{ width: '100%', marginTop: 4 }}>
            {busy ? 'Signing in…' : 'Sign in (mock)'}
          </button>
          {error && <p className="app-error">⚠ {error}</p>}
        </form>
      </div>
    </div>
  );
}
