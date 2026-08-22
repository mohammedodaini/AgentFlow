import "server-only";

import { headers as nextHeaders } from "next/headers";

/**
 * Pass the caller's address through to the API, for the audit trail (M16).
 *
 * **Without this the audit log is worse than empty.** The browser never talks
 * to the API directly (ADR-0016), so every request the backend sees originates
 * from *this* server — and `events.ip_address` recorded the web container's
 * address for every sign-in in the system. A column holding one constant value
 * looks like data and is useless for the thing it exists for, which is noticing
 * that eighty failed sign-ins came from one address in a minute.
 *
 * **It lives in its own module because the first version did not, and missed
 * the routes that matter most.** It was a private function inside `api.ts`,
 * called from `buildHeaders`, which is `apiFetch`'s helper — and register,
 * login and token refresh do not use `apiFetch`. They build their own headers,
 * because they have no session yet. So the three endpoints whose audit records
 * are the entire point of the column — `user.registered`,
 * `user.sign_in_failed`, and the lockout counter behind it — were the three
 * that recorded the container's address.
 *
 * Nothing failed. A TLS drill registered a user through a real browser and
 * Postgres said `user.registered | 172.19.0.7`, which is the frontend.
 *
 * Honest about the limit: this can only forward what it was given. Behind the
 * Caddy reverse proxy in `docker-compose.prod.yml` the real address arrives and
 * is passed on. Running Next with nothing in front of it there is no header to
 * read and nothing is sent, which leaves the backend recording the connection
 * address as before. It is not possible to do better from here: Next does not
 * expose the socket's peer address to application code.
 *
 * **Forwarded verbatim, and it must be.** Caddy is configured to *replace* this
 * header rather than append to it, so what arrives is a single entry Caddy
 * wrote. The backend reads the header from the right, `TRUSTED_PROXY_HOPS`
 * entries in — one, in that deployment — so the entry it trusts is exactly the
 * one Caddy observed. Prepending or appending anything here would shift that
 * count and make the backend trust the wrong entry. See `client_ip` in
 * `app/middleware/rate_limit.py`.
 */
export async function forwardClientAddress(headers: Headers): Promise<void> {
  const incoming = await nextHeaders();
  const forwarded = incoming.get("x-forwarded-for");

  if (forwarded) {
    headers.set("X-Forwarded-For", forwarded);
  }
}

/** The same, for the call sites that build a plain header object. */
export async function withClientAddress(
  base: Record<string, string>,
): Promise<Record<string, string>> {
  const headers = new Headers(base);
  await forwardClientAddress(headers);
  return Object.fromEntries(headers.entries());
}
