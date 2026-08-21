import { NextResponse } from "next/server";

import { clearSession } from "@/lib/session";

/**
 * Drop a dead session and send the user to sign in.
 *
 * **This exists because cookies cannot be cleared from a page.** Next allows
 * cookie mutation only in Server Actions and Route Handlers; calling
 * `cookies().delete()` while a Server Component renders throws React error #441.
 *
 * `refreshSession` in `lib/api.ts` did exactly that — it cleared the session when
 * a refresh failed, from a path that runs during render — so a user whose refresh
 * token had expired or been revoked got a crash rather than a sign-in form. A
 * production audit found it: the crash was masked because nothing rendered an
 * error boundary either, so the whole thing surfaced as Next's default
 * "Application error".
 *
 * A GET that changes state, which is normally wrong. It is defensible here for
 * one reason: the only thing it can do to a visitor is sign *them* out, using
 * only their own cookies. There is nothing for a forged request to achieve.
 */
export async function GET(request: Request) {
  await clearSession();

  return NextResponse.redirect(new URL("/login", request.url));
}
