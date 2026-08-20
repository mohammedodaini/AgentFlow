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
import { redirect } from "next/navigation";

import { ApiError, apiFetch, apiUpload } from "@/lib/api";
import type { Approval, Conversation, DocumentRead, Proposal, Turn } from "@/lib/types";

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
