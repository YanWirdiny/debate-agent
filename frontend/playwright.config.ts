import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests in a real browser: `npm run e2e`.
 *
 * Starts its own backend (port 8100, REPLAY_TRANSCRIPT + INTEL=fake: no keys used, no cost, local
 * event log instead of Atlas) and its own Vite (port 5174), so it never touches a dev session on
 * 8000/5173. Uses the installed Google Chrome (`channel: "chrome"`): Playwright's own Chromium cannot
 * play the H.264/AAC clip videos, and video playback is what these tests check.
 */
const BACKEND_PORT = 8100;
const WEB_PORT = 5174;

export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    channel: "chrome",
    headless: true, // default autoplay policy on purpose: the audience must work from the join click
    // Headless Chrome still plays to the speakers. --mute-audio silences the output only; pages
    // keep video.muted === false, so the "this tab is the sound" checks stay meaningful.
    launchOptions: { args: ["--mute-audio"] },
  },
  webServer: [
    {
      command: `.venv/bin/uvicorn app.main:app --workers 1 --port ${BACKEND_PORT}`,
      cwd: "../backend",
      url: `http://127.0.0.1:${BACKEND_PORT}/health`,
      // No Deepgram key even if backend/.env has one: e2e never pays for speech-to-text.
      env: { REPLAY_TRANSCRIPT: "1", INTEL: "fake", MONGODB_URI: "mongodb://127.0.0.1:1", DEEPGRAM_API_KEY: "" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      env: { API_URL: `http://127.0.0.1:${BACKEND_PORT}` },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
