"""Mock stream (P1): play the fixture clip through the real pipeline with fake lanes and print
what the operator and audience views receive. No API keys, no network.

    python scripts/mock_stream.py [--clip fixture] [--speed 4]

For the frontend, run the real server instead and send {"type":"load","clip_id":"fixture"} then
{"type":"start","clip_id":"fixture"} on /ws/operator:

    uvicorn app.main:app --workers 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eventlog import MemorySink, verify_chain  # noqa: E402
from app.hubs import Hub  # noqa: E402
from app.session import SessionRunner  # noqa: E402


class PrintClient:
    def __init__(self, view: str, show_metrics: bool = False) -> None:
        self.view = view
        self.show_metrics = show_metrics

    async def send_text(self, data: str) -> None:
        msg = json.loads(data)
        if msg["type"] == "metrics" and not self.show_metrics:
            return
        d = msg.get("data") or {}
        if msg["type"] == "segment":
            line = f"{d['speaker_id']:>10}: {d['text']}"
        elif msg["type"] in ("annotation", "annotation_update"):
            line = f"{'':>10}  [{d['category']}] {d['status']:<9} v{d['version']} {d['span_text']!r} ({d['latency_ms']} ms)"
        else:
            line = json.dumps({k: v for k, v in msg.items() if k != "type"})
        print(f"{self.view:<8} {msg['type']:<17} {line}")


async def main(clip_id: str, speed: float, show_metrics: bool = False) -> None:
    operator, audience = Hub("operator"), Hub("audience")
    operator.add(PrintClient("operator", show_metrics))
    audience.add(PrintClient("audience"))
    sink = MemorySink()
    runner = SessionRunner("mock", clip_id, operator=operator, audience=audience, sink=sink, speed=speed)
    await runner.warm_up()
    await runner.start()
    await runner.wait()
    print(f"\n{len(sink.events)} events, hash chain valid: {verify_chain(sink.events)}")
    print(f"metrics: {runner.metrics.snapshot().model_dump()}  p95 bank: {runner.metrics.p95_bank_ms:.0f} ms")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--clip", default="fixture")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--metrics", action="store_true", help="print the 1 s metrics ticks too")
    a = p.parse_args()
    asyncio.run(main(a.clip, a.speed, a.metrics))
