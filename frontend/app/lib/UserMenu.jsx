'use client';

import { useEffect, useRef, useState } from 'react';
import { apiPost } from './api';
import { Icon } from './icons';
import { toggleTheme, useTheme } from './theme';

// The avatar dropdown on every shell page: identity, theme switch and the
// sign-out this app never had. `avatarClassName` lets each shell keep its
// own avatar look (.pw-avatar on the workshop topbar, .av on projects).
// Sign-out: POST /auth/logout revokes the server session AND clears the
// cookie (see backend/app/auth/routes.py) — then a hard navigation to
// /login so no in-memory state survives.
export default function UserMenu({ user, avatarClassName = 'pw-avatar' }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
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
