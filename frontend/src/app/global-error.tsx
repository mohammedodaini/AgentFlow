"use client";

/**
 * The last resort: an error in the root layout itself.
 *
 * This replaces the root layout when it fires, so it has to supply its own
 * `<html>` and `<body>` — and it cannot import anything the layout provides,
 * including the stylesheet. Every style here is inline for that reason, not for
 * brevity: a global error that depended on CSS the failed layout was supposed to
 * load would render as unstyled text, which is precisely the moment it must not.
 *
 * It should essentially never be seen. `error.tsx` catches everything below the
 * root, and the root layout does almost nothing.
 */
export default function GlobalError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          fontFamily: "system-ui, sans-serif",
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          margin: 0,
          padding: "1.5rem",
          background: "#fafaf9",
          color: "#1c1917",
        }}
      >
        <main style={{ maxWidth: "28rem" }}>
          <h1 style={{ fontSize: "1.125rem", margin: "0 0 0.5rem" }}>AgentFlow is unavailable</h1>
          <p style={{ fontSize: "0.875rem", color: "#57534e", margin: "0 0 1rem" }}>
            The application failed to start rendering. This is not something you can fix from
            here — try again in a moment.
          </p>
          {error.digest ? (
            <p style={{ fontFamily: "ui-monospace, monospace", fontSize: "0.75rem", color: "#57534e" }}>
              Reference: {error.digest}
            </p>
          ) : null}
          <button
            type="button"
            onClick={() => retry()}
            style={{
              marginTop: "0.5rem",
              padding: "0.5rem 0.875rem",
              fontSize: "0.875rem",
              borderRadius: "0.5rem",
              border: "1px solid #d6d3d1",
              background: "#1c1917",
              color: "#fafaf9",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
