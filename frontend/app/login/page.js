'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiPost } from '../lib/api';

// The copied backend auth defaults to AUTH_MODE=mock (see
// backend/app/auth/providers/mock.py). MockAuthProvider.login() requires
// a non-empty `name` and `email` (any string works — it's dev-only, no
// real identity check), so this is the smallest form that satisfies it:
// no password, no validation beyond "not empty". POSTs to
// /auth/login/mock (see backend/app/auth/routes.py) which sets the
// session cookie and returns { user, session }.
export default function LoginPage() {
  const router = useRouter();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await apiPost('/auth/login/mock', { name, email });
      router.push('/canvas');
    } catch (err) {
      setError(err.message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 360, margin: '80px auto', fontFamily: 'system-ui, sans-serif' }}>
      <h1>AI Discovery Canvas</h1>
      <p>Sign in (mock auth — dev only, any name/email works).</p>
      <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <label>
          Name
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="username"
            required
            style={{ display: 'block', width: '100%', padding: 8, marginTop: 4 }}
          />
        </label>
        <label>
          Email
          <input
            type="text"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="xyz@example.com"
            required
            style={{ display: 'block', width: '100%', padding: 8, marginTop: 4 }}
          />
        </label>
        <button type="submit" disabled={busy} style={{ padding: 10 }}>
          {busy ? 'Signing in…' : 'Sign in (mock)'}
        </button>
        {error && <p style={{ color: 'crimson' }}>{error}</p>}
      </form>
    </main>
  );
}
