"""scripts/align_clip.py recovers a known cut list from audio alone."""
import numpy as np

from scripts import align_clip as ac


def speechlike(rng, seconds):
    # Bursts of band-limited noise with pauses: varied spectra, like speech.
    n = int(seconds * ac.SR)
    x = np.convolve(rng.standard_normal(n), rng.standard_normal(32), mode="same")
    env = np.repeat(rng.random(int(seconds * 5)) > 0.3, ac.SR // 5)[:n]
    return (x * np.pad(env, (0, n - len(env)))).astype(np.float32)


def test_recovers_pieces_and_cuts():
    rng = np.random.default_rng(1)
    trim = speechlike(rng, 30)
    pieces = [(0, 8), (8, 19), (19, 30)]
    gaps = [5, 12]
    parts = [speechlike(rng, 40)]
    for i, (a, b) in enumerate(pieces):
        parts.append(trim[a * ac.SR:b * ac.SR] + 0.01 * rng.standard_normal((b - a) * ac.SR).astype(np.float32))
        if i < len(gaps):
            parts.append(speechlike(rng, gaps[i]))
    parts.append(speechlike(rng, 10))
    edl = ac.align(ac.features(trim), ac.features(np.concatenate(parts)))
    assert len(edl) == 3
    expected_orig = [40, 40 + 8 + 5, 40 + 8 + 5 + 11 + 12]
    for p, (a, b), o in zip(edl, pieces, expected_orig):
        assert abs(p["trim_start"] - a) <= 0.05 and abs(p["orig_start"] - o) <= 0.05 and p["score"] > 0.9
