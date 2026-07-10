'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiGet, apiPost } from '../lib/api';

// Proof-of-plumbing page. On mount, calls GET /auth/me (see
// backend/app/auth/routes.py) — the copied auth middleware attaches
// `g.current_user` from the session cookie, and this route reports it as
// { mode, authenticated, user }. A 401/`authenticated: false` bounces to
// /login; otherwise we render the user and a button that exercises the
// full backbone: Next.js -> next.config.mjs rewrite -> Flask ->
// auth-gated route -> app.services.llm_service -> Azure OpenAI.
export default function CanvasPage() {
  const router = useRouter();
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [pingResult, setPingResult] = useState(null);
  const [pinging, setPinging] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet('/auth/me');
        if (cancelled) return;
        if (!data || !data.authenticated) {
          router.replace('/login');
          return;
        }
        setUser(data.user);
      } catch {
        if (!cancelled) router.replace('/login');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handlePing() {
    setPinging(true);
    setPingResult(null);
    try {
      const result = await apiPost('/api/agents/ping', {});
      setPingResult(result);
    } catch (err) {
      setPingResult({ ok: false, error: err.message || String(err) });
    } finally {
      setPinging(false);
    }
  }

  if (loading) {
    return <main style={{ padding: 40, fontFamily: 'system-ui, sans-serif' }}>Loading…</main>;
  }

  return (
    <main style={{ maxWidth: 640, margin: '40px auto', fontFamily: 'system-ui, sans-serif' }}>
      <h1>AI Discovery Canvas — backbone scaffold</h1>

      <section style={{ marginBottom: 24 }}>
        <h2>Signed in as</h2>
        <pre style={{ background: '#f0f0f0', padding: 12, overflowX: 'auto' }}>
          {JSON.stringify(user, null, 2)}
        </pre>
      </section>

      <section>
        <h2>Agent backbone check</h2>
        <p>
          Calls <code>POST /api/agents/ping</code>, which is auth-gated and calls the
          copied <code>llm_service</code>. If Azure OpenAI isn&apos;t configured yet in
          <code> backend/.env</code>, this will report a clear <code>ok: false</code> error
          instead of crashing — that&apos;s expected until credentials are added.
        </p>
        <button onClick={handlePing} disabled={pinging} style={{ padding: 10 }}>
          {pinging ? 'Testing…' : 'Test agent backbone'}
        </button>
        {pingResult && (
          <pre style={{ background: '#f0f0f0', padding: 12, marginTop: 16, overflowX: 'auto' }}>
            {JSON.stringify(pingResult, null, 2)}
          </pre>
        )}
      </section>
    </main>
  );
}
