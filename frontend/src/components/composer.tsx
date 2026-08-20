"use client";

/**
 * The message box.
 *
 * Client-side for the same two reasons the auth form is: the pending state, and
 * showing an error without losing what was typed.
 *
 * The pending state matters more here than anywhere else in the app. An agent
 * turn takes seconds — retrieval, then generation — and a form that looks idle
 * during that gets submitted again. A second identical question costs a second
 * agent run and appends a duplicate pair of messages to an append-only
 * transcript, which cannot be tidied up afterwards.
 */
import { useActionState, useRef, useEffect } from "react";

import { Button, ErrorBanner } from "@/components/ui";
import type { ActionResult } from "@/lib/actions";

interface Props {
  action: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
}

export function Composer({ action }: Props) {
  const [state, formAction, pending] = useActionState(action, {});
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    // Cleared only on success. Clearing unconditionally would throw away what the
    // user typed the moment the backend rejected it — and the rejection is
    // usually something they can fix and resend.
    if (!pending && !state.error) {
      formRef.current?.reset();
    }
  }, [pending, state]);

  return (
    <form ref={formRef} action={formAction} className="space-y-2">
      {state.error ? <ErrorBanner>{state.error}</ErrorBanner> : null}

      <div className="flex gap-2">
        <input
          name="content"
          required
          maxLength={20000}
          autoComplete="off"
          disabled={pending}
          placeholder="Ask about your documents…"
          className="flex-1 rounded-lg border border-[--color-border] bg-[--color-surface] px-3 py-2 text-sm outline-none focus:border-[--color-accent] disabled:opacity-60"
        />
        <Button type="submit" disabled={pending}>
          {pending ? "Thinking…" : "Send"}
        </Button>
      </div>
    </form>
  );
}
