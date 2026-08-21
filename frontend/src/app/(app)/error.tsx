"use client";

import Link from "next/link";

import { Button, Card } from "@/components/ui";

/**
 * The same boundary, inside the authenticated shell.
 *
 * A boundary catches errors from the segments *below* it, so the nearest one
 * wins. Without this file a failure on `/documents` would bubble to the root
 * boundary and replace the whole page — navigation included — leaving somebody
 * looking at an error with no way to click anywhere else.
 *
 * With it, the header and nav from `(app)/layout.tsx` stay on screen and only the
 * page content is replaced. That is the difference between "this page is broken"
 * and "the application is broken", and only one of them is usually true.
 */
export default function AppSectionError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <Card className="space-y-4 p-6">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold">This page could not load</h1>
        <p className="text-sm text-[--color-muted]">
          The rest of the app is still working — try again, or pick another section above.
        </p>
      </div>

      {error.digest ? (
        <p className="rounded-lg bg-[--color-canvas] px-3 py-2 font-mono text-xs text-[--color-muted]">
          Reference: {error.digest}
        </p>
      ) : null}

      <div className="flex gap-2">
        <Button type="button" onClick={() => retry()}>
          Try again
        </Button>
        <Link
          href="/chat"
          className="rounded-lg px-3 py-1.5 text-sm text-[--color-muted] transition hover:text-[--color-ink]"
        >
          Back to chat
        </Link>
      </div>
    </Card>
  );
}
