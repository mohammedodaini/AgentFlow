/**
 * Where the backend lives, and the one rule about reading it.
 *
 * Server-only. `BACKEND_URL` has no `NEXT_PUBLIC_` prefix, which is not a
 * stylistic choice: Next inlines `NEXT_PUBLIC_*` into the browser bundle, and a
 * backend URL in the bundle invites client code to call the API directly — which
 * is precisely the thing this frontend is built not to do (see `session.ts`).
 *
 * Importing this module from a Client Component is therefore a build error rather
 * than a subtle security regression, and that is the point.
 */
import "server-only";

export const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

/**
 * The API is versioned and the frontend commits to one version explicitly.
 *
 * `docs/architecture.md` says the client talks to the backend "exclusively
 * through the versioned REST API". Spelling `/api/v1` once here means the day a
 * v2 exists, the migration is a diff in this file rather than a search for string
 * literals across forty components.
 */
export const API_BASE = `${BACKEND_URL}/api/v1`;
