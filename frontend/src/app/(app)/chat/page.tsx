import Link from "next/link";

import { Button, Card, Empty } from "@/components/ui";
import { startConversation } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import type { Conversation, Page } from "@/lib/types";

export const metadata = { title: "Chat · AgentFlow" };

export default async function ChatIndex() {
  const conversations = await apiFetch<Page<Conversation>>("/conversations?limit=50");

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Conversations</h1>
          <p className="text-sm text-[--color-muted]">
            Ask about your documents. Answers cite the passage they came from.
          </p>
        </div>

        <form action={startConversation}>
          <Button type="submit">New conversation</Button>
        </form>
      </div>

      <Card>
        {conversations.items.length === 0 ? (
          <Empty>No conversations yet. Start one to ask your first question.</Empty>
        ) : (
          <ul className="divide-y divide-[--color-border]">
            {conversations.items.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={`/chat/${conversation.id}`}
                  className="flex items-center justify-between gap-4 px-4 py-3 transition hover:bg-[--color-canvas]"
                >
                  <span className="truncate text-sm font-medium">{conversation.title}</span>
                  <time
                    dateTime={conversation.created_at}
                    className="shrink-0 text-xs text-[--color-muted]"
                  >
                    {new Date(conversation.created_at).toLocaleDateString()}
                  </time>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
