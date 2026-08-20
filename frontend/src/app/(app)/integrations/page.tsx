import { Badge, Card, Empty } from "@/components/ui";
import { ConnectButton, DisconnectButton } from "@/components/integration-buttons";
import { apiFetch } from "@/lib/api";
import type { Integration, ProviderRead } from "@/lib/types";

export const metadata = { title: "Integrations · AgentFlow" };

/** Provider ids are snake_case on the wire; people read product names. */
const NAMES: Record<string, string> = {
  gmail: "Gmail",
  google_calendar: "Google Calendar",
  slack: "Slack",
  notion: "Notion",
  github: "GitHub",
  stripe: "Stripe",
};

const TONES: Record<Integration["status"], string> = {
  active: "ok",
  revoked: "bad",
  disconnected: "muted",
};

/**
 * Connected accounts, and what connecting one would grant.
 *
 * The provider list comes from `/integrations/providers` rather than a constant
 * in this file. A hard-coded list drifts the moment an operator sets or unsets a
 * client id, and the failure is a button that leads to a 404 — so the deployment
 * describes itself and the UI renders what it is told.
 *
 * Revoked connections are shown rather than filtered out. "Slack — reconnect it"
 * is the most useful thing this page can say, and hiding it renders a broken
 * integration as an absence indistinguishable from never having connected.
 */
export default async function IntegrationsPage() {
  const [providers, integrations] = await Promise.all([
    apiFetch<ProviderRead[]>("/integrations/providers"),
    apiFetch<Integration[]>("/integrations"),
  ]);

  // The live one wins when a provider has been connected, disconnected and
  // reconnected — the older rows are audit trail, and showing them all would put
  // three Slack entries on the page.
  const live = new Map<string, Integration>();
  for (const integration of integrations) {
    const existing = live.get(integration.provider);
    if (!existing || integration.status === "active") {
      live.set(integration.provider, integration);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Integrations</h1>
        <p className="text-sm text-[--color-muted]">
          Accounts the agent may read. Everything here is read-only except Gmail, which can send
          a draft — and only after you approve the exact message.
        </p>
      </div>

      <Card>
        {providers.length === 0 ? (
          <Empty>
            No providers are configured on this deployment. An operator sets the client id and
            secret for each one.
          </Empty>
        ) : (
          <ul className="divide-y divide-[--color-border]">
            {providers.map((provider) => {
              const connected = live.get(provider.provider);

              return (
                <li
                  key={provider.provider}
                  className="flex flex-wrap items-center gap-3 px-4 py-4"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">
                      {NAMES[provider.provider] ?? provider.provider}
                    </p>

                    <p className="truncate text-xs text-[--color-muted]">
                      {connected?.external_account_id ??
                        (provider.scopes.length === 0
                          ? "Access is granted per page, in Notion"
                          : provider.scopes.join(" · "))}
                    </p>
                  </div>

                  {connected && connected.status !== "disconnected" ? (
                    <Badge tone={TONES[connected.status]}>
                      {connected.status === "revoked" ? "needs reconnecting" : "connected"}
                    </Badge>
                  ) : null}

                  {connected?.status === "active" ? (
                    <DisconnectButton integrationId={connected.id} />
                  ) : (
                    <ConnectButton
                      provider={provider.provider}
                      label={connected?.status === "revoked" ? "Reconnect" : "Connect"}
                    />
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
