"""Local provenance checks, with no model calls or invented evidence."""
from collections.abc import Mapping
from app.models import Annotation, Evidence, Segment


def normalize_source(text: str) -> str:
    return " ".join(text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})).split())


def evidence_span(evidence: Evidence, source_text: str) -> bool:
    quote = normalize_source(evidence.quote)
    return bool(quote) and quote in normalize_source(source_text)


def transcript_span(seg: Segment, text: str, start: int, end: int) -> bool:
    return bool(text) and 0 <= start < end <= len(seg.text) and seg.text[start:end] == text


def stt_safe(seg: Segment, span: str) -> bool:
    # The shared Segment has no word-aligned confidence or uncertainty type.
    # Keep any uncertain transcript candidate-only, including spelled-out numbers
    # and negation forms that an English keyword list could miss.
    return seg.stt_confidence >= .85 and not seg.low_conf_words



def verify_annotation(a: Annotation, seg: Segment, sources: Mapping[str, str]) -> bool:
    return (
        seg.role != "moderator" and a.speaker_id == seg.speaker_id and a.segment_id == seg.segment_id
        and transcript_span(seg, a.span_text, a.span_char_start, a.span_char_end)
        and bool(a.explanation.strip()) and bool(a.evidence)
        and all(e.source_id in sources and evidence_span(e, sources[e.source_id]) for e in a.evidence)
    )
