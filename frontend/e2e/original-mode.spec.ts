import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

/**
 * Original mode on economy_v2, end to end in Chrome: operator + audience show the same untrimmed
 * video in sync, only the joined audience tab makes sound, the cut regions get "not fact-checked"
 * captions, the operator's kill window follows the audience, and playback stops when the backend
 * connection is lost. One live session drives every check, so the tests run in order.
 */
test.describe.configure({ mode: "serial" });

const DELAY_S = 2.5;
let operator: Page;
let audience: Page;
let audienceWs: WebSocketRoute | null = null;
let cutAudience = false;
let source: { window: { start: number; end: number }; edl: { trim_start: number; trim_end: number; orig_start: number }[] };
let t0 = 0;

const videoState = (p: Page) =>
  p.locator("video").evaluate((v: HTMLVideoElement) => ({
    src: v.currentSrc || v.src, muted: v.muted, paused: v.paused, time: v.currentTime, ready: v.readyState, error: v.error?.code ?? null,
  }));

/** Where the audience should be in the original right now (window start + clock - broadcast delay). */
const expectedViewerTime = () => source.window.start + (Date.now() / 1000 - t0) - DELAY_S;
const elapsed = () => Date.now() / 1000 - t0;
async function waitUntilElapsed(s: number) {
  const ms = (t0 + s) * 1000 - Date.now();
  if (ms > 0) await new Promise((r) => setTimeout(r, ms));
}

test.beforeAll(async ({ browser }) => {
  const ctx = await browser.newContext(); // one browser profile: tabs share BroadcastChannel
  operator = await ctx.newPage();
  audience = await ctx.newPage();
  // Pass the audience socket through, keeping a handle so a test can cut it like a dead backend.
  await audience.routeWebSocket(/\/ws\/audience$/, (ws) => {
    if (cutAudience) return void ws.close();
    audienceWs = ws;
    ws.connectToServer();
  });
  source = await (await operator.request.get("/clips/economy_v2/source")).json();
});

test("mapping helpers place the analysed sentences on the original", async () => {
  await operator.goto("/");
  const r = await operator.evaluate(async (src) => {
    const m = await import("/src/original.ts");
    // A sentence starting 10 ms before the moderator/Harris boundary (the real economy_v2 case).
    const harris = { segment_id: "s:1", speaker_id: "harris", role: "speaker", text: "My plan", t_start: 4.46, t_end: 14.9, final: true };
    const lines = m.originalLines([harris as never], src as never);
    const shifted = lines.find((l: { segment_id: string }) => l.segment_id === "s:1");
    return {
      start: shifted.t_start, cutLines: lines.filter((l: { unchecked?: boolean }) => l.unchecked).length,
      inCutBeforeHarris: m.inCut(50, src as never), inPiece: m.inCut(100, src as never),
      trimmedInCut: m.toTrimmed(50, (src as typeof source).edl), trimmedBefore: m.toTrimmed(10, (src as typeof source).edl),
      trimmedInHarris: m.toTrimmed(90, (src as typeof source).edl),
    };
  }, source);
  expect(r.start).toBeCloseTo(4.46 + (86.97 - 4.47), 2); // not 21.4 (moderator piece offset)
  expect(r.cutLines).toBeGreaterThan(10);
  expect(r.inCutBeforeHarris).toBe(true);
  expect(r.inPiece).toBe(false);
  expect(r.trimmedBefore).toBe(-1); // before the window: nothing on air yet
  expect(r.trimmedInCut).toBeCloseTo(4.47, 2); // in the cut: everything before it has aired
  expect(r.trimmedInHarris).toBeCloseTo(4.47 + (90 - 86.97), 1);
});

test("operator loads and starts; both views use the original video", async () => {
  await operator.goto("/operator");
  await audience.goto("/audience");
  await operator.getByRole("combobox", { name: "Clip" }).selectOption("economy_v2");
  await operator.getByRole("button", { name: "Load" }).click();
  await expect(operator.getByRole("button", { name: "Start" })).toBeEnabled({ timeout: 60_000 });
  await audience.getByRole("button", { name: /Click to join/ }).click();
  await operator.getByRole("button", { name: "Start" }).click();
  await expect(operator.getByText("LIVE", { exact: true })).toBeVisible({ timeout: 15_000 });
  t0 = (await (await operator.request.get("/state")).json()).t0_wall;
  expect(t0).toBeGreaterThan(0);

  await expect(operator.getByText("original (as the audience sees it)")).toBeVisible();
  const op = await videoState(operator);
  expect(op.src).toContain("/clips/economy_v2/original");
  expect(op.muted).toBe(true); // the operator preview never makes sound
  const au = await videoState(audience);
  expect(au.src).toContain("/clips/economy_v2/original");
  expect(au.muted).toBe(false); // joined: this tab is the sound
  expect(au.error).toBeNull();
});

test("video waits for the broadcast delay at the window start, then follows the clock", async () => {
  if (elapsed() < DELAY_S - 0.5) {
    const v = await videoState(audience);
    expect(v.time).toBeCloseTo(source.window.start, 0);
  }
  await waitUntilElapsed(DELAY_S + 4);
  for (const page of [audience, operator]) {
    const v = await videoState(page);
    expect(v.paused).toBe(false);
    expect(Math.abs(v.time - expectedViewerTime())).toBeLessThan(0.6);
  }
});

test("moderator line, then cut captions marked not fact-checked", async () => {
  const bar = audience.locator("[aria-live=off]");
  await expect(bar).toContainText("When it comes to the economy", { timeout: 10_000 });
  // Viewer reaches the first cut at original 21.47 s (= elapsed 7 s); Harris's unanalysed answer follows.
  await waitUntilElapsed(DELAY_S + (25 - source.window.start));
  await expect(bar).toContainText("So I was raised", { timeout: 5_000 });
  await expect(bar.getByText("not fact-checked").first()).toBeVisible();
  await expect(audience.getByText("Outside the analysed excerpt")).toBeVisible();
  await expect(audience.getByText(/Agent is \d+ s ahead/)).toBeVisible();
});

test("operator kill window follows the audience: Harris's analysed sentence is not on air during the cut", async () => {
  // The agent finishes Harris's first sentence at trimmed ~15 s; the viewer is then in the cut.
  await expect(operator.getByText(/My plan is to give/)).toBeVisible({ timeout: 30_000 });
  const row = operator.locator("li, div").filter({ hasText: /My plan is to give/ }).last();
  await expect(row.getByText("not on air")).toBeVisible();
  expect(expectedViewerTime()).toBeLessThan(source.edl[1].orig_start); // still before Harris on air
});

test("a second audience tab that joins mutes the first", async () => {
  const second = await audience.context().newPage();
  await second.goto("/audience");
  await second.getByRole("button", { name: /Click to join/ }).click();
  await expect.poll(async () => (await videoState(second)).muted).toBe(false);
  await expect(audience.getByRole("button", { name: /Click to join/ })).toBeVisible({ timeout: 5_000 });
  expect((await videoState(audience)).muted).toBe(true);
  await second.close();
});

test("lost backend connection pauses the audience instead of playing on", async () => {
  await audience.getByRole("button", { name: /Click to join/ }).click();
  await expect.poll(async () => (await videoState(audience)).paused, { timeout: 8_000 }).toBe(false);
  cutAudience = true;
  await audienceWs?.close();
  await expect(audience.getByText("Connection lost · playback paused")).toBeVisible({ timeout: 8_000 });
  const v = await videoState(audience);
  expect(v.paused).toBe(true);
  const t = v.time;
  await new Promise((r) => setTimeout(r, 2_000));
  expect((await videoState(audience)).time).toBe(t); // really stopped, not resumed by the sync loop
});
