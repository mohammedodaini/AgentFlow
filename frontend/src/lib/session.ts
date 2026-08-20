/**
 * The session: three httpOnly cookies, and a rule about what the browser holds.
 *
 * **The browser never holds a JWT.** The backend (M3) returns an access token and
 * a refresh token in a JSON body, and the obvious thing to do with them in a SPA
 * is `localStorage.setItem`. That is the decision this module exists to refuse.
 *
 * A refresh token is a seven-day credential that mints access tokens. In
 * `localStorage` it is readable by any script that reaches the page — one
 * dependency with a compromised release, one injected marketing tag — and
 * exfiltrating it is a single line. httpOnly cookies are not readable from
 * JavaScript at all, so the same XSS gets a session on that tab rather than a
 * week of access from anywhere.
 *
 * The cost is that every API call goes through the Next server, which is why
 * `api.ts` is server-only. That cost buys a second thing worth having: the
 * browser only ever talks to its own origin, so there is no CORS configuration
 * and no preflight — the backend never has to allowlist a browser origin at all.
 *
 * Why not encrypt a single session cookie
 * ---------------------------------------
 * The tempting alternative is one encrypted cookie holding both tokens. It would
 * work, and it would mean this frontend owned a second key and a second crypto
 * decision. The backend already signs these tokens and already knows how to
 * revoke a refresh token (`jti` denylist, M3). Storing them as-is keeps exactly
 * one system responsible for their validity.
 */
import "server-only";

import { cookies } from "next/headers";

const ACCESS_COOKIE = "af_access";
const REFRESH_COOKIE = "af_refresh";
const ORG_COOKIE = "af_org";

/**
 * Cookie lifetimes, deliberately *shorter* than the tokens they carry.
 *
 * The access token lives 30 minutes and the refresh token 7 days
 * (`ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS`). Setting the
 * cookie to expire a little first means the browser discards it while the token
 * is still technically valid, rather than presenting a dead credential and
 * getting a 401 the user has to interpret.
 */
const ACCESS_MAX_AGE = 25 * 60;
const REFRESH_MAX_AGE = 6 * 24 * 60 * 60;

const BASE_COOKIE = {
  httpOnly: true,
  /**
   * `lax`, not `strict`. Strict would drop the session cookie on the OAuth
   * callback — Google redirects the browser to us, which is a cross-site
   * navigation, and the user would land back "logged out" having just connected
   * their calendar. `lax` sends cookies on top-level GET navigations, which is
   * exactly that case and not the cross-site POST that CSRF needs.
   */
  sameSite: "lax" as const,
  /**
   * Secure everywhere except local development, where there is no TLS and the
   * cookie would simply never be set — an authentication bug that looks like a
   * login loop.
   */
  secure: process.env.NODE_ENV === "production",
  path: "/",
};

export interface SessionTokens {
  access_token: string;
  refresh_token: string;
}

/** Store a freshly issued pair. Called after login, register, and refresh. */
export async function setSession(tokens: SessionTokens): Promise<void> {
  const jar = await cookies();
  jar.set(ACCESS_COOKIE, tokens.access_token, { ...BASE_COOKIE, maxAge: ACCESS_MAX_AGE });
  jar.set(REFRESH_COOKIE, tokens.refresh_token, { ...BASE_COOKIE, maxAge: REFRESH_MAX_AGE });
}

export async function clearSession(): Promise<void> {
  const jar = await cookies();
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, ORG_COOKIE]) {
    jar.delete(name);
  }
}

export async function getAccessToken(): Promise<string | undefined> {
  return (await cookies()).get(ACCESS_COOKIE)?.value;
}

export async function getRefreshToken(): Promise<string | undefined> {
  return (await cookies()).get(REFRESH_COOKIE)?.value;
}

/**
 * The active organization.
 *
 * A separate, *non*-httpOnly-sensitive cookie because it is not a credential —
 * it is a UI preference, and the backend re-checks membership on every request
 * (`CurrentMembership`, M3). Putting an organization id here cannot grant access
 * to it; the worst a tampered value achieves is a 403 from the backend.
 *
 * Still httpOnly, because nothing in the browser needs to read it either.
 */
export async function getOrganizationId(): Promise<string | undefined> {
  return (await cookies()).get(ORG_COOKIE)?.value;
}

export async function setOrganizationId(organizationId: string): Promise<void> {
  const jar = await cookies();
  jar.set(ORG_COOKIE, organizationId, { ...BASE_COOKIE, maxAge: REFRESH_MAX_AGE });
}

export async function isSignedIn(): Promise<boolean> {
  return (await getRefreshToken()) !== undefined;
}
