/**
 * The handful of primitives every page here needs.
 *
 * Not shadcn/ui, and the placeholder README that named it was written before
 * there was a UI to look at. shadcn generates ~20 component files with Radix
 * dependencies; M13 needs a button, a field, a card and two banners. Vendoring a
 * component library to use four of it is a dependency added for a future that may
 * not arrive in that shape.
 *
 * These are Server Components — no `"use client"` — because nothing here holds
 * state. That keeps the client bundle at roughly zero for pages that only render.
 */
import type { ComponentProps, ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`rounded-xl border border-[--color-border] bg-[--color-surface] ${className}`}
    >
      {children}
    </div>
  );
}

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ComponentProps<"button"> & { variant?: "primary" | "ghost" | "danger" }) {
  const styles = {
    primary: "bg-[--color-accent] text-[--color-accent-ink] hover:opacity-90",
    ghost: "border border-[--color-border] hover:bg-[--color-canvas]",
    danger: "border border-[--color-danger] text-[--color-danger] hover:bg-[--color-danger]/5",
  }[variant];

  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center rounded-lg px-4 py-2 text-sm font-medium transition disabled:opacity-50 ${styles} ${className}`}
    />
  );
}

export function Field({
  label,
  hint,
  ...props
}: ComponentProps<"input"> & { label: string; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium">{label}</span>
      <input
        {...props}
        className="w-full rounded-lg border border-[--color-border] bg-[--color-surface] px-3 py-2 text-sm outline-none focus:border-[--color-accent]"
      />
      {hint ? <span className="mt-1 block text-xs text-[--color-muted]">{hint}</span> : null}
    </label>
  );
}

/**
 * An error the user can act on.
 *
 * `role="alert"` so a screen reader announces it when it appears — a validation
 * message that is only visible is a message a blind user submits the same broken
 * form twice without hearing.
 */
export function ErrorBanner({ children }: { children: ReactNode }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-[--color-danger] bg-[--color-danger]/5 px-3 py-2 text-sm text-[--color-danger]"
    >
      {children}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-10 text-center text-sm text-[--color-muted]">{children}</p>;
}

export function Badge({ children, tone = "muted" }: { children: ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    muted: "bg-[--color-canvas] text-[--color-muted] border-[--color-border]",
    ok: "bg-[--color-accent]/10 text-[--color-accent] border-[--color-accent]/30",
    warn: "bg-[--color-warn-bg] text-[--color-ink] border-[--color-warn-border]",
    bad: "bg-[--color-danger]/10 text-[--color-danger] border-[--color-danger]/30",
  };

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${tones[tone] ?? tones.muted}`}
    >
      {children}
    </span>
  );
}
