"""scripts/align_clip.py recovers a known cut list from audio alone."""
import subprocess
from pathlib import Path

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


def test_cut_captions_names_speakers_skips_pieces_and_applies_overrides():
    edl = [{"trim_start": 0, "trim_end": 2, "orig_start": 10}, {"trim_start": 2, "trim_end": 4, "orig_start": 20}]
    timeline = [{"t_start": 0, "t_end": 2, "speaker_id": "moderator", "role": "moderator"},
                {"t_start": 2, "t_end": 4, "speaker_id": "harris", "role": "speaker"}]
    segments = [{"t_start": 0, "t_end": 2, "text": "Your question?"}, {"t_start": 2, "t_end": 4, "text": "My plan plan works."}]

    def w(t, spk, text):
        return {"start": t, "end": t + 0.3, "speaker": spk, "word": text.lower(), "punctuated_word": text}
    words = [w(10.2, 0, "Your"), w(10.6, 0, "question?"),            # piece 1: speaker 0 = moderator
             w(13.0, 1, "Thanks."), w(14.0, 1, "So"), w(14.4, 1, "anyway"),  # cut: speaker 1 (named in piece 2)
             w(19.8, 1, "big"),                                       # straddles piece 2 start, not in transcript: kept
             w(20.2, 1, "My"), w(20.6, 1, "plan"), w(21.0, 1, "plan."),       # piece 2: speaker 1 = harris
             w(21.85, 1, "works."),                                   # straddles piece 2 end (22 s), duplicate: skipped
             w(25.0, 1, "Over"), w(25.4, 1, "to"), w(25.8, 1, "you.")]
    lines = ac.cut_captions(words, edl, timeline, {"start": 10, "end": 30}, [(24.9, 25.5, "moderator", "moderator")], segments)
    assert [(l["speaker_id"], l["text"]) for l in lines] == [
        ("harris", "Thanks."), ("harris", "So anyway"), ("harris", "big"),  # a 5 s pause starts a new line
        ("moderator", "Over"), ("harris", "to you.")]                    # override covers "Over" only
    assert lines[0]["words"][0] == {"text": "Thanks.", "t_start": 13.0, "t_end": 13.3}


def test_ffmpeg_paths_are_absolute(tmp_path, monkeypatch):
    """File paths passed to ffmpeg are resolved, so a name starting with '-' is not an option."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        dest = args[-1]
        if dest != "-":
            Path(dest).write_bytes(b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"\x00\x00\x00\x00")

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    dashed = Path("-dashed.wav")
    assert ac.decode(dashed).shape == (1,)
    decode_input = calls[0][calls[0].index("-i") + 1]
    assert decode_input == str(dashed.resolve()) and decode_input.startswith("/")

    class _Key:
        def get_secret_value(self):
            return "k"

    class _Settings:
        deepgram_api_key = _Key()

    monkeypatch.setattr("app.db.lenient_settings", lambda: _Settings())
    monkeypatch.setattr(
        "scripts.label_clip.transcribe",
        lambda wav, key: {"results": {"channels": [{"alternatives": [{"words": [{"start": 0.2, "end": 0.5, "word": "hi"}]}]}]}},
    )
    original = Path("-original.mp4")
    cache = tmp_path / "window.json"
    words = ac.transcribe_window(original, 3.0, 8.0, cache)
    assert words == [{"start": 3.2, "end": 3.5, "word": "hi"}]
    args = calls[1]
    original_arg = args[args.index("-i") + 1]
    wav_arg = args[-1]
    assert original_arg == str(original.resolve()) and original_arg.startswith("/")
    assert wav_arg == str(cache.with_suffix(".wav").resolve()) and wav_arg.startswith("/")
