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
// M14 put a second proposal form on this page, so the selectors are scoped to a
// card rather than to `input[name="instruction"]` — which now matches two.
await page.goto("http://localhost:3000/approvals");
const calendarForm = page.locator('div:has(> h2:text-is("Draft a calendar change"))');
await calendarForm.locator('input[name="instruction"]').fill("Schedule a design review on 2026-09-10 09:00");
await calendarForm.locator('button:has-text("Draft it")').click();
await page.waitForSelector("text=/Create a calendar event/", { timeout: 30000 });
check("the proposal appears in the inbox", (await page.textContent("body")).includes("design review"));

await page.fill('input[name="reason"]', "Already booked.");
await page.click('button:has-text("Reject")');
await page.waitForTimeout(2500);
const afterReject = await page.textContent("body");
check("rejecting clears it from the inbox", !afterReject.includes("2026-09-10") && !afterReject.includes("10 September"));

// --- M14: the email agent drafts, and sends nothing ---
const emailForm = page.locator('div:has(> h2:text-is("Draft an email"))');
await emailForm.locator('input[name="instruction"]').fill(
  "Email ada@agentflow.dev about the board pack saying the numbers are final.",
);
await emailForm.locator('button:has-text("Draft it")').click();
await page.waitForSelector("text=/Send an email to/", { timeout: 30000 });
const inbox = await page.textContent("body");
check("the email proposal reaches the inbox", inbox.includes("ada@agentflow.dev"));
// The body is what actually gets sent, so it is what the person deciding must
// see — the summary alone would be a second account of the message.
check("the exact message is shown for review", inbox.includes("the numbers are final."));

await page.click('button:has-text("Reject")');
await page.waitForTimeout(2500);
check(
  "rejecting the email clears it too",
  !(await page.textContent("body")).includes("the numbers are final."),
);

// --- M14: integrations ---
await page.goto("http://localhost:3000/integrations");
const integrations = await page.textContent("body");
check("every configured provider is offered", ["Gmail", "Google Calendar", "Slack", "Notion", "GitHub", "Stripe"].every((name) => integrations.includes(name)));
// Google Drive is in the Provider enum and deliberately unimplemented. A button
// leading to a 404 is worse than no button.
check("Google Drive is not offered", !integrations.includes("Google Drive"));
check("no scope string leaks a token", !/ya29\.|xoxb-|sk_live/.test(integrations));

// The offline authorization server lives at a `.test` domain, which RFC 2606
// reserves and no DNS resolves — deliberately, so a stray offline URL in a real
// deployment fails loudly instead of quietly reaching somebody's server. That
// also means the navigation cannot *complete* here, so the assertion is on the
// request the browser attempted rather than on `page.url()`, which never changes.
let authorizeRequest = null;
page.on("request", (request) => {
  if (request.url().includes("offline.agentflow.test")) {
    authorizeRequest = request.url();
  }
});

await page.locator("li", { hasText: "Slack" }).locator('button:has-text("Connect")').click();
await page.waitForTimeout(4000);
check(
  "connecting leaves our origin for an authorization server",
  authorizeRequest !== null,
  `attempted: ${authorizeRequest}`,
);
// Without an unguessable, single-use state carrying the tenant binding, an
// attacker's crafted callback URL connects *their* account to a victim's
// organization (ADR-0014). It is the only credential the callback can carry.
check(
  "the consent URL carries a state parameter",
  authorizeRequest !== null && authorizeRequest.includes("state="),
);

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
