"use client";

import { useActionState } from "react";

import { Button, ErrorBanner } from "@/components/ui";
import type { ActionResult } from "@/lib/actions";

/**
 * Ask the agent to draft a calendar change.
 *
 * The placeholder shows the exact date format on purpose. The parser is
 * deliberately strict — it reads `YYYY-MM-DD HH:MM` and refuses anything looser,
 * because half-understanding "tomorrow at 3" puts a meeting in a diary at the
 * wrong time and raises nothing (M12). A strict parser with no visible example is
 * a form people fail twice before reading the error.
 */
export function ProposeForm({
  action,
}: {
  action: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
}) {
  const [state, formAction, pending] = useActionState(action, {});

  return (
    <form action={formAction} className="space-y-2">
      {state.error ? <ErrorBanner>{state.error}</ErrorBanner> : null}

      <div className="flex gap-2">
        <input
          name="instruction"
          required
          autoComplete="off"
          disabled={pending}
          placeholder="Schedule a design review on 2026-08-20 09:00"
          className="flex-1 rounded-lg border border-[--color-border] bg-[--color-surface] px-3 py-2 text-sm outline-none focus:border-[--color-accent] disabled:opacity-60"
        />
        <Button type="submit" disabled={pending}>
          {pending ? "Drafting…" : "Draft it"}
        </Button>
      </div>
    </form>
  );
}
