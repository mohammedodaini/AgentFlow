import { UploadForm } from "@/components/upload-form";
import { Badge, Card, Empty } from "@/components/ui";
import { uploadDocument } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import type { DocumentRead, Page } from "@/lib/types";

export const metadata = { title: "Documents · AgentFlow" };

const TONES: Record<DocumentRead["status"], string> = {
  ready: "ok",
  failed: "bad",
  processing: "warn",
  pending: "warn",
};

export default async function DocumentsPage() {
  const documents = await apiFetch<Page<DocumentRead>>("/documents?limit=50");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Documents</h1>
        <p className="text-sm text-[--color-muted]">
          What the agent can answer from. Uploading returns immediately; indexing happens in the
          background, so a new file shows as pending until a worker has read it.
        </p>
      </div>

      <Card className="p-4">
        <UploadForm action={uploadDocument} />
      </Card>

      <Card>
        {documents.items.length === 0 ? (
          <Empty>No documents yet. Upload a PDF, a text file or some Markdown.</Empty>
        ) : (
          <ul className="divide-y divide-[--color-border]">
            {documents.items.map((document) => (
              <li key={document.id} className="flex items-center gap-3 px-4 py-3">
                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {document.title}
                </span>

                {/* The failure reason is written for the person who uploaded it
                    rather than the person who deployed (M5), so it is shown
                    rather than hidden behind a generic "failed". */}
                {document.error ? (
                  <span className="truncate text-xs text-[--color-danger]">{document.error}</span>
                ) : null}

                <Badge tone={TONES[document.status]}>{document.status}</Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
