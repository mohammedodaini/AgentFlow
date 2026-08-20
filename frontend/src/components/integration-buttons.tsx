"use client";

import { useTransition } from "react";

import { Button } from "@/components/ui";
import { connectProvider, disconnectIntegration } from "@/lib/actions";

/**
 * Start an OAuth flow.
 *
 * A form posting to a Server Action rather than a link, because the URL to visit
 * does not exist until the backend has minted a `state` and stored it — so it
 * cannot be an `href` rendered at build time. The action redirects, which is a
 * real navigation to the provider's consent screen.
 */
export function ConnectButton({ provider, label }: { provider: string; label: string }) {
  const [pending, start] = useTransition();

  return (
    <form
      action={() => {
        start(async () => {
          await connectProvider(provider);
        });
      }}
    >
      <Button type="submit" disabled={pending}>
        {pending ? "Opening…" : label}
      </Button>
    </form>
  );
}

/**
 * Turn a connection off.
 *
 * The row survives on the backend for the audit trail; the credential does not.
 * Nothing here warns or confirms — disconnecting is reversible in one click, and
 * a confirmation on a safe action teaches people to dismiss confirmations.
 */
export function DisconnectButton({ integrationId }: { integrationId: string }) {
  const [pending, start] = useTransition();

  return (
    <form
      action={() => {
        start(async () => {
          await disconnectIntegration(integrationId);
        });
      }}
    >
      <Button type="submit" variant="ghost" disabled={pending}>
        {pending ? "Disconnecting…" : "Disconnect"}
      </Button>
    </form>
  );
}
