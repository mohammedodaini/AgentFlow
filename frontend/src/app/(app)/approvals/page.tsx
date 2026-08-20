import { DecideForm } from "@/components/decide-form";
import { ProposeForm } from "@/components/propose-form";
import { Badge, Card, Empty } from "@/components/ui";
import { decideApproval, proposeCalendarAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import type { Approval } from "@/lib/types";

export const metadata = { title: "Approvals · AgentFlow" };

/**
 * The inbox: what the agent wants permission to do.
 *
 * The list is oldest-first, matching the backend, because a queue of things
 * people are waiting on is worked from the front — newest-first would bury the
 * request that has been outstanding longest, which is the one about to expire.
 */
export default async function ApprovalsPage() {
  const approvals = await apiFetch<Approval[]>("/approvals?limit=50");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Approvals</h1>
        <p className="text-sm text-[--color-muted]">
          The agent never changes anything without being told to. Requests expire if nobody
          decides.
        </p>
      </div>

      <Card className="space-y-3 p-4">
        <h2 className="text-sm font-medium">Draft a calendar change</h2>
        <ProposeForm action={proposeCalendarAction} />
      </Card>

      <Card>
        {approvals.length === 0 ? (
          <Empty>Nothing is waiting for you.</Empty>
        ) : (
          <ul className="divide-y divide-[--color-border]">
            {approvals.map((approval) => (
              <li key={approval.id} className="space-y-3 px-4 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <p className="text-sm font-medium">{approval.summary}</p>
                  <Badge tone="warn">
                    expires {new Date(approval.expires_at).toLocaleString()}
                  </Badge>
                </div>

                {/* The full action, not only the summary. The sentence above is
                    rendered from this by code, and showing both lets a careful
                    user check that they match — which is the whole reason the
                    action is stored whole (ADR-0015). */}
                <details className="text-xs text-[--color-muted]">
                  <summary className="cursor-pointer select-none">Exactly what will happen</summary>
                  <pre className="mt-2 overflow-x-auto rounded-lg bg-[--color-canvas] p-3">
                    {JSON.stringify(approval.requested_action, null, 2)}
                  </pre>
                </details>

                <DecideForm
                  approve={decideApproval.bind(null, approval.id, "approve")}
                  reject={decideApproval.bind(null, approval.id, "reject")}
                />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
