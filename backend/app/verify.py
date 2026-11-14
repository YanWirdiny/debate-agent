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


_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion",
    "trillion", "percent", "half", "double", "triple", "first", "second", "third", "dozen",
}
_NEGATIONS = {"not", "no", "never", "none", "nobody", "nothing", "neither", "nor", "without", "cannot"}


def _risky(word: str) -> bool:
    w = word.lower().replace("’", "'").strip(".,!?;:\"'()$%")
    return w in _NEGATIONS or w.endswith("n't") or w in _NUMBER_WORDS or any(c.isdigit() for c in w)


def stt_safe(seg: Segment, span: str) -> bool:
    """STT guard (CONTEXT.md 5.3 step 5). Unsafe -> max status candidate. Unsafe when the segment
    confidence is < .85, or a low-confidence word is a number/negation anywhere in the sentence
    (it may sit outside the span and still flip its meaning), or any low-confidence word is
    inside the span itself. A low-confidence filler word elsewhere ("ago") does not block."""
    if seg.stt_confidence < .85:
        return False
    span_words = {w.lower().strip(".,!?;:\"'()") for w in span.split()}
    return not any(_risky(w) or w.lower().strip(".,!?;:\"'()") in span_words for w in seg.low_conf_words)


def verify_annotation(a: Annotation, seg: Segment, sources: Mapping[str, str]) -> bool:
    return (
        seg.role != "moderator" and a.speaker_id == seg.speaker_id and a.segment_id == seg.segment_id
        and transcript_span(seg, a.span_text, a.span_char_start, a.span_char_end)
        and bool(a.explanation.strip()) and bool(a.evidence)
        and all(e.source_id in sources and evidence_span(e, sources[e.source_id]) for e in a.evidence)
    )
