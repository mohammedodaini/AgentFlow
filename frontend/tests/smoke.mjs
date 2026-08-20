/**
 * The smoke test: can a person actually use this?
 *
 * `pnpm build` proves the app compiles and `pnpm lint` proves it is tidy. Neither
 * proves that somebody can register, ask a question and get an answer — and that
 * is the bar this milestone was held to. So this drives a real Chromium against a
 * real backend, through the real forms.
 *
 * Not a unit test, and deliberately not in a test runner. It needs the whole
 * system up (Postgres, Redis, the API, the arq worker, this app), which is a
 * precondition no `pnpm test` should silently assume. `make smoke` from the repo
 * root is the entry point, and it says what it needs.
 *
 * Three of the assertions are about security rather than function:
 *
 *   - the session cookies are httpOnly,
 *   - `document.cookie` cannot see them,
 *   - the run trace never contains `checkpoint`.
 *
 * The first two are the whole argument of `src/lib/session.ts` — a refresh token
 * is a seven-day credential, and in `localStorage` one injected script exfiltrates
 * it. The third is ADR-0012's: the graph's internal state is not a public
 * contract. All three would pass silently if they regressed, because nothing
 * about the page would look different.
 *
 * Usage:
 *   make up && make dev && make worker && (cd frontend && pnpm start)
 *   make smoke
 */
import { chromium } from "playwright";

const EMAIL = `browser-${Date.now()}@example.com`;
const PASSWORD = "correct-horse-battery-staple";
const ok = [];
const fail = [];
const check = (name, cond, detail = "") =>
  cond ? ok.push(name) : fail.push(`${name} ${detail}`);

const browser = await chromium.launch();
const page = await browser.newPage();
page.on("pageerror", (e) => fail.push(`console error: ${e.message}`));

// --- register through the real form (a Server Action) ---
await page.goto("http://localhost:3000/register");
await page.fill('input[name="full_name"]', "Ada Lovelace");
await page.fill('input[name="email"]', EMAIL);
await page.fill('input[name="password"]', PASSWORD);
await Promise.all([page.waitForURL("**/chat"), page.click('button[type="submit"]')]);
check("register redirects to /chat", page.url().endsWith("/chat"));

// --- the credential must not be reachable from JavaScript ---
const cookieNames = (await page.context().cookies()).map((c) => c.name);
const httpOnly = (await page.context().cookies())
  .filter((c) => ["af_access", "af_refresh"].includes(c.name))
  .every((c) => c.httpOnly);
const visibleToJs = await page.evaluate(() => document.cookie);
check("session cookies exist", cookieNames.includes("af_access") && cookieNames.includes("af_refresh"));
check("tokens are httpOnly", httpOnly);
check("tokens invisible to document.cookie", !visibleToJs.includes("af_access"), `got: ${visibleToJs}`);

// --- start a conversation and ask something (Server Actions) ---
await page.click('button:has-text("New conversation")');
await page.waitForURL("**/chat/**");
await page.fill('input[name="content"]', "How are expenses reimbursed?");
await page.click('button:has-text("Send")');
await page.waitForSelector("text=/reimbursed/i", { timeout: 30000 });
const transcript = await page.textContent("body");
check("the question is shown", transcript.includes("How are expenses reimbursed?"));
check("an answer came back", /reimbursed monthly|could not find/i.test(transcript));
check("a trace link is offered", transcript.includes("How this answer was produced"));

// --- the trace page ---
await page.click("text=How this answer was produced");
await page.waitForURL("**/runs/**");
const trace = await page.textContent("body");
check("trace shows the graph nodes", trace.includes("retrieve") && trace.includes("generate"));
check("checkpoint is never published", !trace.includes("checkpoint"));

// --- propose a calendar action, then reject it ---
await page.goto("http://localhost:3000/approvals");
await page.fill('input[name="instruction"]', "Schedule a design review on 2026-09-10 09:00");
await page.click('button:has-text("Draft it")');
await page.waitForSelector("text=/Create a calendar event/", { timeout: 30000 });
check("the proposal appears in the inbox", (await page.textContent("body")).includes("design review"));

await page.fill('input[name="reason"]', "Already booked.");
await page.click('button:has-text("Reject")');
await page.waitForTimeout(2500);
const afterReject = await page.textContent("body");
check("rejecting clears it from the inbox", !afterReject.includes("2026-09-10") && !afterReject.includes("10 September"));

// --- upload a document ---
await page.goto("http://localhost:3000/documents");
await page.setInputFiles('input[type="file"]', {
  name: "policy.txt",
  mimeType: "text/plain",
  buffer: Buffer.from("Holiday requests need two weeks notice."),
});
await page.click('button:has-text("Upload")');
await page.waitForSelector("text=policy.txt", { timeout: 20000 });
check("the upload appears", (await page.textContent("body")).includes("policy.txt"));

// --- sign out, and the guard ---
await page.click('button:has-text("Sign out")');
await page.waitForURL("**/login");
await page.goto("http://localhost:3000/chat");
check("signed out cannot reach /chat", page.url().includes("/login"));

await browser.close();

console.log(`\nPASS ${ok.length}`);
ok.forEach((n) => console.log("  ✓", n));
if (fail.length) {
  console.log(`\nFAIL ${fail.length}`);
  fail.forEach((n) => console.log("  ✗", n));
  process.exit(1);
}
