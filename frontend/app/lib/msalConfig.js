/*
 * MSAL configuration for Microsoft Entra ID (Azure AD) — Phase 5.
 *
 * Ported from frd-generator's authConfig.js (same tenant, same app
 * registration — it's a public-client SPA app using PKCE, so there is
 * NO client secret anywhere in this flow; MSAL does the whole OAuth
 * handshake in the browser). Adapted for Next.js: env vars must be
 * prefixed NEXT_PUBLIC_ to reach the browser bundle (Vite used
 * import.meta.env.VITE_* instead — different bundler, same idea).
 *
 * redirectUri is `window.location.origin`, matching what's actually
 * registered on the Azure app registration: http://localhost:5173.
 * That's WHY frontend/package.json pins the dev server to that exact
 * port (`next dev -p 5173`) — MSAL's redirect must match a registered
 * URI byte-for-byte, and reusing NaviCore's already-registered dev URI
 * means zero Azure Portal changes are needed to test this locally.
 */

import { PublicClientApplication, LogLevel } from '@azure/msal-browser';

const tenantId = process.env.NEXT_PUBLIC_AZURE_TENANT_ID || '';
const clientId = process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || '';

export const msalConfig = {
  auth: {
    clientId,
    authority: tenantId
      ? `https://login.microsoftonline.com/${tenantId}`
      : 'https://login.microsoftonline.com/common',
    redirectUri: typeof window !== 'undefined' ? window.location.origin : 'http://localhost:5173',
    postLogoutRedirectUri: typeof window !== 'undefined'
      ? window.location.origin + '/login'
      : 'http://localhost:5173/login',
    navigateToLoginRequestUrl: false,
  },
  cache: {
    cacheLocation: 'sessionStorage',
    storeAuthStateInCookie: false,
  },
  system: {
    loggerOptions: {
      logLevel: LogLevel.Warning,
      piiLoggingEnabled: false,
      loggerCallback: (level, message) => {
        if (level === LogLevel.Error) console.error('[MSAL]', message);
        if (level === LogLevel.Warning) console.warn('[MSAL]', message);
      },
    },
  },
};

// openid+profile+email give the id_token a name/UPN; User.Read gets an
// access token for Graph /me (unused today, but incremental-consent-free
// for later — e.g. avatar photo — since it's requested up front).
export const loginRequest = { scopes: ['openid', 'profile', 'email', 'User.Read'] };

export function isAzureConfigured() {
  return Boolean(clientId && tenantId);
}

// Lazily constructed + initialized singleton (MSAL v3 requires
// `await instance.initialize()` before any other call, and Next.js
// renders this module on the server too, where `window` doesn't exist —
// so construction is deferred to the browser via getMsalInstance()).
let _instance = null;
let _initPromise = null;

export function getMsalInstance() {
  if (typeof window === 'undefined') return null;
  if (!_instance) _instance = new PublicClientApplication(msalConfig);
  if (!_initPromise) _initPromise = _instance.initialize();
  return { instance: _instance, ready: _initPromise };
}
