/** @type {import('next').NextConfig} */
const nextConfig = {
  // Dev-time proxy to the Flask backend (see ../backend, :5101). Keeping
  // these paths same-origin (served through :3000) means the session
  // cookie Flask sets (`navicore_session`) works without any CORS/cookie
  // -domain gymnastics — the browser just thinks it's talking to one
  // origin. Note: rewrites only apply during `next dev`/`next start`;
  // a real deployment would front both processes with a real reverse
  // proxy (nginx, Azure Front Door, etc.) doing the same job.
  async rewrites() {
    const backend = 'http://localhost:5101';
    return [
      { source: '/auth/:path*', destination: `${backend}/auth/:path*` },
      { source: '/connections/:path*', destination: `${backend}/connections/:path*` },
      { source: '/api/agents/:path*', destination: `${backend}/api/agents/:path*` },
      { source: '/healthz', destination: `${backend}/healthz` },
      { source: '/readyz', destination: `${backend}/readyz` },
    ];
  },
};

export default nextConfig;
