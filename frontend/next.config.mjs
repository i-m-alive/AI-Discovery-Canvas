/** @type {import('next').NextConfig} */
const nextConfig = {
  // Next.js's OWN dev-server proxy for rewrites has a default 30-second
  // timeout — confirmed by direct reproduction: a real 70-90s agent call
  // (deepresearch, which chains several sequential Bedrock calls) died at
  // exactly 30.0s with Next's generic plain-text "Internal Server Error",
  // even though the Flask backend had completed (or was still correctly
  // running) underneath — this was NEVER a backend bug. Raised to 5
  // minutes, comfortably above the slowest agent pipeline today.
  experimental: {
    proxyTimeout: 300_000,
  },
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
      { source: '/api/canvas/:path*', destination: `${backend}/api/canvas/:path*` },
      { source: '/api/projects/:path*', destination: `${backend}/api/projects/:path*` },
      { source: '/api/workshops/:path*', destination: `${backend}/api/workshops/:path*` },
      { source: '/api/integrations/:path*', destination: `${backend}/api/integrations/:path*` },
      { source: '/api/export/:path*', destination: `${backend}/api/export/:path*` },
      { source: '/healthz', destination: `${backend}/healthz` },
      { source: '/readyz', destination: `${backend}/readyz` },
    ];
  },
};

export default nextConfig;
