import Link from "next/link";
import { redirect } from "next/navigation";

import { signOut } from "@/lib/auth";
import { isSignedIn } from "@/lib/session";

/**
 * The authenticated shell, and the one place that enforces "you must be signed
 * in".
 *
 * A layout rather than a check repeated in every page: a route group layout wraps
 * every page beneath it, so adding a page under `(app)/` cannot accidentally ship
 * without the guard. The alternative — remembering to call `requireSession()` at
 * the top of each page — fails silently exactly once, on the page somebody added
 * in a hurry.
 *
 * This is defence in depth rather than the security boundary. The real boundary
 * is the backend, which re-checks the JWT and the membership on every request
 * (M3). This only decides what the UI shows.
 */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  if (!(await isSignedIn())) {
    redirect("/login");
  }

  const links = [
    { href: "/chat", label: "Chat" },
    { href: "/approvals", label: "Approvals" },
    { href: "/documents", label: "Documents" },
  ];

  return (
    <div className="min-h-screen">
      <header className="border-b border-[--color-border] bg-[--color-surface]">
        <div className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-3">
          <Link href="/chat" className="font-semibold tracking-tight">
            AgentFlow
          </Link>

          <nav className="flex items-center gap-1 text-sm">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className="rounded-lg px-3 py-1.5 text-[--color-muted] transition hover:bg-[--color-canvas] hover:text-[--color-ink]"
              >
                {link.label}
              </Link>
            ))}
          </nav>

          <form action={signOut} className="ml-auto">
            <button
              type="submit"
              className="text-sm text-[--color-muted] transition hover:text-[--color-ink]"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8">{children}</main>
    </div>
  );
}
