"use client";

import { useActionState } from "react";

import { Button, ErrorBanner } from "@/components/ui";
import type { ActionResult } from "@/lib/actions";

/**
 * Ask an agent to draft something that will need approval.
 *
 * **The placeholder is a required prop, not a default.** Both parsers behind this
 * form are deliberately strict — the calendar one reads `YYYY-MM-DD HH:MM` and
 * the email one wants `<address> about <subject> saying <body>` — because
 * half-understanding an instruction puts a meeting in a diary at the wrong time
 * (M12) or sends the wrong words to a real person (M14). A strict parser with no
 * visible example is a form people fail twice before they read the error, so
 * every caller has to supply one.
 */
export function ProposeForm({
  action,
  placeholder,
  submitLabel,
}: {
  action: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
  placeholder: string;
  submitLabel: string;
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
          placeholder={placeholder}
          className="flex-1 rounded-lg border border-[--color-border] bg-[--color-surface] px-3 py-2 text-sm outline-none focus:border-[--color-accent] disabled:opacity-60"
        />
        <Button type="submit" disabled={pending}>
          {pending ? "Drafting…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}
