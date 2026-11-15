import { expect, test, type Page } from "@playwright/test";

/**
 * The site as it is now, end to end in Chrome, against the e2e backend from playwright.config.ts
 * (REPLAY_TRANSCRIPT + INTEL=fake, no Deepgram key: no cost). Covers the presentation (slides and
 * keys), the recorded demo, a full live-clip run to its end, and a live room failing cleanly when
 * speech-to-text is not configured. The room's happy path needs Deepgram: it is covered by
 * backend/tests/pipeline/test_room.py (fake Deepgram) and a manual run.
 */
test.describe.configure({ mode: "serial" });

test.use({
  permissions: ["microphone"],
  launchOptions: { args: ["--mute-audio", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
});

const slideInView = (p: Page) =>
  p.evaluate(() => {
    const m = innerHeight / 2;
    return [...document.querySelectorAll<HTMLElement>("[data-slide]")].find((e) => {
      const r = e.getBoundingClientRect();
      return r.top <= m && r.bottom > m;
    })?.dataset.slide;
  });
const opacity = (p: Page, sel: string) => p.locator(sel).first().evaluate((e) => Number(getComputedStyle(e).opacity));
/** Wait until smooth scrolling (and the scrubbed animation) has come to rest. */
async function settle(p: Page) {
  let last = -1;
  for (let i = 0; i < 40; i++) {
    await p.waitForTimeout(200);
    const y = await p.evaluate(() => Math.round(scrollY));
    if (y === last) return;
    last = y;
  }
}
async function press(p: Page, key: string) {
  await p.keyboard.press(key);
  await settle(p);
}
const pipelineStep = (p: Page) => p.locator('#how svg[role="img"]').getAttribute("aria-label");

test("mapping helpers place the analysed sentences on the original", async ({ page }) => {
  const source = await (await page.request.get("/clips/economy_v2/source")).json();
  await page.goto("/");
  const r = await page.evaluate(async (src) => {
    const m = await import("/src/original.ts");
    const harris = { segment_id: "s:1", speaker_id: "harris", role: "speaker", text: "My plan", t_start: 4.46, t_end: 14.9, final: true };
    const lines = m.originalLines([harris as never], src as never);
    const shifted = lines.find((l: { segment_id: string }) => l.segment_id === "s:1");
    return {
      start: shifted.t_start, inCutBeforeHarris: m.inCut(50, src as never), inPiece: m.inCut(100, src as never),
      trimmedInCut: m.toTrimmed(50, src.edl), trimmedBefore: m.toTrimmed(10, src.edl),
    };
  }, source);
  expect(r.start).toBeCloseTo(4.46 + (86.97 - 4.47), 2);
  expect(r.inCutBeforeHarris).toBe(true);
  expect(r.inPiece).toBe(false);
  expect(r.trimmedBefore).toBe(-1);
  expect(r.trimmedInCut).toBeCloseTo(4.47, 2);
});

test("presentation: slides move with the keys; the problem photo gives way to the paragraph", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Every claim, checked/ })).toBeVisible();
  await press(page, "ArrowDown");
  expect(await slideInView(page)).toBe("1");
  expect(await opacity(page, ".problem-photo")).toBeGreaterThan(0.9); // photo first
  await press(page, "ArrowDown");
  expect(await opacity(page, ".problem-reveal")).toBeGreaterThan(0.9); // then the paragraph
  expect(await opacity(page, ".problem-photo")).toBeLessThan(0.1);
  await press(page, "ArrowDown");
  expect(await slideInView(page)).toBe("2");
  expect(await pipelineStep(page)).toContain("Hear");
  await press(page, "ArrowRight");
  expect(await pipelineStep(page)).toContain("Split");
  await press(page, "ArrowRight");
  expect(await pipelineStep(page)).toContain("Check");
  await press(page, "ArrowLeft");
  expect(await pipelineStep(page)).toContain("Split");
  await press(page, "ArrowUp"); // back up: to the problem slide's paragraph, then the photo
  await press(page, "ArrowUp");
  expect(await slideInView(page)).toBe("1");
});

test("recorded demo embeds the YouTube run; /operator goes to the live tab", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("heading", { name: "Watch a full run." })).toBeVisible();
  await expect(page.locator("iframe")).toHaveAttribute("src", /youtube-nocookie\.com\/embed\/QroA1SkaJTg/);
  await expect(page.getByText("not a live session")).toBeVisible();
  await page.goto("/operator");
  await expect(page).toHaveURL(/\/demo\/live$/);
});

test("live clip runs to its end: original video with sound, captions, then ENDED with a frozen clock", async ({ page }) => {
  test.setTimeout(150_000);
  await page.goto("/demo/live");
  await page.getByRole("combobox", { name: "Clip" }).selectOption("economy_v2");
  await page.getByRole("button", { name: "Load" }).click();
  await expect(page.getByRole("button", { name: "Start" })).toBeEnabled({ timeout: 60_000 });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByText("LIVE", { exact: true })).toBeVisible({ timeout: 15_000 });

  const video = page.locator("video");
  await expect(video).toHaveAttribute("src", /\/clips\/economy_v2\/original/);
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => !v.paused && v.readyState >= 2), { timeout: 15_000 }).toBe(true);
  expect(await video.evaluate((v: HTMLVideoElement) => v.muted)).toBe(false); // the operator is the sound now
  await expect(page.getByText(/When it comes to the economy/)).toBeVisible({ timeout: 15_000 });

  await expect(page.getByText("ENDED", { exact: true })).toBeVisible({ timeout: 90_000 });
  const clock = page.locator("header .tabular-nums");
  const t1 = await clock.innerText();
  await page.waitForTimeout(2_500);
  expect(await clock.innerText()).toBe(t1); // stopped, not counting on
  const [m, s] = t1.split(":").map(Number);
  expect(m * 60 + s).toBeLessThanOrEqual(55); // economy_v2 is 54 s long
});

test("live room without speech-to-text fails cleanly and can be retried", async ({ page }) => {
  await page.goto("/demo/room");
  await page.getByLabel("Debate topic").fill("Taxes and the deficit");
  await page.getByLabel("Debater 1 name").fill("Alice");
  await page.getByLabel("Debater 2 name").fill("Bob");
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByText(/needs DEEPGRAM_API_KEY/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Start" })).toBeEnabled(); // back to idle, not stuck warming up
  await expect(page.getByText("LIVE", { exact: true })).toHaveCount(0);
});
