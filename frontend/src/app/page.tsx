import { redirect } from "next/navigation";

import { isSignedIn } from "@/lib/session";

/**
 * The root is a fork, not a page.
 *
 * A marketing landing page belongs here eventually; until there is one, sending
 * people straight to the thing they came for beats a placeholder that has to be
 * clicked through.
 */
export default async function Home() {
  redirect((await isSignedIn()) ? "/chat" : "/login");
}
