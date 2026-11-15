"""Find where each piece of a trimmed clip comes from in the original (untrimmed) video.

The agent analyses the trimmed clip; the viewer can watch the original. To map a sentence's time in
the trimmed clip to the original, we need the cut list (EDL): for each kept piece, where it starts
and ends in the trimmed clip and where it starts in the original. This script recovers it from the
audio alone, so nobody has to time the cuts by hand.

    python scripts/align_clip.py economy_v2 path/to/original.mp4            # print the EDL
    python scripts/align_clip.py economy_v2 path/to/original.mp4 --write    # + data/clips/<id>/source.json

Method (numpy only): both files are decoded to 8 kHz mono with ffmpeg and turned into 32 log-band
energies every 10 ms. Each 3 s window of the trimmed clip is located in the original by normalized
cross-correlation (FFT). Consecutive windows with the same offset form one piece; the cut between
two pieces is placed at the 10 ms frame that best explains both sides. Accuracy: ~10-20 ms.

source.json is kept out of clip.json because `Clip` forbids unknown keys (contract change later).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CLIPS = ROOT / "data" / "clips"

SR = 8000
HOP = 80            # 10 ms frames
WIN = 512           # 15.6 Hz bins, so every band below has data
BANDS = 32
FPS = SR // HOP     # 100 frames per second
CHUNK_S = 3.0
STEP_S = 1.0
MIN_SCORE = 0.6     # below this a window is silence/noise and is not trusted
SAME_OFFSET_FRAMES = 3


def decode(path: Path) -> np.ndarray:
    """Any audio/video file -> float32 mono 8 kHz (ffmpeg)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def features(x: np.ndarray) -> np.ndarray:
    """(frames, BANDS) log band energies, each band z-normalized over the file."""
    n = max(0, (len(x) - WIN) // HOP + 1)
    window = np.hanning(WIN).astype(np.float32)
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    edges = np.geomspace(150, 3800, BANDS + 1)
    band_of = np.digitize(freqs, edges) - 1
    mask = (band_of >= 0) & (band_of < BANDS)
    out = np.empty((n, BANDS), dtype=np.float32)
    for start in range(0, n, 50_000):  # blocks keep memory flat on a 2 h debate
        idx = np.arange(start, min(n, start + 50_000))[:, None] * HOP + np.arange(WIN)
        power = np.abs(np.fft.rfft(x[idx] * window, axis=1)) ** 2
        energy = np.zeros((len(idx), BANDS), dtype=np.float64)
        np.add.at(energy.T, band_of[mask], power[:, mask].T)
        out[start:start + len(idx)] = energy
    # Noise floor at 1% of each band's median energy: silence becomes a normal low value instead of
    # log(0) outliers that swamp the correlation.
    floor = 0.01 * np.median(out, axis=0) + 1e-12
    out = np.log(out + floor)
    out = out[:, [b for b in range(BANDS) if (band_of[mask] == b).any()]]  # drop a band with no bins
    out -= out.mean(axis=0)
    out /= out.std(axis=0) + 1e-6
    return out


class Locator:
    """Pearson correlation (per band, mean removed) of a feature window against the whole original.
    Mean removal makes silence match nothing: a flat window has no shape to correlate."""

    def __init__(self, orig: np.ndarray, max_len: int) -> None:
        self.orig = orig
        self.n = 1 << int(np.ceil(np.log2(len(orig) + max_len)))
        self.spec = np.fft.rfft(orig.astype(np.float64), self.n, axis=0)  # float32 FFT is too coarse here
        o = orig.astype(np.float64)
        zero = np.zeros((1, orig.shape[1]))
        self.s1 = np.concatenate([zero, np.cumsum(o, axis=0)])        # per-band running sums
        self.s2 = np.concatenate([zero, np.cumsum(o * o, axis=0)])

    def locate(self, win: np.ndarray) -> tuple[int, float]:
        L = len(win)
        w = win - win.mean(axis=0)
        w_norm = np.sqrt((w * w).sum(axis=0))                          # per band
        if w_norm.sum() < 1e-3 * L:
            return 0, 0.0                                              # flat window (silence)
        corr = np.fft.irfft(self.spec * np.conj(np.fft.rfft(w.astype(np.float64), self.n, axis=0)), self.n, axis=0)
        valid = len(self.orig) - L + 1
        corr = corr[:valid]                                            # sum(w * o_window) per band
        mean = (self.s1[L:L + valid] - self.s1[:valid]) / L
        # Features have unit variance, so a window's sum of squares is ~L. Floor both sides at 5% of
        # that: a silent band then scores ~0 instead of dividing by ~0.
        floor = 0.05 * L
        var = np.maximum((self.s2[L:L + valid] - self.s2[:valid]) - L * mean * mean, floor)
        score = np.clip(corr / (np.sqrt(var) * np.sqrt(np.maximum(w_norm ** 2, floor))), -1, 1).mean(axis=1)
        best = int(np.argmax(score))
        return best, float(score[best])


def align(trim: np.ndarray, orig: np.ndarray) -> list[dict]:
    L, step = int(CHUNK_S * FPS), int(STEP_S * FPS)
    if len(trim) < L:
        raise SystemExit("trimmed clip is shorter than one 3 s window")
    loc = Locator(orig, L)
    starts = list(range(0, len(trim) - L + 1, step))
    if starts[-1] != len(trim) - L:
        starts.append(len(trim) - L)
    hits = []
    for s in starts:
        pos, score = loc.locate(trim[s:s + L])
        hits.append((s, pos - s, score))
    good = [h for h in hits if h[2] >= MIN_SCORE]
    if not good:
        raise SystemExit("no part of the trimmed clip was found in the original (wrong file?)")

    # Group consecutive trusted windows by offset (orig_frame - trim_frame).
    groups: list[list[tuple[int, int, float]]] = []
    for h in good:
        if groups and abs(h[1] - groups[-1][-1][1]) <= SAME_OFFSET_FRAMES:
            groups[-1].append(h)
        else:
            groups.append([h])
    # A window straddling a cut can match a third, wrong offset: drop one-window groups (a real piece
    # under ~3 s cannot be located reliably anyway), then merge neighbours that share an offset.
    if len(groups) > 1:
        groups = [g for g in groups if len(g) > 1] or groups
    merged: list[list[tuple[int, int, float]]] = []
    for g in groups:
        if merged and abs(np.median([h[1] for h in g]) - np.median([h[1] for h in merged[-1]])) <= SAME_OFFSET_FRAMES:
            merged[-1].extend(g)
        else:
            merged.append(g)
    groups = merged
    offsets = [int(np.median([h[1] for h in g])) for g in groups]

    # Cut between neighbouring pieces: the frame c minimizing error(A before c) + error(B from c).
    cuts = [0]
    for i in range(1, len(groups)):
        lo, hi = groups[i - 1][-1][0], groups[i][0][0] + L
        dA, dB = offsets[i - 1], offsets[i]
        t = np.arange(lo, hi)
        okA = (t + dA >= 0) & (t + dA < len(orig))
        okB = (t + dB >= 0) & (t + dB < len(orig))
        eA = np.where(okA, ((trim[t] - orig[np.clip(t + dA, 0, len(orig) - 1)]) ** 2).sum(axis=1), 1e6)
        eB = np.where(okB, ((trim[t] - orig[np.clip(t + dB, 0, len(orig) - 1)]) ** 2).sum(axis=1), 1e6)
        total = np.concatenate([[0], np.cumsum(eA)]) + np.concatenate([np.cumsum(eB[::-1])[::-1], [0]])
        cuts.append(int(lo + np.argmin(total)))
    cuts.append(len(trim) + (WIN // HOP))  # last frame covers the tail of the audio

    edl = []
    for i, d in enumerate(offsets):
        a, b = cuts[i], cuts[i + 1]
        edl.append({"trim_start": round(a / FPS, 2), "trim_end": round(b / FPS, 2),
                    "orig_start": round((a + d) / FPS, 2),
                    "score": round(float(np.mean([h[2] for h in groups[i]])), 3)})
    edl[-1]["trim_end"] = round(min(edl[-1]["trim_end"], len(trim) / FPS + WIN / SR), 2)
    return edl


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip_id")
    ap.add_argument("original", type=Path, help="original (untrimmed) video or audio file")
    ap.add_argument("--trimmed", type=Path, help="default: data/clips/<id>/audio.wav")
    ap.add_argument("--url", default="", help="where the original comes from (stored in source.json)")
    ap.add_argument("--write", action="store_true", help="write data/clips/<id>/source.json")
    args = ap.parse_args()
    clip_dir = CLIPS / args.clip_id
    trimmed = args.trimmed or clip_dir / "audio.wav"
    if not trimmed.exists() or not args.original.exists():
        sys.exit(f"missing file: {trimmed if not trimmed.exists() else args.original}")

    print(f"decoding {trimmed.name} and {args.original.name} ...", flush=True)
    t_audio, o_audio = decode(trimmed), decode(args.original)
    edl = align(features(t_audio), features(o_audio))
    orig_len = len(o_audio) / SR

    print(f"\ntrimmed {len(t_audio) / SR:.2f} s  ->  original {orig_len:.2f} s\n")
    print(f"{'piece':>5} {'trimmed':>17} {'original':>19} {'cut after':>10} {'match':>6}")
    for i, p in enumerate(edl):
        o_end = p["orig_start"] + p["trim_end"] - p["trim_start"]
        gap = (edl[i + 1]["orig_start"] - o_end) if i + 1 < len(edl) else None
        print(f"{i + 1:>5} {p['trim_start']:7.2f}-{p['trim_end']:7.2f} s {p['orig_start']:8.2f}-{o_end:8.2f} s "
              f"{(f'{gap:.2f} s' if gap is not None else '-'):>10} {p['score']:6.2f}")
    if any(p["score"] < 0.8 for p in edl):
        print("\nwarning: a piece matched weakly (< 0.80); check it by ear before using it")

    if args.write:
        orig_path = args.original.resolve()
        rel = orig_path.relative_to(CLIPS) if orig_path.is_relative_to(CLIPS) else orig_path.name
        out = {"original": {"path": str(rel), "url": args.url, "duration": round(orig_len, 2)},  # relative to data/clips
               "window": {"start": edl[0]["orig_start"],
                          "end": round(edl[-1]["orig_start"] + edl[-1]["trim_end"] - edl[-1]["trim_start"], 2)},
               "edl": edl}
        (clip_dir / "source.json").write_text(json.dumps(out, indent=2) + "\n")
        print(f"\nwrote {clip_dir / 'source.json'}")


if __name__ == "__main__":
    main()
