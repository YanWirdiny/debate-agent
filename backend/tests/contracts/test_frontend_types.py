import inspect
import re
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from app import models as m

TYPES_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "types.ts"
WIRE_MODELS = (
    "Segment",
    "Evidence",
    "Annotation",
    "MetricsSnapshot",
    "SegmentMessage",
    "AnnotationMessage",
    "AnnotationUpdateMessage",
    "MetricsMessage",
    "ReadyMessage",
    "StartedMessage",
    "EndedMessage",
    "RemovalMessage",
    "BannerMessage",
    "PendingMessage",
    "PartialWord",
    "PartialMessage",
    "LoadMessage",
    "StartMessage",
    "KillMessage",
)
ALIASES = {"ServerMessage": m.ServerMessage, "ClientMessage": m.ClientMessage}


def interfaces(text: str) -> dict[str, set[str]]:
    found = {}
    for name, body in re.findall(r"export interface (\w+)\s*\{([^}]*)\}", text):
        found[name] = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:", body, re.M))
    return found


def alias_members(text: str, name: str) -> set[str]:
    match = re.search(rf"export type {name}\b\s*=\s*(.*?);", text, re.S)
    assert match, f"frontend/src/types.ts is missing export type {name}"
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", match.group(1)))


def union_members(alias: object) -> set[str]:
    union = get_args(alias)[0]
    return {cls.__name__ for cls in get_args(union)}


def test_types_ts_matches_models():
    text = TYPES_TS.read_text()
    models = {
        name: cls
        for name, cls in vars(m).items()
        if inspect.isclass(cls) and issubclass(cls, BaseModel) and cls.__module__ == m.__name__
    }
    parsed = interfaces(text)
    assert set(parsed) == set(WIRE_MODELS)
    for name in WIRE_MODELS:
        assert parsed[name] == set(models[name].model_fields), name
    for name, alias in ALIASES.items():
        assert alias_members(text, name) == union_members(alias)
    status = models["RemovalMessage"].model_fields["status"].annotation
    assert alias_members(text, "RemovalStatus") == set(get_args(status))
