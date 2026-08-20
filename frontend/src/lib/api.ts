/**
 * The only place this frontend talks to the backend.
 *
 * Server-only, by construction: it reads httpOnly cookies, which a Client
 * Component cannot. That constraint is the design (see `session.ts`) rather than
 * an inconvenience — it means there is exactly one function that can attach a
 * credential, so "what does the browser send to the API?" has a one-word answer:
 * nothing.
 *
 * Refresh happens here, once
 * --------------------------
 * An access token lives 30 minutes, so a user reading a long transcript will
 * cross the boundary mid-session. `apiFetch` retries a 401 exactly once, after
 * refreshing. Once, not in a loop: if the refreshed token is also rejected the
 * credential is genuinely dead, and retrying would turn one bad session into a
 * request storm against `/auth/refresh`.
 *
 * The backend rotates refresh tokens and denylists the old `jti` (M3), so a
 * successful refresh must persist the *new* pair. Storing only the access token
 * would work for 30 minutes and then log the user out with no explanation —
 * exactly the class of bug M11 found in its own token handling.
 */
import "server-only";

import { headers as nextHeaders } from "next/headers";

import { API_BASE } from "@/lib/config";
import {
  clearSession,
  getAccessToken,
  getOrganizationId,
  getRefreshToken,
  setSession,
} from "@/lib/session";
import type { ApiErrorBody } from "@/lib/types";

/** A backend failure, carrying the status so callers can branch on it. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 401/403 — the session is the problem, not the request. */
  get isAuthFailure(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Skip the `X-Organization-Id` header — only `/auth` and `/users/me` do. */
  withoutOrganization?: boolean;
  /**
   * Next's fetch cache behaviour. Defaults to `no-store`, because everything
   * this API returns is per-user and per-tenant: caching a conversation list
   * across requests is a correctness bug the moment two people use the app.
   */
  cache?: RequestCache;
}

async function buildHeaders(withoutOrganization: boolean): Promise<Headers> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const token = await getAccessToken();

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  if (!withoutOrganization) {
    const organizationId = await getOrganizationId();

    if (organizationId) {
      headers.set("X-Organization-Id", organizationId);
    }
  }

  await forwardClientAddress(headers);

  return headers;
}

/**
 * Pass the caller's address through to the API, for the audit trail (M16).
 *
 * **Without this the audit log is worse than empty.** The browser never talks to
 * the API directly (ADR-0016), so every request the backend sees originates from
 * *this* server — and `events.ip_address` recorded the web container's address
 * for every sign-in in the system. A column holding one constant value looks like
 * data and is useless for the thing it exists for, which is noticing that eighty
 * failed sign-ins came from one address in a minute.
 *
 * Honest about the limit: this can only forward what it was given. Behind a load
 * balancer that sets `X-Forwarded-For` — the production case — the real address
 * arrives and is passed on. Running Next with nothing in front of it, there is no
 * header to read and nothing is sent, which leaves the backend recording the
 * connection address as before. It is not possible to do better from here: Next
 * does not expose the socket's peer address to application code.
 *
 * The backend reads only the *first* entry of this header and only trusts it
 * behind a proxy — see `client_ip` in `app/middleware/rate_limit.py`.
 */
async function forwardClientAddress(headers: Headers): Promise<void> {
  const incoming = await nextHeaders();
  const forwarded = incoming.get("x-forwarded-for");

  if (forwarded) {
    headers.set("X-Forwarded-For", forwarded);
  }
}

/**
 * Exchange the refresh token for a new pair. Returns false when it cannot.
 *
 * Clearing the session on failure is the important half. Leaving a dead refresh
 * token in the jar means every later request repeats this dance and fails the
 * same way, and the user sees a UI that is signed in and cannot load anything.
 */
async function refreshSession(): Promise<boolean> {
  const refreshToken = await getRefreshToken();

  if (!refreshToken) {
    return false;
  }

  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
    cache: "no-store",
  });

  if (!response.ok) {
    await clearSession();
    return false;
  }

  await setSession(await response.json());
  return true;
}

async function toApiError(response: Response): Promise<ApiError> {
  // The backend answers a consistent envelope (`app/api/errors.py`), but a proxy
  // or a crash can produce HTML. Falling back rather than letting a JSON parse
  // error escape means the user sees "something went wrong" instead of a stack
  // trace about an unexpected token.
  try {
    const body = (await response.json()) as ApiErrorBody;
    return new ApiError(response.status, body.error.code, body.error.message);
  } catch {
    return new ApiError(response.status, "unknown", `Request failed (${response.status}).`);
  }
}

/** Call the backend as the signed-in user, refreshing once if the token expired. */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, withoutOrganization = false, cache = "no-store" } = options;

  const send = async (): Promise<Response> =>
    fetch(`${API_BASE}${path}`, {
      method,
      headers: await buildHeaders(withoutOrganization),
      body: body === undefined ? undefined : JSON.stringify(body),
      cache,
    });

  let response = await send();

  if (response.status === 401 && (await refreshSession())) {
    response = await send();
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  // 204, and any other body-less success. `response.json()` on an empty body
  // throws, and a caller that expected `void` should not have to know that.
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/**
 * Upload a file. Separate from `apiFetch` because multipart is not JSON.
 *
 * Deliberately not merged into `apiFetch` with a "is this a FormData?" branch:
 * the two differ in headers (the boundary must be set by fetch, not by us — a
 * hand-written `Content-Type: multipart/form-data` without the boundary fails in
 * a way that reads as a server bug) and in what a retry means.
 */
export async function apiUpload<T>(path: string, formData: FormData): Promise<T> {
  const headers = await buildHeaders(false);
  headers.delete("Content-Type");

  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: formData,
    cache: "no-store",
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  return (await response.json()) as T;
}
