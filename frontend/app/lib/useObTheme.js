'use client';

// Shared Orbitz theme state — same localStorage key the workshop
// workspace (OrbitzApp) uses, so login/projects/canvas stay in one theme.
import { useCallback, useEffect, useState } from 'react';

export function useObTheme() {
  const [theme, setTheme] = useState('dark');
  useEffect(() => {
    try {
      const t = localStorage.getItem('orbitz-theme');
      if (t === 'light' || t === 'dark') setTheme(t);
    } catch {}
  }, []);
  const flip = useCallback(() => {
    setTheme((cur) => {
      const next = cur === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('orbitz-theme', next); } catch {}
      return next;
    });
  }, []);
  return [theme, flip];
}
