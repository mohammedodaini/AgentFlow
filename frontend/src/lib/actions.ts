"use server";

/**
 * Every write the UI can perform, in one file.
 *
 * Server Actions rather than route handlers plus client `fetch`, for the reason
 * `auth.ts` gives — but there is a second one here. `revalidatePath` only exists
 * server-side, and it is what makes the UI correct after a write: sending a
 * message must re-render the transcript, approving must empty the inbox. A client
 * fetch would leave the page showing what it showed before, and the fix for that
 * is client state that can disagree with the server.
 */

import { revalidatePath } from "next/cache";
import { redirect, unstable_rethrow } from "next/navigation";

import { ApiError, apiFetch, apiUpload } from "@/lib/api";
import type {
  Approval,
  ConnectStart,
  Conversation,
  DocumentRead,
  Proposal,
  Supervised,
  Turn,
} from "@/lib/types";

export interface ActionResult {
  error?: string;
}

/**
 * Turn a backend failure into something a person can act on.
 *
 * The backend writes messages for the person who asked rather than the person who
 * deployed (the rule `documents.error` set at M5), so passing them straight
 * through is usually right. An unexpected exception is not: it would put a stack
 * trace or a connection string in front of a user.
 */
function explain(error: unknown): ActionResult {
  // **Let Next's own control flow through, first.** `redirect` and `notFound`
  // work by throwing, and `apiFetch` now redirects to /login when a session has
  // lapsed. Caught here, that redirect would be swallowed and rendered as
  // "Could not reach the server" — the user stranded on a form that will never
  // succeed, with a message describing a problem that does not exist.
  unstable_rethrow(error);

  if (error instanceof ApiError) {
    return { error: error.message };
  }

  return { error: "Could not reach the server. Try again in a moment." };
}

// --- conversations -------------------------------------------------------

export async function startConversation(): Promise<void> {
  const conversation = await apiFetch<Conversation>("/conversations", {
    method: "POST",
    body: {},
  });

  revalidatePath("/chat");
  redirect(`/chat/${conversation.id}`);
}

export async function sendMessage(
  conversationId: string,
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const content = String(formData.get("content") ?? "").trim();

  if (content === "") {
    // Guarded here as well as by `required` on the input, because a Server Action
    // is a public endpoint: anything that can POST can reach it, and the backend
    // would answer 422 with a message about a schema rather than about a message.
    return { error: "Type something first." };
  }

  try {
    await apiFetch<Turn>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: { content },
    });
  } catch (error) {
    return explain(error);
  }

  revalidatePath(`/chat/${conversationId}`);
  revalidatePath("/chat");
  return {};
}

// --- approvals -----------------------------------------------------------

export async function proposeCalendarAction(
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const instruction = String(formData.get("instruction") ?? "").trim();

  if (instruction === "") {
    return { error: "Describe what to schedule." };
  }

  let proposal: Proposal;

  try {
    proposal = await apiFetch<Proposal>("/agent-runs/calendar", {
      method: "POST",
      body: { instruction },
    });
  } catch (error) {
    return explain(error);
  }

  revalidatePath("/approvals");

  // A run that understood nothing produces no approval — and the *message* is the
  // whole response, telling the user how to phrase it so it works. Swallowing it
  // and showing an empty inbox would leave them guessing.
  return proposal.approval ? {} : { error: proposal.message ?? "Nothing to propose." };
}

export async function decideApproval(
  approvalId: string,
  decision: "approve" | "reject",
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const reason = String(formData.get("reason") ?? "").trim();

  try {
    await apiFetch<Approval>(`/approvals/${approvalId}/${decision}`, {
      method: "POST",
      body: decision === "reject" ? { reason: reason === "" ? null : reason } : {},
    });
  } catch (error) {
    return explain(error);
  }

  revalidatePath("/approvals");
  return {};
}

export async function proposeEmailAction(
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const instruction = String(formData.get("instruction") ?? "").trim();

  if (instruction === "") {
    return { error: "Describe the email to draft." };
  }

  let proposal: Proposal;

  try {
    proposal = await apiFetch<Proposal>("/agent-runs/email", {
      method: "POST",
      body: { instruction },
    });
  } catch (error) {
    return explain(error);
  }

  revalidatePath("/approvals");
  return proposal.approval ? {} : { error: proposal.message ?? "Nothing to propose." };
}

/**
 * One box for everything (M15).
 *
 * This replaces the two forms that stood here — "draft a calendar change" and
 * "draft an email" — and the replacement is the whole point of the milestone.
 * Two labelled forms made the *user* the router: they had to know which of the
 * product's internal agents they wanted before they could type anything.
 *
 * A refusal comes back as a successful response with `delegated: null`, so it is
 * surfaced as an error string here rather than thrown. The backend's `reason`
 * names what the product can do, which is the only thing that lets somebody
 * succeed on their next attempt.
 */
export async function superviseInstruction(
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const instruction = String(formData.get("instruction") ?? "").trim();

  if (instruction === "") {
    return { error: "Say what you would like done." };
  }

  let outcome: Supervised;

  try {
    outcome = await apiFetch<Supervised>("/agent-runs/supervised", {
      method: "POST",
      body: { instruction },
    });
  } catch (error) {
    return explain(error);
  }

  revalidatePath("/approvals");

  if (!outcome.delegated) {
    return { error: outcome.reason };
  }

  // A question is answered rather than queued, so send the user to the run that
  // holds the answer. Anything needing permission stays here, in the inbox.
  if (!outcome.approval) {
    redirect(`/runs/${outcome.delegated.id}`);
  }

  return {};
}

// --- integrations --------------------------------------------------------

/**
 * Start an OAuth flow by sending the browser to the provider's consent screen.
 *
 * The backend returns the URL in a JSON body rather than issuing a 302, because a
 * redirect in response to an XHR is followed invisibly and the client gets an
 * opaque CORS error instead of a consent screen (M11). A Server Action can do
 * what an XHR cannot: `redirect()` here is a real, top-level navigation.
 *
 * `state` is minted by the backend and stored in Redis before this returns, so
 * the callback — which arrives with no auth header at all — can be tied back to
 * this organization. Nothing about that binding passes through the browser.
 */
export async function connectProvider(provider: string): Promise<void> {
  const start = await apiFetch<ConnectStart>(`/integrations/${provider}/connect`);

  // Outside the try/catch that would otherwise swallow it: Next implements
  // `redirect` by throwing, so catching broadly here would turn a working
  // navigation into a silent no-op.
  redirect(start.authorize_url);
}

export async function disconnectIntegration(integrationId: string): Promise<ActionResult> {
  try {
    await apiFetch(`/integrations/${integrationId}`, { method: "DELETE" });
  } catch (error) {
    return explain(error);
  }

  revalidatePath("/integrations");
  return {};
}

// --- documents -----------------------------------------------------------

export async function uploadDocument(
  _previous: ActionResult,
  formData: FormData,
): Promise<ActionResult> {
  const file = formData.get("file");

  if (!(file instanceof File) || file.size === 0) {
    return { error: "Choose a file first." };
  }

  const upload = new FormData();
  upload.set("file", file);

  try {
    await apiUpload<DocumentRead>("/documents", upload);
  } catch (error) {
    return explain(error);
  }

  // The upload answers 202 and a worker does the real work, so the row appears as
  // `pending` and changes later. Revalidating shows it immediately as pending
  // rather than leaving the user unsure whether the upload landed at all.
  revalidatePath("/documents");
  return {};
}
