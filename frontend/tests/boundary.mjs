/**
 * Failure-path drill: what a user sees when things go wrong.
 *
 * Destructive — it kills the API — so it is NOT part of `make smoke`. Run it by
 * hand before a release, with the whole stack up:
 *
 *     make dev  &  make web        (or a container stack)
 *     node tests/boundary.mjs
 *
 * Everything here was added after a production audit found that six server pages
 * called the API with no error handling and there was no error boundary anywhere,
 * so an API outage rendered Next's bare "Application error: a server-side
 * exception has occurred".
 */
import { chromium } from "playwright";
import { execSync } from "node:child_process";

const WEB = process.env.SMOKE_BASE_URL ?? "http://localhost:3000";
const API = process.env.SMOKE_API_URL ?? "http://localhost:8000";
const PASSWORD = "correct-horse-battery-staple";

const b = await chromium.launch();
const page = await b.newPage();
let fails = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "  ✓" : "  ✗"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) fails++;
};

async function signUp(label) {
  await page.goto(`${WEB}/register`);
  await page.fill('input[name="full_name"]', label);
  await page.fill('input[name="email"]', `boundary-${label}-${Date.now()}@agentflow.dev`);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL("**/chat", { timeout: 30000 });
}

// --- a mistyped URL ------------------------------------------------------
await page.goto(`${WEB}/no-such-page`);
let text = await page.innerText("body");
check("a 404 names the product and offers a way back", /Page not found/.test(text));
check("it is not Next's bare default", !/This page could not be found/.test(text));

// --- a session the backend has revoked -----------------------------------
// The layout guard only checks that a refresh *cookie exists*; it cannot tell a
// live token from a revoked one. Logging out behind the browser's back and
// dropping the access cookie reproduces "thirty minutes later, and IT revoked
// your session" exactly.
await signUp("revoked");
const refresh = (await page.context().cookies()).find((c) => c.name === "af_refresh")?.value;
check("the refresh token is httpOnly, so only the server can read it", Boolean(refresh));

await fetch(`${API}/api/v1/auth/logout`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ refresh_token: refresh }),
});
// Only the access cookie. Revoking the refresh token alone proves nothing — the
// access token is valid for another half hour and no request would touch it.
await page.context().clearCookies({ name: "af_access" });

await page.goto(`${WEB}/documents`, { waitUntil: "domcontentloaded" });
check("a revoked session lands on the sign-in form", page.url().includes("/login"), `at ${page.url()}`);
check("and the cookies were actually dropped",
  !(await page.context().cookies()).some((c) => c.name === "af_refresh"));

// --- the API is down -----------------------------------------------------
// A *fresh* session, because the check above deliberately ended signed out.
await signUp("outage");
console.log("  … killing the API");
execSync("pkill -f 'uvicorn app.main:app --port 8000' || true");
await new Promise((r) => setTimeout(r, 2000));

await page.goto(`${WEB}/documents`, { waitUntil: "domcontentloaded" });
text = await page.innerText("body");
check("an outage shows our error page", /could not load/i.test(text), `got: ${text.slice(0, 90)}`);
check("not the bare Application error string",
  !/Application error: a server-side exception/.test(text));
check("a retry control is offered", /Try again/.test(text));
check("the shell survives, so the user can go elsewhere",
  /Chat/.test(text) && /Approvals/.test(text));
check("no stack trace, file path or traceback is shown",
  !/at \/|\.tsx:|\.py:|Traceback|node_modules/.test(text));

await b.close();
console.log(fails === 0 ? "\nPASS" : `\nFAIL ${fails}`);
process.exit(fails === 0 ? 0 : 1);
