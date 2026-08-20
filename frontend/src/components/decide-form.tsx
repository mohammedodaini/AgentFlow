"use client";

/**
 * Approve or reject, with the pending state that keeps it honest.
 *
 * The disabled-while-pending here is not cosmetic. Approving executes a real
 * side effect on somebody's calendar, and a double click sends two requests. The
 * backend refuses the second with a 409 — the status transition is the
 * idempotency key (M12) — so the effect happens once regardless; what this
 * prevents is the user seeing "This request was already approved" as though
 * something had gone wrong.
 */
import { useActionState } from "react";

import { Button, ErrorBanner } from "@/components/ui";
import type { ActionResult } from "@/lib/actions";

interface Props {
  approve: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
  reject: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
}

export function DecideForm({ approve, reject }: Props) {
  const [approveState, approveAction, approving] = useActionState(approve, {});
  const [rejectState, rejectAction, rejecting] = useActionState(reject, {});
  const busy = approving || rejecting;
  const error = approveState.error ?? rejectState.error;

  return (
    <div className="space-y-3">
      {error ? <ErrorBanner>{error}</ErrorBanner> : null}

      <div className="flex flex-wrap items-center gap-2">
        <form action={approveAction}>
          <Button type="submit" disabled={busy}>
            {approving ? "Doing it…" : "Approve"}
          </Button>
        </form>

        <form action={rejectAction} className="flex flex-1 items-center gap-2">
          <input
            name="reason"
            placeholder="Reason (optional)"
            disabled={busy}
            className="min-w-0 flex-1 rounded-lg border border-[--color-border] bg-[--color-surface] px-3 py-2 text-sm outline-none focus:border-[--color-accent] disabled:opacity-60"
          />
          {/* Rejecting needs no reason: requiring one makes the safe choice the
              tedious one, and a UI that does that gets fewer safe choices. */}
          <Button type="submit" variant="danger" disabled={busy}>
            {rejecting ? "…" : "Reject"}
          </Button>
        </form>
      </div>
    </div>
  );
}
