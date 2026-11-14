"""Segmenter (P1): Deepgram final words -> sentences tagged with speaker and role.

Sentence end = terminal punctuation, Deepgram utterance_end (`flush()`), or a speaker change.
A speaker change mid-buffer always forces a split: never a cross-speaker sentence.
"""
from __future__ import annotations

from app.ingest import TimelineLookup
from app.models import Segment, Word, segment_id

LOW_CONF = 0.85
MAX_WORDS = 60  # force a split on very long unpunctuated runs so lanes are not starved
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "vs.", "u.s.", "u.k.", "e.g.", "i.e.", "etc."}
_TRAILING = "\"')]}”’"


def _ends_sentence(token: str) -> bool:
    t = token.rstrip(_TRAILING)
    if not t or t[-1] not in ".?!":
        return False
    return t.lower() not in _ABBREVIATIONS


class Segmenter:
    def __init__(self, session_id: str, timeline: TimelineLookup | None = None) -> None:
        self.session_id = session_id
        self.timeline = timeline
        self._buf: list[Word] = []
        self._seq = 0

    def on_final(self, words: list[Word]) -> list[Segment]:
        out: list[Segment] = []
        for w in words:
            w = self._tag(w)
            if self._buf and self._buf[-1].speaker_id != w.speaker_id:
                out.append(self._emit())
            self._buf.append(w)
            if _ends_sentence(w.text) or len(self._buf) >= MAX_WORDS:
                out.append(self._emit())
        return out

    def flush(self) -> list[Segment]:
        """Deepgram utterance_end, or end of stream."""
        return [self._emit()] if self._buf else []

    def _tag(self, w: Word) -> Word:
        if self.timeline is not None:
            speaker_id, role = self.timeline.who((w.t_start + w.t_end) / 2)
            return w.model_copy(update={"speaker_id": speaker_id, "role": role})
        if w.speaker_id is None or w.role is None:
            raise ValueError("word has no speaker and the segmenter has no timeline")
        return w

    def _emit(self) -> Segment:
        words, self._buf = self._buf, []
        seg = Segment(
            segment_id=segment_id(self.session_id, self._seq),
            session_id=self.session_id,
            speaker_id=words[0].speaker_id or "",
            role=words[0].role or "speaker",
            text=" ".join(w.text for w in words),
            t_start=words[0].t_start,
            t_end=words[-1].t_end,
            stt_confidence=round(sum(w.confidence for w in words) / len(words), 4),
            low_conf_words=[w.text.strip(".,!?;:\"'()") for w in words if w.confidence < LOW_CONF],
        )
        self._seq += 1
        return seg
