import Link from "next/link";
import { notFound } from "next/navigation";

import { Composer } from "@/components/composer";
import { Badge, Card, Empty } from "@/components/ui";
import { sendMessage } from "@/lib/actions";
import { ApiError, apiFetch } from "@/lib/api";
import type { Message } from "@/lib/types";

export const metadata = { title: "Conversation · AgentFlow" };

export default async function ConversationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let messages: Message[];

  try {
    messages = await apiFetch<Message[]>(`/conversations/${id}/messages?limit=200`);
  } catch (error) {
    // The backend answers 404 for another tenant's thread as well as for one that
    // does not exist — deliberately, so the API cannot be used to discover which
    // ids are real. Rendering Next's 404 keeps that property in the UI instead of
    // leaking the difference through a distinct error page.
    if (error instanceof ApiError && error.status === 404) {
      notFound();
    }

    throw error;
  }

  return (
    <div className="space-y-6">
      <Link href="/chat" className="text-sm text-[--color-muted] hover:text-[--color-ink]">
        ← All conversations
      </Link>

      <Card className="divide-y divide-[--color-border]">
        {messages.length === 0 ? (
          <Empty>Ask the first question.</Empty>
        ) : (
          messages.map((message) => <Turn key={message.id} message={message} />)
        )}
      </Card>

      <Composer action={sendMessage.bind(null, id)} />
    </div>
  );
}

function Turn({ message }: { message: Message }) {
  const isUser = message.role === "user";

  return (
    <article className="flex gap-3 px-4 py-4">
      <div className="w-20 shrink-0">
        <Badge tone={isUser ? "muted" : "ok"}>{isUser ? "You" : "AgentFlow"}</Badge>
      </div>

      <div className="min-w-0 flex-1 space-y-2">
        {/* `whitespace-pre-wrap` because the model's answers contain newlines and
            the citation markers sit at the end of sentences — collapsing that
            turns a readable answer into a wall. */}
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>

        {message.agent_run_id ? (
          // The trace is a client-facing surface by design (ADR-0012): when an
          // answer looks wrong, "what did it search for, and what came back?" is
          // often a question the user can answer faster than we can.
          <Link
            href={`/runs/${message.agent_run_id}`}
            className="inline-block text-xs text-[--color-muted] underline-offset-2 hover:underline"
          >
            How this answer was produced
          </Link>
        ) : null}
      </div>
    </article>
  );
}
