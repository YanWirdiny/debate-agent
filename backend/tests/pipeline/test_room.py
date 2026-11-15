"""Live room mode: browser mic -> Deepgram (diarized) -> voices named by LiveSpeakers -> segments.

The fake Deepgram emits scripted words, each with a `speaker` (voice) index, once enough audio has
arrived (stream time = bytes received; the runner sends 1 s of warm-up silence first).
"""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app import ingest
from app.eventlog import MemorySink, verify_chain
from app.hubs import Hub, RecordingClient
from app.main import SessionManager
from app.models import Clip, Participant, Word
from app.segmenter import Segmenter
from app.session import LANE_BUDGET_S, ROOM_LANE_TIMEOUT_S, SessionRunner

SR = 16000
BYTES_PER_S = SR * 2

# (stream t_start, t_end, text, voice)
SCRIPT = [
    (1.2, 1.5, "Unemployment", 0), (1.5, 1.7, "is", 0), (1.7, 2.0, "zero", 0), (2.0, 2.4, "percent.", 0),
    (2.6, 2.9, "That", 1), (2.9, 3.1, "is", 1), (3.1, 3.4, "false.", 1),
    (3.6, 3.8, "I", 1), (3.8, 4.0, "agree", 1),  # no period: flushed when the room stops
]


class FakeDeepgram:
    def __init__(self) -> None:
        self.query = ""

    def process_request(self, connection, request):
        self.query = request.path
        return None

    async def handler(self, ws):
        received = 0
        pending = list(SCRIPT)
        async for msg in ws:
            if isinstance(msg, bytes):
                received += len(msg)
                t = received / BYTES_PER_S
                ready = [w for w in pending if w[1] <= t]
                pending = [w for w in pending if w[1] > t]
                if ready:
                    words = [{"word": x.strip(".").lower(), "punctuated_word": x, "start": a, "end": b,
                              "confidence": 0.95, "speaker": v} for a, b, x, v in ready]
                    await ws.send(json.dumps({"type": "Results", "is_final": True, "channel": {"alternatives": [{"words": words}]}}))
            elif json.loads(msg)["type"] == "CloseStream":
                await ws.send(json.dumps({"type": "Metadata"}))
                await ws.close()
                return


@pytest.fixture
async def fake_dg():
    fake = FakeDeepgram()
    async with serve(fake.handler, "127.0.0.1", 0, process_request=fake.process_request) as server:
        yield fake, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/listen"


PEOPLE = [Participant(speaker_id="alice", name="Alice"), Participant(speaker_id="bob", name="Bob")]


def room_runner(url, sink, topic=None):
    op, au = Hub("operator"), Hub("audience")
    opc = RecordingClient()
    op.add(opc)
    clip = Clip(clip_id="room", title="Alice vs Bob", video_path="", audio_path="")
    r = SessionRunner("room-t", "room", clip=clip, operator=op, audience=au, source="mic", participants=PEOPLE,
                      deepgram_key="k", deepgram_url=url, sink=sink, lane_timeout=ROOM_LANE_TIMEOUT_S, topic=topic)
    return r, opc


def msgs(client, type_):
    return [m for m in (json.loads(x) for x in client.messages) if m["type"] == type_]


async def feed(runner, seconds: float) -> None:
    chunk = ingest.silence(0.1)
    for _ in range(round(seconds * 10)):
        runner.feed_mic(chunk)
        await asyncio.sleep(0)


async def until(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.02)


# --- units -----------------------------------------------------------------------


def test_dg_words_carry_the_voice():
    msg = {"type": "Results", "is_final": True, "channel": {"alternatives": [{"words": [
        {"word": "a", "start": 0, "end": 0.2, "confidence": 0.9, "speaker": 1},
        {"word": "b", "start": 0.2, "end": 0.4, "confidence": 0.9}]}]}}
    words = ingest.dg_final_words(msg)
    assert [w.speaker_id for w in words] == ["voice1", None]


def w(text, voice):
    return Word(text=text, t_start=0, t_end=0.1, confidence=0.9, speaker_id=f"voice{voice}")


def test_live_speakers_auto_assign_override_and_assign():
    ls = ingest.LiveSpeakers([("alice", "speaker"), ("bob", "speaker"), ("mod", "moderator")])
    assert ls.tag(w("x", 3)) == ("mod", "moderator")  # a debate opens with the moderator
    assert ls.tag(w("x", 5)) == ("alice", "speaker")  # then the debaters, in order
    assert ls.tag(w("x", 0)) == ("bob", "speaker")
    assert ls.tag(w("x", 7)) == ("bob", "speaker")  # everyone taken: last debater, until fixed
    ls.assign(7, "mod")
    assert ls.tag(w("x", 7)) == ("mod", "moderator")
    ls.set_override("alice")
    assert ls.tag(w("x", 0)) == ("alice", "speaker")  # the operator's key beats the voice map
    ls.set_override(None)
    assert ls.tag(w("x", 0)) == ("bob", "speaker")
    with pytest.raises(ValueError):
        ls.assign(1, "nobody")


def test_segmenter_splits_when_the_voice_changes():
    seg = Segmenter("s", ingest.LiveSpeakers([("alice", "speaker"), ("bob", "speaker")]))
    out = seg.on_final([w("Yes", 0), w("no", 1)])
    assert [(s.speaker_id, s.text) for s in out] == [("alice", "Yes")]
    assert [s.speaker_id for s in seg.flush()] == ["bob"]


# --- end to end ------------------------------------------------------------------


async def test_room_end_to_end(fake_dg):
    fake, url = fake_dg
    sink = MemorySink()
    runner, opc = room_runner(url, sink)
    await runner.warm_up()
    assert "diarize=true" in fake.query
    assert runner.speaker_ids() == ["alice", "bob"]
    assert runner.ctx.participants["bob"].name == "Bob"  # no dossier, still named in prompts
    await runner.start(lead_s=0.0)
    budgets = []
    orig = runner.intel.run_fallacy

    async def spy(seg, view, emit):
        budgets.append(LANE_BUDGET_S.get())
        return await orig(seg, view, emit)
    runner.intel.run_fallacy = spy

    await feed(runner, 2.6)  # stream 1.0 + 2.6 = 3.6 s: both first sentences
    await until(lambda: len(msgs(opc, "segment")) >= 2)
    segs = [m["data"] for m in msgs(opc, "segment")]
    assert [(s["speaker_id"], s["text"]) for s in segs] == [("alice", "Unemployment is zero percent."), ("bob", "That is false.")]
    voices = msgs(opc, "voices")
    assert voices and voices[-1]["voices"] == {"0": "alice", "1": "bob"}

    # Operator presses 1: whatever is heard now is Alice, whichever voice Deepgram hears.
    runner.live_speakers.set_override("alice")
    await runner.announce_voices()
    assert msgs(opc, "voices")[-1]["override"] == "alice"
    await feed(runner, 0.6)
    await asyncio.sleep(0.2)
    await runner.stop()  # flushes "I agree" (no period), then ends

    segs = [m["data"] for m in msgs(opc, "segment")]
    assert (segs[-1]["speaker_id"], segs[-1]["text"]) == ("alice", "I agree")
    ended = msgs(opc, "ended")
    assert len(ended) == 1 and ended[0]["session_id"] == "room-t"
    assert budgets and all(b == ROOM_LANE_TIMEOUT_S for b in budgets)  # lanes ran on the room budget
    assert verify_chain(sink.events) and [e.type for e in sink.events][-1] == "session_end"


async def test_mic_audio_ignored_before_start_and_after_stop(fake_dg):
    _, url = fake_dg
    runner, _ = room_runner(url, MemorySink())
    await runner.warm_up()
    runner.feed_mic(b"\x00\x00" * 1600)
    assert runner.mic_q.empty()  # not started yet
    await runner.start(lead_s=0.0)
    await runner.stop()
    runner.feed_mic(b"\x00\x00" * 1600)
    assert runner.mic_q.empty()


async def test_manager_room_needs_a_deepgram_key():
    mgr = SessionManager(source="replay", intel="fake", deepgram_key="")
    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        await mgr.load_room(PEOPLE)
    with pytest.raises(ValueError, match="no live room"):
        mgr.room()


async def test_typed_topic_seeds_context_and_prefetches(fake_dg):
    _, url = fake_dg
    sink = MemorySink()
    runner, opc = room_runner(url, sink, topic="  Taxes and the deficit  ")
    await runner.warm_up()
    asked = []

    async def prefetch(job):
        asked.append(job.question.text)
        return "general", []
    runner.intel.prefetch = prefetch
    await runner.start(lead_s=0.0)
    await until(lambda: asked)
    assert asked == ["Taxes and the deficit"]  # the typed topic is the prefetch question (Gemini + Search)
    assert runner.ctx.rolling_summary == "Debate topic: Taxes and the deficit."  # every lane sees it
    assert runner.voices_message().topic == "Taxes and the deficit"
    await runner.stop()
    start = json.loads(sink.events[0].payload_json)
    assert start["topic"] == "Taxes and the deficit"  # on the record
