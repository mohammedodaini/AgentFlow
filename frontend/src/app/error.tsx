"use client";

import Link from "next/link";

import { Button, Card } from "@/components/ui";

/**
 * What the user sees when a page outside the authenticated shell throws.
 *
 * **Before this file existed there was nothing**, and a production audit found
 * it: six server pages call `apiFetch` with no `try`/`catch`, so an API that was
 * down rendered Next's own fallback — the bare string "Application error: a
 * server-side exception has occurred", with no explanation, no way back, and no
 * reference anybody could quote to support.
 *
 * Error boundaries must be Client Components; that is a React requirement rather
 * than a preference. It is also why this is one of the few files here that ships
 * JavaScript to the browser, and why it stays small.
 *
 * **`error.message` is deliberately not rendered.** Next redacts server error
 * messages in production precisely because they carry stack traces, file paths
 * and connection strings. `digest` is the hash tying this screen to the exact
 * server-side log line, which is the thing worth showing: a person can quote it,
 * and it identifies the failure without describing it.
 */
export default function AppError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-screen max-w-lg items-center px-6">
      <Card className="w-full space-y-4 p-6">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Something went wrong</h1>
          <p className="text-sm text-[--color-muted]">
            This page could not load. It is usually temporary — the server may be restarting or
            briefly unreachable.
          </p>
        </div>

        {error.digest ? (
          <p className="rounded-lg bg-[--color-canvas] px-3 py-2 font-mono text-xs text-[--color-muted]">
            Reference: {error.digest}
          </p>
        ) : null}

        <div className="flex gap-2">
          {/* `retry()` re-fetches and re-renders the boundary's children, which is
              the right first action: the common cause is one request that failed
              once. `reset()` also exists and only clears the error state without
              re-fetching, which would show the same failure again. */}
          <Button type="button" onClick={() => retry()}>
            Try again
          </Button>
          <Link
            href="/login"
            className="rounded-lg px-3 py-1.5 text-sm text-[--color-muted] transition hover:text-[--color-ink]"
          >
            Sign in
          </Link>
        </div>
      </Card>
    </main>
  );
}
