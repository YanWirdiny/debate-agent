"""P1 Phase 1 slice 2: audio pump, Deepgram streaming (against a local fake server), reconnect and
fallback, label_clip.py. No network, no API key."""

import asyncio
import json
import time
import wave
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from app import ingest
from app.eventlog import MemorySink, verify_chain
from app.hubs import Hub, RecordingClient
from app.models import Clip, TimelineEntry
from app.session import SessionRunner
from scripts import label_clip

SR = ingest.SAMPLE_RATE
BYTES_PER_S = 2 * SR


def write_wav(path: Path, seconds: float, rate: int = SR, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds) * channels)
    return path


class RecordingDG:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.chunks.append(data)


# --- audio pump -------------------------------------------------------------------


async def test_audio_pump_chunks_timing_and_resume(tmp_path):
    clip = Clip(clip_id="c", title="c", video_path="", audio_path=str(write_wav(tmp_path / "a.wav", 0.35)))
    dg = RecordingDG()
    pump = ingest.AudioPump(speed=10)
    t0 = asyncio.get_running_loop().time()
    await pump.run(clip, dg, time.time())
    elapsed = asyncio.get_running_loop().time() - t0
    assert [len(c) for c in dg.chunks] == [3200, 3200, 3200, 1600]  # 100 ms chunks, last one partial
    assert pump.done and pump.position == pytest.approx(0.35)
    assert 0.03 <= elapsed < 0.2  # paced at 10x (0.035 s), not dumped instantly

    dg2 = RecordingDG()
    resumed = ingest.AudioPump(speed=1000, start_s=0.2)
    await resumed.run(clip, dg2, time.time())
    assert sum(len(c) for c in dg2.chunks) == int(0.15 * BYTES_PER_S)


def test_open_pcm_rejects_wrong_format(tmp_path):
    with pytest.raises(ValueError, match="16 kHz mono"):
        ingest.open_pcm(write_wav(tmp_path / "s.wav", 0.1, rate=44100, channels=2))
    with pytest.raises(FileNotFoundError, match="ffmpeg"):
        ingest.open_pcm(tmp_path / "missing.wav")


def test_dg_final_words_offset_and_interims():
    w = {"word": "six", "punctuated_word": "6%.", "start": 1.5, "end": 1.9, "confidence": 0.97}
    final = {"type": "Results", "is_final": True, "channel": {"alternatives": [{"words": [w]}]}}
    [word] = ingest.dg_final_words(final, offset=-1.0)
    assert (word.text, word.t_start, word.t_end, word.confidence) == ("6%.", 0.5, 0.9, 0.97)
    assert ingest.dg_final_words({**final, "is_final": False}) is None
    assert ingest.dg_final_words({"type": "UtteranceEnd", "last_word_end": 2.0}) is None


# --- fake Deepgram server -----------------------------------------------------------

SCRIPT = [  # (clip t_start, t_end, text): moderator 0-2, speaker_a 2-4, speaker_b 4-6
    (0.2, 0.6, "What"), (0.6, 1.0, "is"), (1.0, 1.4, "the"), (1.4, 1.8, "rate?"),
    (2.2, 2.6, "The"), (2.6, 3.0, "rate"), (3.0, 3.4, "is"), (3.4, 3.8, "6%."),
    (4.2, 4.6, "No,"), (4.6, 5.0, "it"), (5.0, 5.4, "is"), (5.4, 5.8, "4%"),  # no period: UtteranceEnd ends it
]
TIMELINE = [
    TimelineEntry(t_start=0, t_end=2, speaker_id="moderator", role="moderator"),
    TimelineEntry(t_start=2, t_end=4, speaker_id="speaker_a", role="speaker"),
    TimelineEntry(t_start=4, t_end=6, speaker_id="speaker_b", role="speaker"),
]


class FakeDeepgram:
    """Emits SCRIPT words as final Results once their audio has arrived, in stream time.
    Stream time = clip time - pump start + warm-up silence, like the real service."""

    def __init__(self, runner_ref: list, drop_at: list[float] | None = None) -> None:
        self.runner_ref = runner_ref
        self.drop_at = list(drop_at or [])
        self.connections = 0
        self.auth: list[str] = []
        self.query = ""
        self.keepalives = 0

    def process_request(self, connection, request):
        self.auth.append(request.headers.get("Authorization", ""))
        self.query = request.path
        if request.headers.get("Authorization") != "Token test-key":
            return connection.respond(401, "bad key\n")
        return None

    async def handler(self, ws):
        self.connections += 1
        runner = self.runner_ref[0]
        start = runner.pump.start_s if runner and runner.pump else 0.0
        received = 0
        pending = [w for w in SCRIPT if w[0] >= start]
        drop = self.drop_at.pop(0) if self.drop_at else None

        def stream_t(clip_t: float) -> float:
            return clip_t - start + 1.0  # 1 s of warm-up silence precedes the pump on every connection

        async def emit(upto_clip_t: float) -> None:
            nonlocal pending
            ready = [w for w in pending if w[1] <= upto_clip_t]
            pending = [w for w in pending if w[1] > upto_clip_t]
            if not ready:
                return
            words = [{"word": t.strip("?.,%").lower(), "punctuated_word": t, "start": stream_t(a), "end": stream_t(b),
                      "confidence": 0.95} for a, b, t in ready]
            await ws.send(json.dumps({"type": "Results", "is_final": False, "channel": {"alternatives": [{"words": words}]}}))
            await ws.send(json.dumps({"type": "Results", "is_final": True, "channel": {"alternatives": [{"words": words}]}}))

        async for msg in ws:
            if isinstance(msg, bytes):
                received += len(msg)
                clip_t = start + received / BYTES_PER_S - 1.0
                if drop is not None and clip_t >= drop:
                    await ws.close(1011, "simulated drop")
                    return
                await emit(clip_t)
                if clip_t >= 5.9 and not pending:
                    await ws.send(json.dumps({"type": "UtteranceEnd", "last_word_end": stream_t(5.8)}))
            elif json.loads(msg)["type"] == "KeepAlive":
                self.keepalives += 1
            elif json.loads(msg)["type"] == "CloseStream":
                await ws.send(json.dumps({"type": "Metadata"}))
                await ws.close()
                return


@pytest.fixture
async def fake_dg():
    ref: list = [None]
    fake = FakeDeepgram(ref)
    async with serve(fake.handler, "127.0.0.1", 0, process_request=fake.process_request) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, ref, f"ws://127.0.0.1:{port}/v1/listen"


def live_runner(tmp_path, url, ref, key="test-key", transcript=None, sink=None):
    clip = Clip(clip_id="live", title="live", video_path="", audio_path=str(write_wav(tmp_path / "a.wav", 6.0)),
                timeline=TIMELINE, transcript_path=transcript)
    op, au = Hub("operator"), Hub("audience")
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc), au.add(auc)
    r = SessionRunner("live-s", "live", clip=clip, operator=op, audience=au, source="deepgram",
                      deepgram_key=key, deepgram_url=url, speed=20, sink=sink or MemorySink())
    ref[0] = r
    return r, opc, auc


def msgs(client, type_=None):
    out = [json.loads(m) for m in client.messages]
    return [m for m in out if type_ is None or m["type"] == type_]


async def test_deepgram_live_end_to_end(tmp_path, fake_dg):
    fake, ref, url = fake_dg
    sink = MemorySink()
    runner, opc, auc = live_runner(tmp_path, url, ref, sink=sink)
    await runner.warm_up()
    assert runner.dg is not None and runner.dg.sent_s == pytest.approx(1.0)  # warm-up silence
    await runner.start(lead_s=0.05)
    await asyncio.wait_for(runner.wait(), 10)

    segs = [m["data"] for m in msgs(opc, "segment")]
    assert [(s["speaker_id"], s["role"], s["text"]) for s in segs] == [
        ("moderator", "moderator", "What is the rate?"),
        ("speaker_a", "speaker", "The rate is 6%."),
        ("speaker_b", "speaker", "No, it is 4%"),  # flushed by UtteranceEnd
    ]
    assert segs[1]["t_start"] == pytest.approx(2.2) and segs[1]["t_end"] == pytest.approx(3.8)  # clip time
    assert fake.auth == ["Token test-key"] and "encoding=linear16" in fake.query and "sample_rate=16000" in fake.query
    assert not [m for m in msgs(opc, "banner")]  # no replay banner in live mode
    anns = [m["data"] for m in msgs(opc, "annotation")]
    assert {a["segment_id"] for a in anns} <= {s["segment_id"] for s in segs[1:]}  # moderator never flagged
    assert verify_chain(sink.events) and [e.type for e in sink.events].count("segment") == 3


async def test_deepgram_drop_reconnects_and_resumes(tmp_path, fake_dg):
    fake, ref, url = fake_dg
    fake.drop_at = [3.0]
    runner, opc, _ = live_runner(tmp_path, url, ref)
    await runner.warm_up()
    await runner.start(lead_s=0.05)
    await asyncio.wait_for(runner.wait(), 10)

    assert fake.connections == 2 and runner.reconnects == 1
    banners = [m["text"] for m in msgs(opc, "banner")]
    assert any("reconnecting (1/3)" in b for b in banners)
    segs = [m["data"] for m in msgs(opc, "segment")]
    assert segs[0]["text"] == "What is the rate?"
    assert segs[-1] == {**segs[-1], "speaker_id": "speaker_b", "text": "No, it is 4%"}  # resumed with clip times
    assert segs[-1]["t_start"] == pytest.approx(4.2)


async def test_deepgram_lost_falls_back_to_transcript(tmp_path, fake_dg):
    fake, ref, url = fake_dg
    fake.drop_at = [3.0, 3.0, 3.0, 3.0, 3.0]  # every reconnect drops too
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(
        json.dumps({"segment_id": f"live:{i}", "session_id": "live", "speaker_id": spk, "role": role, "text": text,
                    "t_start": a, "t_end": b, "stt_confidence": 0.9, "low_conf_words": []}) + "\n"
        for i, (spk, role, text, a, b) in enumerate([
            ("moderator", "moderator", "What is the rate?", 0.2, 1.8),
            ("speaker_a", "speaker", "The rate is 6%.", 2.2, 3.8),
            ("speaker_b", "speaker", "No, it is 4%.", 4.2, 5.8),
        ])))
    runner, opc, _ = live_runner(tmp_path, url, ref, transcript=str(transcript))
    await runner.warm_up()
    await runner.start(lead_s=0.05)
    await asyncio.wait_for(runner.wait(), 10)

    banners = [m["text"] for m in msgs(opc, "banner")]
    assert any("switched to pre-made transcript" in b for b in banners)
    assert [m["data"]["text"] for m in msgs(opc, "segment")][-1] == "No, it is 4%."


async def test_deepgram_bad_key_fails_warm_up(tmp_path, fake_dg):
    _, ref, url = fake_dg
    runner, _, _ = live_runner(tmp_path, url, ref, key="wrong")
    with pytest.raises(ConnectionError, match="DEEPGRAM_API_KEY"):
        await runner.warm_up()


async def test_deepgram_keepalive_while_waiting_for_start(fake_dg):
    fake, _, url = fake_dg
    dg = ingest.DeepgramLive("test-key", url=url, keepalive_s=0.1)
    await dg.connect()
    await asyncio.sleep(0.5)
    await dg.close()
    assert fake.keepalives >= 2


# --- label_clip --------------------------------------------------------------------


def dg_word(t, a, b, spk):
    return {"word": t.lower().strip("?.,"), "punctuated_word": t, "start": a, "end": b, "confidence": 0.9, "speaker": spk}


DIARIZED = [
    dg_word("Welcome.", 0.0, 0.5, 0), dg_word("First", 0.6, 0.9, 0), dg_word("question?", 0.9, 1.4, 0),
    dg_word("Taxes", 2.0, 2.4, 1), dg_word("went", 2.4, 2.7, 1), dg_word("Yeah", 2.75, 2.9, 2),  # blip
    dg_word("up.", 3.0, 3.3, 1), dg_word("They", 4.0, 4.3, 2), dg_word("fell.", 4.3, 4.8, 2),
]


def test_label_clip_map_turns_and_transcript():
    mapping = label_clip.parse_map(["0=moderator:moderator", "1=speaker_a"])
    assert mapping == {0: ("moderator", "moderator"), 1: ("speaker_a", "speaker")}
    with pytest.raises(ValueError):
        label_clip.parse_map(["x=bob"])
    tl = label_clip.turns_from_words(DIARIZED, mapping)
    assert [(e.speaker_id, e.role, e.t_start, e.t_end) for e in tl] == [
        ("moderator", "moderator", 0.0, 1.4), ("speaker_a", "speaker", 2.0, 3.3), ("dg2", "speaker", 4.0, 4.8)]
    segs = label_clip.transcript_from_words("c", DIARIZED, tl)
    assert [(s.speaker_id, s.text) for s in segs] == [
        ("moderator", "Welcome."), ("moderator", "First question?"), ("speaker_a", "Taxes went Yeah up."), ("dg2", "They fell.")]
    assert ingest.validate_timeline(tl) == []
    assert ingest.validate_timeline(tl[1:]) == ["no moderator entry"]


def test_label_clip_draft_build_show_from_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest, "CLIPS_DIR", tmp_path)
    folder = tmp_path / "c1"
    folder.mkdir()
    write_wav(folder / "audio.wav", 5.0)
    (folder / label_clip.CACHE_NAME).write_text(json.dumps(
        {"results": {"channels": [{"alternatives": [{"words": DIARIZED}]}]}}))  # no network: cached response

    label_clip.main(["draft", "c1", "--title", "Test clip"])  # first pass, unmapped
    assert "unmapped Deepgram speakers" in capsys.readouterr().out
    label_clip.main(["draft", "c1", "--map", "0=moderator:moderator", "--map", "1=speaker_a", "--map", "2=speaker_b"])
    clip = ingest.load_clip("c1", tmp_path)
    assert clip.title == "Test clip" and clip.audio_path == str(folder / "audio.wav") and clip.duration == 4.8
    assert [e.speaker_id for e in clip.timeline] == ["moderator", "speaker_a", "speaker_b"]  # from timeline.json
    with pytest.raises(SystemExit, match="hand-fixed"):
        label_clip.main(["draft", "c1"])  # mapped timeline is protected

    # hand fix: rename the third speaker, then rebuild the transcript from the fixed timeline
    fixed = json.loads((folder / "timeline.json").read_text())
    fixed[2]["speaker_id"] = "speaker_c"
    (folder / "timeline.json").write_text(json.dumps(fixed))
    label_clip.main(["build", "c1"])
    rows = [json.loads(l) for l in (folder / "transcript.jsonl").read_text().splitlines()]
    assert rows[-1]["speaker_id"] == "speaker_c" and rows[-1]["text"] == "They fell."
    capsys.readouterr()
    label_clip.main(["show", "c1"])
    out = capsys.readouterr().out
    assert "speaker_c" in out and "First question?" in out and "MOD" in out
    assert ingest.load_transcript_words(folder / "transcript.jsonl")  # usable by replay mode
