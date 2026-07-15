'use client';

import { useEffect, useState } from 'react';

// Theme state lives on <html data-theme="..."> + localStorage('aidc-theme').
// The initial value is applied pre-paint by the inline script in
// app/layout.js — this module only reads/toggles it afterwards. A custom
// 'aidc-theme' event keeps every mounted toggle (topbar button, user menu)
// in sync without a context provider.

const KEY = 'aidc-theme';

export function currentTheme() {
  if (typeof document === 'undefined') return 'light';
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

export function setTheme(theme) {
  const t = theme === 'dark' ? 'dark' : 'light';
  const root = document.documentElement;
  // Scope the global cross-fade to the flip itself (see globals.css).
  root.classList.add('theme-anim');
  root.setAttribute('data-theme', t);
  try { window.localStorage.setItem(KEY, t); } catch { /* private mode */ }
  window.setTimeout(() => root.classList.remove('theme-anim'), 350);
  window.dispatchEvent(new CustomEvent('aidc-theme', { detail: t }));
}

export function toggleTheme() {
  setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

// Hook: returns the live theme ('light' | 'dark'), re-rendering on toggle.
export function useTheme() {
  const [theme, set] = useState('light');
  useEffect(() => {
    set(currentTheme());
    const onChange = (e) => set(e.detail || currentTheme());
    window.addEventListener('aidc-theme', onChange);
    return () => window.removeEventListener('aidc-theme', onChange);
  }, []);
  return theme;
}
