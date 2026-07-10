// Small fetch wrapper for talking to the Flask backend.
//
// Paths passed in here are relative (e.g. '/auth/me', '/api/agents/ping')
// because next.config.mjs's rewrites() proxies them to the Flask backend
// at :5101 while keeping the browser same-origin (:3000). That's what
// lets the `navicore_session` cookie Flask sets travel with every
// request without any CORS/cookie-domain configuration.

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request(path, options) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  let parsed = null;
  const text = await res.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    const message =
      (parsed && typeof parsed === 'object' && (parsed.error || parsed.message)) ||
      `Request failed: ${res.status}`;
    throw new ApiError(message, res.status, parsed);
  }

  return parsed;
}

export function apiGet(path) {
  return request(path, { method: 'GET' });
}

export function apiPost(path, body) {
  return request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });
}

export { ApiError };
