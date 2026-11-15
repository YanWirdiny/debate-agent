import { useEffect, useRef } from "react";
import { useView } from "./store";
import type { ClientMessage, ServerMessage, VoicesMessage } from "./types";

const RETRY_MS = [500, 1000, 2000, 4000];

export interface ViewSocket {
  send: (m: ClientMessage) => boolean;
}

/** One WebSocket per view with auto-reconnect; every message goes through the store reducer. */
export function useViewSocket(view: "audience" | "operator", onMessage?: (m: ServerMessage) => void): ViewSocket {
  const wsRef = useRef<WebSocket | null>(null);
  const handler = useRef(onMessage);
  handler.current = onMessage;

  useEffect(() => {
    let stopped = false;
    let attempt = 0;
    let timer: number | undefined;
    const { apply, setConn } = useView.getState();

    const connect = () => {
      setConn("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/${view}`);
      wsRef.current = ws;
      ws.onopen = () => {
        attempt = 0;
        setConn("open");
      };
      ws.onmessage = (ev) => {
        let m: ServerMessage;
        try {
          m = JSON.parse(ev.data as string) as ServerMessage;
        } catch {
          return;
        }
        apply(m);
        handler.current?.(m);
      };
      ws.onclose = () => {
        setConn("closed");
        if (stopped) return;
        timer = window.setTimeout(connect, RETRY_MS[Math.min(attempt++, RETRY_MS.length - 1)]);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [view]);

  return {
    send: (m) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(JSON.stringify(m));
      return true;
    },
  };
}

export interface ServerState {
  session_id: string | null;
  clip_id: string | null;
  t0_wall: number | null;
  speed: number;
  source: string;
  intel: string;
  /** Clip seconds where the session ended; null/absent while it runs. */
  ended_clip_time?: number | null;
  /** Live room: participants and voice map (null for clip sessions). */
  voices?: VoicesMessage | null;
}

export async function fetchState(): Promise<ServerState | null> {
  try {
    const r = await fetch("/state");
    return r.ok ? ((await r.json()) as ServerState) : null;
  } catch {
    return null;
  }
}

export interface ClipInfo {
  clip_id: string;
  title: string;
  duration: number | null;
}

export async function fetchClips(): Promise<ClipInfo[]> {
  try {
    const r = await fetch("/clips");
    return r.ok ? ((await r.json()) as ClipInfo[]) : [];
  } catch {
    return [];
  }
}
