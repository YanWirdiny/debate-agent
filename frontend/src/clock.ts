import { useEffect, useState } from "react";
import { useView } from "./store";

const DRIFT_MAX_S = 0.3; // ARCHITECTURE.md section 9
const DRIFT_CHECK_MS = 5000;

/** Server clip time in seconds: (now - t0_wall) * speed. Same machine clock as the backend. */
export function clipTimeNow(t0Wall: number | null, speed: number): number | null {
  if (t0Wall == null) return null;
  return Math.max(0, (Date.now() / 1000 - t0Wall) * speed);
}

/** Re-renders every `ms` with the current clip time. */
export function useClipTime(ms = 250): number | null {
  const t0 = useView((s) => s.t0Wall);
  const speed = useView((s) => s.speed);
  const [t, setT] = useState(() => clipTimeNow(t0, speed));
  useEffect(() => {
    setT(clipTimeNow(t0, speed));
    if (t0 == null) return;
    const id = window.setInterval(() => setT(clipTimeNow(t0, speed)), ms);
    return () => window.clearInterval(id);
  }, [t0, speed, ms]);
  return t;
}

/**
 * Keep a <video> on the server's clip clock: play when the session starts, then every 5 s seek
 * if drift > 300 ms. Speeds other than 1x (test replays) set the playback rate.
 */
export function useVideoSync(video: HTMLVideoElement | null, enabled = true): void {
  const t0 = useView((s) => s.t0Wall);
  const speed = useView((s) => s.speed);
  useEffect(() => {
    if (!video || !enabled || t0 == null) return;
    const sync = () => {
      const target = clipTimeNow(t0, speed);
      if (target == null) return;
      if (Number.isFinite(video.duration) && target > video.duration) return;
      if (Math.abs(video.currentTime - target) > DRIFT_MAX_S) video.currentTime = target;
      video.playbackRate = speed;
      if (video.paused) void video.play().catch(() => undefined);
    };
    sync();
    const id = window.setInterval(sync, DRIFT_CHECK_MS);
    return () => window.clearInterval(id);
  }, [video, enabled, t0, speed]);
}
