import type { Participant, VoicesMessage } from "../types";

export interface RoomNames {
  a: string;
  b: string;
  mod: string;
  topic: string;
}

/** "Kamala Harris" -> "kamala_harris": the speaker_id the backend expects (^[a-z0-9_]{1,32}$). */
export function slug(name: string, fallback: string): string {
  const s = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
  return s || fallback;
}

export function participantsFrom(n: RoomNames): Participant[] {
  const out: Participant[] = [
    { speaker_id: slug(n.a, "speaker_a"), name: n.a.trim() || "Speaker A", role: "speaker" },
    { speaker_id: slug(n.b, "speaker_b"), name: n.b.trim() || "Speaker B", role: "speaker" },
  ];
  if (out[1].speaker_id === out[0].speaker_id) out[1].speaker_id += "_2";
  if (n.mod.trim()) out.push({ speaker_id: "moderator", name: n.mod.trim(), role: "moderator" });
  return out;
}

/** Keys: 1 / 2 = debaters, M = moderator, 0 = back to automatic (voices). */
export function keyTarget(key: string, participants: Participant[]): string | null | undefined {
  const debaters = participants.filter((p) => p.role === "speaker");
  if (key === "1") return debaters[0]?.speaker_id;
  if (key === "2") return debaters[1]?.speaker_id;
  if (key === "m" || key === "M") return participants.find((p) => p.role === "moderator")?.speaker_id;
  if (key === "0") return null;
  return undefined; // not a room key
}

export function RoomSetup({ names, setNames, mics, micId, setMicId, level, disabled }: {
  names: RoomNames;
  setNames: (n: RoomNames) => void;
  mics: MediaDeviceInfo[];
  micId: string;
  setMicId: (id: string) => void;
  level: number;
  disabled: boolean;
}) {
  const input = "w-36 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm placeholder:text-zinc-500";
  return (
    <>
      <input className={`${input} w-56`} placeholder="Debate topic, e.g. the economy" value={names.topic} disabled={disabled}
        aria-label="Debate topic" maxLength={200} onChange={(e) => setNames({ ...names, topic: e.target.value })} />
      <input className={input} placeholder="Debater 1" value={names.a} disabled={disabled} aria-label="Debater 1 name"
        onChange={(e) => setNames({ ...names, a: e.target.value })} />
      <span className="text-sm text-zinc-500">vs</span>
      <input className={input} placeholder="Debater 2" value={names.b} disabled={disabled} aria-label="Debater 2 name"
        onChange={(e) => setNames({ ...names, b: e.target.value })} />
      <input className={input} placeholder="Moderator (optional)" value={names.mod} disabled={disabled} aria-label="Moderator name"
        onChange={(e) => setNames({ ...names, mod: e.target.value })} />
      <select value={micId} onChange={(e) => setMicId(e.target.value)} disabled={disabled} aria-label="Microphone"
        className="max-w-48 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm">
        <option value="">Default microphone</option>
        {mics.map((m, i) => (
          <option key={m.deviceId || i} value={m.deviceId}>
            {m.label || `Microphone ${i + 1}`}
          </option>
        ))}
      </select>
      <LevelMeter level={level} />
    </>
  );
}

function LevelMeter({ level }: { level: number }) {
  const pct = Math.min(100, Math.round(Math.sqrt(level) * 180)); // perceptual-ish: quiet speech still moves it
  return (
    <span className="flex items-center gap-1.5 text-xs text-zinc-500" title="Microphone level">
      mic
      <span className="h-2 w-20 overflow-hidden rounded-full bg-zinc-800">
        <span className="block h-full rounded-full bg-emerald-600 transition-[width] duration-100" style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

/** Right column in a room: who is speaking now, who each voice is, and the keys to fix it. */
export function RoomPanel({ voices, speaking, live, onAssign, onOverride }: {
  voices: VoicesMessage | null;
  speaking: string | null;
  live: boolean;
  onAssign: (voice: number, speakerId: string) => void;
  onOverride: (speakerId: string | null) => void;
}) {
  const people = voices?.participants ?? [];
  const name = (id: string | null | undefined) => people.find((p) => p.speaker_id === id)?.name ?? id ?? "nobody yet";
  const entries = Object.entries(voices?.voices ?? {});
  const debaters = people.filter((p) => p.role === "speaker");
  const mod = people.find((p) => p.role === "moderator");
  return (
    <div className="space-y-5">
      {voices?.topic && (
        <div>
          <p className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">Topic</p>
          <p className="mt-1 text-lg font-semibold">{voices.topic}</p>
        </div>
      )}
      <div className="rounded-lg bg-zinc-900 p-4">
        <p className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">Speaking now</p>
        <p className="mt-1 text-3xl font-bold tracking-tight">{live ? name(voices?.override ?? speaking) : "—"}</p>
        <p className="mt-1 text-xs text-zinc-500">
          {voices?.override ? "set by you (press 0 for automatic)" : "automatic, from the voice"}
        </p>
      </div>

      <div>
        <p className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">Voices</p>
        {entries.length === 0 && (
          <p className="mt-2 text-sm text-zinc-500">No voice heard yet. The first voice goes to the moderator (if named), then the debaters in order.</p>
        )}
        <ul className="mt-2 space-y-1.5">
          {entries.map(([voice, sid]) => (
            <li key={voice} className="flex items-center gap-2 text-sm">
              <span className="w-16 text-zinc-500">Voice {Number(voice) + 1}</span>
              <select value={sid} onChange={(e) => onAssign(Number(voice), e.target.value)}
                className="flex-1 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm" aria-label={`Voice ${Number(voice) + 1}`}>
                {people.map((p) => (
                  <option key={p.speaker_id} value={p.speaker_id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </li>
          ))}
        </ul>
      </div>

      <div>
        <p className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">Fix the speaker</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {[...debaters.map((p, i) => ({ key: String(i + 1), p })), ...(mod ? [{ key: "M", p: mod }] : [])].map(({ key, p }) => (
            <button key={key} type="button" disabled={!live} onClick={() => onOverride(p.speaker_id)}
              className={`btn ${voices?.override === p.speaker_id ? "btn-primary" : ""}`}>
              <kbd className="mr-1.5 font-mono text-xs opacity-70">{key}</kbd>
              {p.name}
            </button>
          ))}
          <button type="button" disabled={!live || !voices?.override} onClick={() => onOverride(null)} className="btn">
            <kbd className="mr-1.5 font-mono text-xs opacity-70">0</kbd>Automatic
          </button>
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          Voices are guessed from the sound. If a name is wrong, press its key while that person talks, or reassign the voice above.
        </p>
      </div>
    </div>
  );
}
