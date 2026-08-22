"use server";

/**
 * Signing in, signing up, signing out — as Server Actions.
 *
 * Server Actions rather than a client `fetch` to a route handler, for a reason
 * that matters here more than usual: **the password never enters a JavaScript
 * bundle or a client-side variable.** The form posts directly to the server, so
 * there is no code path in the browser that has ever held it.
 *
 * It also means the login page works with JavaScript disabled, which is a
 * pleasant side effect rather than the goal.
 *
 * Errors come back as values, not exceptions
 * ------------------------------------------
 * These return `{ error }` instead of throwing, because a thrown error in a
 * Server Action becomes an opaque "an error occurred" in production — Next
 * deliberately does not leak server messages to the client. "Incorrect email or
 * password" is not a leak; it is the entire content of the interaction, so it
 * travels as a returned value.
 */

import { redirect } from "next/navigation";

import { withClientAddress } from "@/lib/client-address";
import { API_BASE } from "@/lib/config";
import { clearSession, getRefreshToken, setSession, setOrganizationId } from "@/lib/session";
import type { Membership } from "@/lib/types";

export interface AuthResult {
  error?: string;
}

/**
 * Pick the organization to act in after signing in.
 *
 * Registration creates a personal organization and an owner membership in one
 * transaction (M3), so there is always at least one. Taking the first is a
 * placeholder for a switcher — and it is *stored* rather than recomputed per
 * request, because "which tenant am I in?" must not silently change when the
 * list reorders.
 */
async function adoptFirstOrganization(accessToken: string): Promise<void> {
  const response = await fetch(`${API_BASE}/organizations`, {
    headers: await withClientAddress({ Authorization: `Bearer ${accessToken}` }),
    cache: "no-store",
  });

  if (!response.ok) {
    return;
  }

  const memberships = (await response.json()) as Membership[];
  const first = memberships[0];

  if (first) {
    await setOrganizationId(first.organization.id);
  }
}

async function authenticate(path: string, payload: Record<string, unknown>): Promise<AuthResult> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    // **The audit trail's whole reason for having an address column.** These
    // are the sign-in and registration events, and the failed-login counter
    // that account lockout reads. Before this they recorded the frontend
    // container's address for every user in the system.
    headers: await withClientAddress({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
    cache: "no-store",
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    return {
      error: body?.error?.message ?? "Something went wrong. Please try again.",
    };
  }

  const tokens = await response.json();
  await setSession(tokens);
  await adoptFirstOrganization(tokens.access_token);
  return {};
}

export async function signIn(_previous: AuthResult, formData: FormData): Promise<AuthResult> {
  const result = await authenticate("/auth/login", {
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  });

  if (result.error) {
    return result;
  }

  // Outside the try/catch above on purpose: `redirect` works by throwing, and a
  // `catch` around it would swallow the navigation and leave the user staring at
  // a form that just succeeded.
  redirect("/chat");
}

export async function signUp(_previous: AuthResult, formData: FormData): Promise<AuthResult> {
  const fullName = String(formData.get("full_name") ?? "").trim();

  const result = await authenticate("/auth/register", {
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
    full_name: fullName === "" ? null : fullName,
  });

  if (result.error) {
    return result;
  }

  redirect("/chat");
}

export async function signOut(): Promise<void> {
  const refreshToken = await getRefreshToken();

  if (refreshToken) {
    // Best effort. The backend denylists the `jti` so the token cannot be reused
    // (M3), but a network failure here must not trap somebody in a session they
    // asked to leave — the cookies go either way.
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
      cache: "no-store",
    }).catch(() => undefined);
  }

  await clearSession();
  redirect("/login");
}
