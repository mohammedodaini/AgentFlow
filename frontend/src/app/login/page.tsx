import Link from "next/link";
import { redirect } from "next/navigation";

import { AuthForm } from "@/components/auth-form";
import { Card } from "@/components/ui";
import { signIn } from "@/lib/auth";
import { isSignedIn } from "@/lib/session";

export const metadata = { title: "Sign in · AgentFlow" };

export default async function LoginPage() {
  // Checked on the server before rendering, so an already-signed-in user never
  // sees a login form flash before being redirected away from it.
  if (await isSignedIn()) {
    redirect("/chat");
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <h1 className="mb-1 text-2xl font-semibold">Sign in</h1>
      <p className="mb-6 text-sm text-[--color-muted]">
        An AI employee for your business — it reads your documents and asks before it acts.
      </p>

      <Card className="p-6">
        <AuthForm action={signIn} submitLabel="Sign in" />
      </Card>

      <p className="mt-6 text-center text-sm text-[--color-muted]">
        No account yet?{" "}
        <Link href="/register" className="font-medium text-[--color-accent] hover:underline">
          Create one
        </Link>
      </p>
    </main>
  );
}
