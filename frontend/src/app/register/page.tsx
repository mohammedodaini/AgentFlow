import Link from "next/link";
import { redirect } from "next/navigation";

import { AuthForm } from "@/components/auth-form";
import { Card } from "@/components/ui";
import { signUp } from "@/lib/auth";
import { isSignedIn } from "@/lib/session";

export const metadata = { title: "Create an account · AgentFlow" };

export default async function RegisterPage() {
  if (await isSignedIn()) {
    redirect("/chat");
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <h1 className="mb-1 text-2xl font-semibold">Create an account</h1>
      <p className="mb-6 text-sm text-[--color-muted]">
        You get a personal organization to work in. Invite people to it later.
      </p>

      <Card className="p-6">
        <AuthForm action={signUp} submitLabel="Create account" withName />
      </Card>

      <p className="mt-6 text-center text-sm text-[--color-muted]">
        Already have one?{" "}
        <Link href="/login" className="font-medium text-[--color-accent] hover:underline">
          Sign in
        </Link>
      </p>
    </main>
  );
}
