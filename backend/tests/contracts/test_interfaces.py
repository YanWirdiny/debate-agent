import importlib
import inspect
from typing import get_type_hints
import pytest
from app import interfaces as api

FUNCTIONS = {
    "app.lanes.claims": ["run_claims"], "app.lanes.fallacy": ["run_fallacy"],
    "app.lanes.consistency": ["run_consistency"],
    "app.lanes.fake": ["run_claims", "run_fallacy", "run_consistency"],
    "app.intel.async_jobs": ["grounded_verdict", "prefetch", "summarize"],
    "app.llm.client": ["groq_json", "gemini_json"],
}
CLASSES = {"app.ingest": ["AudioPump", "TimelineLookup"], "app.segmenter": ["Segmenter"], "app.aggregator": ["Aggregator"], "app.eventlog": ["EventWriter"], "app.kb.store": ["KnowledgeBase"]}

def check(actual, expected):
    assert inspect.signature(actual) == inspect.signature(expected)
    assert get_type_hints(actual) == get_type_hints(expected)
    assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(expected)

@pytest.mark.parametrize("module,names", FUNCTIONS.items())
async def test_functions(module, names):
    for name in names:
        actual = getattr(importlib.import_module(module), name)
        check(actual, getattr(api, name))
        args = [None for p in inspect.signature(actual).parameters.values() if p.default is inspect.Parameter.empty]
        with pytest.raises(NotImplementedError):
            await actual(*args)

@pytest.mark.parametrize("module,names", CLASSES.items())
def test_classes(module, names):
    for name in names:
        actual = getattr(importlib.import_module(module), name)
        expected = getattr(api, name)
        for method in expected.__dict__:
            if not method.startswith("_"):
                check(getattr(actual, method), getattr(expected, method))

@pytest.mark.parametrize("module,names", CLASSES.items())
async def test_class_stubs_raise(module, names):
    for name in names:
        cls = getattr(importlib.import_module(module), name)
        instance = cls()
        for method in getattr(api, name).__dict__:
            if method.startswith("_"):
                continue
            bound = getattr(instance, method)
            args = [None for p in inspect.signature(bound).parameters.values() if p.default is inspect.Parameter.empty]
            with pytest.raises(NotImplementedError):
                if inspect.iscoroutinefunction(bound):
                    await bound(*args)
                else:
                    bound(*args)
