"use client";

import { useActionState, useEffect, useRef } from "react";

import { Button, ErrorBanner } from "@/components/ui";
import type { ActionResult } from "@/lib/actions";

/**
 * Upload a document.
 *
 * The accept list mirrors `ALLOWED_UPLOAD_MIME_TYPES` on the backend. It is a
 * convenience, not a control — the backend enforces the real allowlist, and a
 * file picker filter is trivially bypassed. Keeping them in step matters anyway:
 * a user who can select a `.docx` and then gets a 415 has been invited to fail.
 */
export function UploadForm({
  action,
}: {
  action: (previous: ActionResult, formData: FormData) => Promise<ActionResult>;
}) {
  const [state, formAction, pending] = useActionState(action, {});
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (!pending && !state.error) {
      formRef.current?.reset();
    }
  }, [pending, state]);

  return (
    <form ref={formRef} action={formAction} className="space-y-2">
      {state.error ? <ErrorBanner>{state.error}</ErrorBanner> : null}

      <div className="flex flex-wrap items-center gap-2">
        <input
          type="file"
          name="file"
          required
          disabled={pending}
          accept=".pdf,.txt,.md,application/pdf,text/plain,text/markdown"
          className="flex-1 text-sm file:mr-3 file:rounded-lg file:border file:border-[--color-border] file:bg-[--color-surface] file:px-3 file:py-1.5 file:text-sm"
        />
        <Button type="submit" disabled={pending}>
          {pending ? "Uploading…" : "Upload"}
        </Button>
      </div>
    </form>
  );
}
