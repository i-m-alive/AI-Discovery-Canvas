import { redirect } from 'next/navigation';

// Server component. Unauthenticated users bounce further to /login once
// /canvas's client-side `GET /auth/me` check 401s (see app/canvas/page.js)
// — no Next.js middleware-based auth gating here, that's unnecessary
// complexity for this scaffold.
export default function RootPage() {
  redirect('/canvas');
}
