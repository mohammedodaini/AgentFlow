import Link from "next/link";

import { Card } from "@/components/ui";

/**
 * A 404 that says which product the user is in.
 *
 * Next's default is a bare "404 | This page could not be found" on a white page,
 * which is indistinguishable from a misconfigured proxy or a dead host. Somebody
 * who mistyped a URL should be able to tell that the application is fine and get
 * back to it in one click.
 *
 * A Server Component: nothing here is interactive, so nothing needs to ship.
 */
export const metadata = { title: "Not found · AgentFlow" };

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-lg items-center px-6">
      <Card className="w-full space-y-4 p-6">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Page not found</h1>
          <p className="text-sm text-[--color-muted]">
            That address does not exist in AgentFlow. The application itself is running normally.
          </p>
        </div>
        <Link
          href="/chat"
          className="inline-block rounded-lg bg-[--color-ink] px-3 py-1.5 text-sm text-[--color-surface] transition hover:opacity-90"
        >
          Back to chat
        </Link>
      </Card>
    </main>
  );
}
