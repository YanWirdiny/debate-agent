import { useEffect, useState } from "react";
import { useView } from "./store";

const DRIFT_MAX_S = 0.3; // ARCHITECTURE.md section 9
const DRIFT_CHECK_MS = 5000;

/**
 * Broadcast delay for the audience view. The pipeline runs in real time; the audience video plays
 * this many seconds behind it, so by the time a sentence is spoken on screen its transcript (STT
 * ~0.5 s) and usually its flag (<= 1.5 s after the sentence ends) have already arrived. Captions are
 * then revealed word by word at the speaker's pace, and nothing appears ahead of the video.
 * Override with ?delay=<seconds> on the audience URL (0 = no delay, sentences appear when final).
 */
export const DEFAULT_AUDIENCE_DELAY_S = 2.5;

export function audienceDelay(search = location.search): number {
  const v = Number(new URLSearchParams(search).get("delay"));
  return new URLSearchParams(search).has("delay") && Number.isFinite(v) && v >= 0 && v <= 15 ? v : DEFAULT_AUDIENCE_DELAY_S;
}

/** Server clip time in seconds: (now - t0_wall) * speed. Same machine clock as the backend. */
export function clipTimeNow(t0Wall: number | null, speed: number): number | null {
  if (t0Wall == null) return null;
  return Math.max(0, (Date.now() / 1000 - t0Wall) * speed);
}

/** Re-renders every `ms` with the current clip time, minus `delay` (null before start). */
export function useClipTime(ms = 250, delay = 0): number | null {
  const t0 = useView((s) => s.t0Wall);
  const speed = useView((s) => s.speed);
  const now = () => {
    const t = clipTimeNow(t0, speed);
    return t == null ? null : t - delay * speed;
  };
  const [t, setT] = useState(now);
  useEffect(() => {
    setT(now());
    if (t0 == null) return;
    const id = window.setInterval(() => setT(now()), ms);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t0, speed, ms, delay]);
  return t;
}

/**
 * The time the viewer is watching: the video's own position while it plays (captions then follow
 * the picture exactly, even after a stall), else the delayed server clock.
 */
export function useDisplayTime(video: HTMLVideoElement | null, delay: number, ms = 100, offset = 0): number | null {
  const clock = useClipTime(ms, delay);
  if (clock == null) return null;
  if (video && !video.paused && !video.ended && video.readyState >= 2) return video.currentTime;
  return clock + offset;
}

/**
 * Keep a <video> on the server's clip clock minus `delay`: hold at 0 until the delayed clock starts,
 * then play, and every 5 s seek if drift > 300 ms. Speeds other than 1x (test replays) set the rate.
 * `offset`: where clip time 0 is in this video (original mode: the window start); `end`: pause there.
 */
export function useVideoSync(video: HTMLVideoElement | null, enabled = true, delay = 0, offset = 0, end = Infinity): void {
  const t0 = useView((s) => s.t0Wall);
  const speed = useView((s) => s.speed);
  useEffect(() => {
    if (!video || !enabled || t0 == null) return;
    let wait: number | undefined;
    const sync = () => {
      const now = clipTimeNow(t0, speed);
      if (now == null) return;
      const clip = now - delay * speed;
      const target = offset + clip;
      if (clip < 0) {
        video.pause();
        video.currentTime = offset;
        window.clearTimeout(wait);
        wait = window.setTimeout(sync, (-clip / speed) * 1000); // start exactly when the delay elapses
        return;
      }
      if (target >= end) {
        video.pause();
        return;
      }
      if (Number.isFinite(video.duration) && target > video.duration) return;
      if (Math.abs(video.currentTime - target) > DRIFT_MAX_S) video.currentTime = target;
      video.playbackRate = speed;
      if (video.paused) void video.play().catch(() => undefined);
    };
    sync();
    const id = window.setInterval(sync, DRIFT_CHECK_MS);
    const stopAtEnd = () => {
      if (video.currentTime >= end) video.pause(); // exact, not on the 5 s drift check
    };
    video.addEventListener("timeupdate", stopAtEnd);
    return () => {
      video.removeEventListener("timeupdate", stopAtEnd);
      window.clearInterval(id);
      window.clearTimeout(wait);
    };
  }, [video, enabled, t0, speed, delay, offset, end]);
}
