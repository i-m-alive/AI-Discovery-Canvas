'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiGet } from './api';

// Shared client-side auth gate: calls GET /auth/me, redirects to /login on
// 401/failure, otherwise returns the user object. Used by every top-level
// page (projects list, project detail, canvas) — pulled out here once it
// was needed in more than one place instead of duplicating the same
// ~15-line effect per page.
export function useAuthedUser() {
  const router = useRouter();
  const [user, setUser] = useState(null);

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
        setUser(data.user || {});
      } catch {
        if (!cancelled) router.replace('/login');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  return user;
}
