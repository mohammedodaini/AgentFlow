"use client";

/**
 * The one client component in the auth flow, and only because of `useActionState`.
 *
 * It needs client JS for exactly two things: showing the server's error message
 * without a full re-render, and disabling the submit button while the request is
 * in flight. That second one is not decoration — without it, a slow login invites
 * a second click, and a second registration attempt is a duplicate-email error on
 * an account that was just created successfully.
 *
 * The password still never touches client JavaScript: the form posts to a Server
 * Action, so the value goes straight from the DOM to the server.
 */
import { useActionState } from "react";

import { Button, ErrorBanner, Field } from "@/components/ui";
import type { AuthResult } from "@/lib/auth";

interface Props {
  action: (previous: AuthResult, formData: FormData) => Promise<AuthResult>;
  submitLabel: string;
  withName?: boolean;
}

export function AuthForm({ action, submitLabel, withName = false }: Props) {
  const [state, formAction, pending] = useActionState(action, {});

  return (
    <form action={formAction} className="space-y-4">
      {state.error ? <ErrorBanner>{state.error}</ErrorBanner> : null}

      {withName ? <Field label="Your name" name="full_name" autoComplete="name" /> : null}

      <Field
        label="Email"
        name="email"
        type="email"
        required
        autoComplete="email"
        autoFocus={!withName}
      />
      <Field
        label="Password"
        name="password"
        type="password"
        required
        minLength={12}
        // `new-password` on the register form tells a password manager to offer
        // to generate one; `current-password` on login tells it to fill. Getting
        // these the wrong way round is why so many sign-up forms autofill the
        // password you already use somewhere else.
        autoComplete={withName ? "new-password" : "current-password"}
        hint={withName ? "At least 12 characters." : undefined}
      />

      <Button type="submit" disabled={pending} className="w-full">
        {pending ? "Working…" : submitLabel}
      </Button>
    </form>
  );
}
