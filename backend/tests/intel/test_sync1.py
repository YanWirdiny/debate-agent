import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from pydantic import BaseModel, ConfigDict

from app.kb.store import KnowledgeBase, ReviewedClaim
from app.lanes import claims
from app.llm import client
from app.models import Annotation, Claim, ClaimRecord, LaneContext, Passage, Segment, SpeakerDigest, Statement
from app.verify import evidence_span, stt_safe, transcript_span, verify_annotation

FIXTURES = Path(__file__).parents[1] / 'fixtures/kb_small'


def rows(name):
    return [json.loads(s) for s in (FIXTURES / f'{name}.jsonl').read_text().splitlines()]


class Encoder:
    def encode(self, texts, normalize_embeddings=True):
        return np.array([[1., 0.] for _ in texts])


@pytest.fixture
def kb():
    bank = KnowledgeBase(encoder=Encoder(), claims=[ReviewedClaim.model_validate(r) for r in rows('claims')], passages=[Passage.model_validate(r) for r in rows('passages')], statements=[Statement.model_validate(r) for r in rows('statements')], digests=[SpeakerDigest.model_validate(r) for r in rows('speaker_digests')])
    bank._prepare()
    return bank


@pytest.fixture
def seg():
    return Segment(segment_id='s:0', session_id='s', speaker_id='speaker_a', role='speaker', text='Earth has one natural moon.', t_start=0, t_end=1, stt_confidence=.99)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def blocked(*args, **kwargs):
        raise AssertionError('unexpected HTTP request in mocked-provider tests')
    monkeypatch.setattr(httpx.AsyncClient, 'send', blocked)
    monkeypatch.setattr(client, '_settings', lambda: SimpleNamespace(
        llm_provider='groq', llm_model_fast='test-fast', llm_model_gemini='test-gemini', llm_model_gemini_fallback='test-fallback',
        groq_api_key=SimpleNamespace(get_secret_value=lambda: 'secret-test'), gemini_api_key=SimpleNamespace(get_secret_value=lambda: 'secret-test')))


def test_bank_review_source_and_identity(kb):
    claim = kb._claims[0].hit.claim
    assert kb.match_bank('speaker_a', claim).verdict_category == 'V'
    assert kb.match_bank('other', claim) is None
    assert kb.digest('speaker_a').name == 'Synthetic speaker A'
    assert kb.digest('other') is None
    kb._passages[0].text = 'Missing quote'
    assert kb.match_bank('speaker_a', claim) is None


@pytest.mark.parametrize('text', ['Earth has two natural moons.', 'Earth does not have one natural moon.', 'Earth has one artificial moon.', 'Earth has one natural moon in 1800.', 'Earth has 1 percent natural moon.', 'One natural moon has Earth.'])
def test_identical_embeddings_do_not_prove_equivalence(kb, text):
    original = kb._claims[0].hit.claim
    changed = original.model_copy(update={'text': text, 'decontextualized': text, 'embedding': [1., 0.]})
    assert kb.match_bank('speaker_a', changed) is None


def test_extraction_cannot_rewrite_a_claim_into_a_bank_hit(kb):
    original = kb._claims[0].hit.claim
    assert kb.match_bank('speaker_a', original.model_copy(update={'text': 'Earth has two moons.'})) is None


def test_search_copy_and_add(kb):
    assert kb.search_passages('moon', None)[0].passage_id == 'moon'
    assert kb.search_statements('speaker_a', 'moon')[0].statement_id == 'prior'
    assert kb.search_statements('other', 'moon') == []
    assert kb.search_passages('moon', None, 0) == []
    passage = kb._passages[0].model_copy(update={'passage_id': 'new'})
    kb.add_passages('new-topic', [passage])
    assert passage.topic == 'astronomy'
    assert kb.search_passages('moon', 'new-topic', 1)[0].topic == 'new-topic'
    assert kb.topic_for('moon') in {'astronomy', 'new-topic'}
    digest = kb.digest('speaker_a')
    digest.name = 'Changed'
    assert kb.digest('speaker_a').name != 'Changed'


class Collection:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls
    def find(self, query, projection):
        self.calls[self.name] = query
        async def cursor():
            for row in rows(self.name):
                if all((row.get(k) in v['$in'] if isinstance(v, dict) else row.get(k) == v) for k, v in query.items()):
                    yield row
        return cursor()


async def test_load_filters_all_topics_and_local_encoder(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda *args, **kwargs: Encoder()))
    calls = {}
    class DB:
        def __getitem__(self, name): return Collection(name, calls)
    bank = await KnowledgeBase.load(DB(), ['speaker_a'], [])
    assert calls['passages'] == {}
    assert calls['claims']['reviewed'] is True
    assert bank.digest('speaker_a')
    assert bank.match_bank('speaker_a', bank._claims[0].hit.claim)
    bank = await KnowledgeBase.load(DB(), ['speaker_a'], ['other-topic'])
    assert not bank._passages
    with pytest.raises(ValueError):
        await KnowledgeBase.load(None, [], [])


async def test_claim_emission_and_verification(monkeypatch, kb, seg):
    raw = {'checkworthy': True, 'claims': [{k: v for k, v in rows('claims')[0]['hit']['claim'].items() if k != 'embedding'}]}
    result = SimpleNamespace(data=raw, model_id='test-model', prompt_hash='hash')
    monkeypatch.setattr(claims, 'groq_json', AsyncMock(return_value=result))
    emitted = []
    queue = asyncio.Queue()
    await claims.run_claims(seg, LaneContext(), kb, emitted.append, queue)
    assert [type(x) for x in emitted] == [ClaimRecord, Annotation]
    record, annotation = emitted
    assert record.annotation_id == annotation.annotation_id == 's:0:claims:0'
    assert record.claim.embedding == []  # P1 dedupe must not use similarity alone
    assert annotation.status == 'verified'
    assert verify_annotation(annotation, seg, kb.source_texts())
    assert annotation.model_id == 'test-model' and annotation.prompt_hash == 'hash'
    assert queue.empty()
    bad = annotation.model_copy(update={'span_char_start': 1})
    assert not verify_annotation(bad, seg, kb.source_texts())
    seg.low_conf_words = ['one']
    emitted.clear()
    await claims.run_claims(seg, LaneContext(), kb, emitted.append, queue)
    assert emitted[-1].status == 'candidate'


async def test_moderator_invalid_span_and_unknown(monkeypatch, kb, seg):
    mock = AsyncMock()
    monkeypatch.setattr(claims, 'groq_json', mock)
    await claims.run_claims(seg.model_copy(update={'role': 'moderator'}), LaneContext(), kb, lambda x: None, asyncio.Queue())
    mock.assert_not_awaited()
    raw = {'checkworthy': True, 'claims': [dict(text='invented', decontextualized='invented', entities=[], numbers=[], topic=None)]}
    mock.return_value = SimpleNamespace(data=raw)
    with pytest.raises(ValueError, match='absent'):
        await claims.run_claims(seg, LaneContext(), kb, lambda x: None, asyncio.Queue())
    raw['claims'][0].update(text=seg.text, decontextualized=seg.text)
    emitted = []
    await claims.run_claims(seg, LaneContext(), KnowledgeBase(encoder=Encoder()), emitted.append, asyncio.Queue())
    assert len(emitted) == 1 and isinstance(emitted[0], ClaimRecord)
    assert emitted[0].annotation_id is None


def test_quote_and_stt_guards(kb, seg):
    evidence = kb._claims[0].hit.evidence[0]
    assert evidence_span(evidence, '\nEarth  has one natural moon.\n')
    assert not evidence_span(evidence, 'Earth has two natural moons.')
    assert not evidence_span(evidence.model_copy(update={'quote': ''}), seg.text)
    assert not transcript_span(seg, '', 0, 0)
    assert not stt_safe(seg.model_copy(update={'stt_confidence': .84}), seg.text)
    assert not stt_safe(seg.model_copy(update={'low_conf_words': ["isn't"]}), seg.text)
    assert not stt_safe(seg.model_copy(update={"low_conf_words": ["twenty"]}), seg.text)


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ok: bool


async def test_client_validation_retry_hash_and_safe_logs(monkeypatch, caplog):
    mock = AsyncMock(side_effect=[('{"wrong":true}', {}), ('{"ok":true}', {'prompt_tokens': 4})])
    monkeypatch.setattr(client, '_groq', mock)
    with caplog.at_level('INFO'):
        result = await client.groq_json('claim_extract', {'sentence': 'secret transcript'}, Output)
    assert mock.await_count == 2 and result.data == {'ok': True}
    assert result.prompt_hash == hashlib.sha256((client.PROMPTS / 'claim_extract.md').read_bytes()).hexdigest()
    assert result.usage == {'prompt_tokens': 4}
    assert 'secret transcript' not in caplog.text and 'secret-test' not in caplog.text


async def test_client_fallback_and_exhaustion(monkeypatch):
    groq = AsyncMock(side_effect=RuntimeError('secret-test provider body'))
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', groq)
    monkeypatch.setattr(client, '_gemini', gemini)
    result = await client.groq_json('claim_extract', {}, Output)
    assert groq.await_count == 2 and gemini.await_count == 1
    assert result.provider == 'gemini' and result.model_id == 'test-fallback'
    gemini.side_effect = RuntimeError('secret-test')
    with pytest.raises(client.LLMError, match='bounded') as err:
        await client.groq_json('claim_extract', {}, Output)
    assert 'secret-test' not in str(err.value)


async def test_deadline_and_cancellation(monkeypatch):
    async def slow(*args):
        await asyncio.sleep(5)
    monkeypatch.setattr(client, '_groq', slow)
    monkeypatch.setattr(client, '_gemini', slow)
    start = asyncio.get_running_loop().time()
    with pytest.raises(client.LLMError):
        await client.groq_json('claim_extract', {}, Output, timeout=.06)
    assert asyncio.get_running_loop().time() - start < .3
    task = asyncio.create_task(client.groq_json('claim_extract', {}, Output))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_gemini_schema_path_and_prompt_guards(monkeypatch):
    mock = AsyncMock(return_value=('{"ok":true}', {'cached_tokens': 5}))
    monkeypatch.setattr(client, '_gemini', mock)
    result = await client.gemini_json('claim_extract', {}, Output, grounded=True, cache_name='cache')
    assert result.provider == 'gemini'
    assert mock.call_args.args[-2:] == (True, 'cache')
    with pytest.raises(ValueError):
        await client.groq_json('../secret', {}, Output)
    with pytest.raises(client.LLMError, match='not implemented'):
        await client.groq_json('stance', {}, Output)


def test_unreviewed_and_conflicting_records_are_not_verdicts(kb):
    row = kb._claims[0].model_copy(deep=True)
    row.reviewed = False
    assert KnowledgeBase(encoder=Encoder(), claims=[row])._claims == []
    row.reviewed = True
    row.hit.verdict_category = 'F'
    kb._claims.append(row)
    assert kb.match_bank('speaker_a', row.hit.claim) is None


def test_source_metadata_and_ambiguous_source_rejected(kb):
    claim = kb._claims[0].hit.claim
    kb._claims[0].hit.evidence[0].url = 'https://example.invalid/invented'
    assert kb.match_bank('speaker_a', claim) is None
    kb._claims[0].hit.evidence[0].url = kb._passages[0].source.url
    kb._passages.append(kb._passages[0].model_copy(update={'text': 'conflicting source text'}))
    assert kb.match_bank('speaker_a', claim) is None


async def test_bank_hit_reaches_p1_ledger_event_log_and_audience(monkeypatch, kb, seg, tmp_path):
    from app.session import SessionRunner
    from app.eventlog import verify_chain
    from app.models import Event
    runner = SessionRunner('s', 'fixture', kb=kb, archive_path=tmp_path / 'events.jsonl')
    runner.aggregator.register_segment(seg)
    raw = {'checkworthy': True, 'claims': [rows('claims')[0]['hit']['claim']]}
    monkeypatch.setattr(claims, 'groq_json', AsyncMock(return_value=SimpleNamespace(data=raw, model_id='mock', prompt_hash='hash')))
    audience = AsyncMock()
    runner.aggregator.audience = audience
    writer_task = asyncio.create_task(runner.writer.run())
    agg_task = asyncio.create_task(runner.aggregator.run())
    await claims.run_claims(seg, LaneContext(), kb, runner.emit, runner.slow_q)
    await runner.aggregator.stop()
    await agg_task
    await runner.writer.stop()
    await writer_task
    assert runner.ctx.claim_ledger['speaker_a'][0].annotation_id == 's:0:claims:0'
    annotation = runner.aggregator.state['s:0:claims:0']
    assert runner.aggregator.is_public(annotation)
    audience.broadcast.assert_awaited_once()
    events = [Event.model_validate_json(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert verify_chain(events) and events[0].type == 'annotation'


async def test_groq_adapter_sends_json_schema_and_async_request(monkeypatch):
    response = httpx.Response(200, request=httpx.Request('POST', 'https://example.invalid'), json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}], 'usage': {'prompt_tokens': 2}})
    post = AsyncMock(return_value=response)
    connection = SimpleNamespace(post=post)
    class Context:
        async def __aenter__(self): return connection
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(client.httpx, 'AsyncClient', lambda **kwargs: Context())
    raw, usage = await client._groq('mock', 'fixture-key', 'Static rules', '{"sentence":"untrusted"}', Output, .2)
    assert Output.model_validate_json(raw).ok
    request = post.call_args.kwargs
    assert request['json']['temperature'] == 0
    assert request['json']['response_format'] == {'type': 'json_object'}
    assert '"ok"' in request['json']['messages'][0]['content']
    assert request['json']['messages'][1]['content'] == '{"sentence":"untrusted"}'
    assert usage == {'prompt_tokens': 2}


async def test_gemini_adapter_uses_async_sdk_schema_and_closes(monkeypatch):
    from google import genai
    generate = AsyncMock(return_value=SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason='STOP')], text='{"ok":true}',
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2, cached_content_token_count=1)))
    seen = {}
    class Context:
        async def __aenter__(self): return SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        async def __aexit__(self, *args): seen['closed'] = True
    def factory(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(aio=Context())
    monkeypatch.setattr(genai, 'Client', factory)
    raw, usage = await client._gemini('mock', 'fixture-key', 'Rules', '{}', Output, .25, False, None)
    assert Output.model_validate_json(raw).ok and usage['cached_tokens'] == 1
    assert seen['closed'] and seen['http_options'].timeout == 250
    assert seen['http_options'].retry_options.attempts == 1
    config = generate.call_args.kwargs['config']
    assert config.response_mime_type == 'application/json' and config.response_schema is Output
    assert config.temperature == 0 and config.system_instruction == 'Rules'
    await client._gemini('mock', 'fixture-key', 'Rules', '{}', Output, .25, True, 'cache')
    config = generate.call_args.kwargs['config']
    assert config.cached_content == 'cache' and config.system_instruction is None and config.tools


def _extracted(text, **updates):
    claim = dict(text=text, decontextualized=text, entities=[], numbers=[], topic=None)
    claim.update(updates)
    return claim


async def test_claims_llm_timeout_is_shorter_than_the_lane_budget(monkeypatch, kb, seg):
    import inspect
    from app.session import LANE_TIMEOUT_S, SessionRunner
    mock = AsyncMock(return_value=SimpleNamespace(data={'checkworthy': False, 'claims': []}, model_id='m', prompt_hash='h'))
    monkeypatch.setattr(claims, 'groq_json', mock)
    await claims.run_claims(seg, LaneContext(), kb, lambda item: None, asyncio.Queue())
    timeout = mock.await_args.kwargs['timeout']
    lane_timeout = inspect.signature(SessionRunner.__init__).parameters['lane_timeout'].default
    assert lane_timeout == LANE_TIMEOUT_S
    assert timeout == claims.CLAIMS_LLM_TIMEOUT_S == LANE_TIMEOUT_S - claims.CLAIMS_LLM_RESERVE_S
    assert timeout < lane_timeout
    assert timeout == pytest.approx(.8)


async def test_first_groq_attempt_uses_most_of_the_budget(monkeypatch):
    from app.lanes.claims import CLAIMS_LLM_TIMEOUT_S
    allotted = []

    async def slow(*args, **kwargs):
        allotted.append(args[-1])
        await asyncio.sleep(5)

    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', slow)
    monkeypatch.setattr(client, '_gemini', gemini)
    timeout = CLAIMS_LLM_TIMEOUT_S
    reserve = client.MIN_FALLBACK_BUDGET_S + client.FALLBACK_SLACK_S
    start = asyncio.get_running_loop().time()
    result = await client.groq_json('claim_extract', {}, Output, timeout=timeout)
    elapsed = asyncio.get_running_loop().time() - start
    assert client.MIN_FALLBACK_BUDGET_S == pytest.approx(.35)
    assert client.FALLBACK_SLACK_S == pytest.approx(.03)
    assert len(allotted) == 1
    assert allotted[0] == pytest.approx(timeout - reserve, abs=0.02)
    assert allotted[0] > timeout / 2
    assert elapsed > allotted[0] - 0.05
    assert elapsed < allotted[0] + 0.25
    assert result.provider == 'gemini'
    assert gemini.await_count == 1


async def test_groq_timeout_does_not_retry(monkeypatch):
    calls = 0

    async def slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)

    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', slow)
    monkeypatch.setattr(client, '_gemini', gemini)
    result = await client.groq_json('claim_extract', {}, Output, timeout=0.8)
    assert calls == 1
    assert gemini.await_count == 1
    assert result.provider == 'gemini'
    assert result.model_id == 'test-fallback'


async def test_http_timeout_exception_does_not_retry_groq(monkeypatch):
    groq = AsyncMock(side_effect=httpx.TimeoutException('timed out'))
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', groq)
    monkeypatch.setattr(client, '_gemini', gemini)
    result = await client.groq_json('claim_extract', {}, Output, timeout=0.8)
    assert groq.await_count == 1
    assert gemini.await_count == 1
    assert result.provider == 'gemini'


async def test_fallback_skipped_when_remaining_below_minimum(monkeypatch, caplog):
    groq = AsyncMock(side_effect=TimeoutError())
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', groq)
    monkeypatch.setattr(client, '_gemini', gemini)
    timeout = client.MIN_FALLBACK_BUDGET_S / 2
    with caplog.at_level('WARNING'):
        with pytest.raises(client.LLMError, match='bounded') as err:
            await client.groq_json('claim_extract', {'sentence': 'secret transcript'}, Output, timeout=timeout)
    assert timeout < client.MIN_FALLBACK_BUDGET_S
    assert groq.await_count == 1
    assert gemini.await_count == 0
    assert 'fallback skipped' in caplog.text
    assert 'secret transcript' not in caplog.text
    assert 'secret-test' not in caplog.text
    assert 'secret-test' not in str(err.value)


async def test_fast_http_error_retries_groq_once(monkeypatch):
    request = httpx.Request('POST', 'https://example.invalid')
    error = httpx.HTTPStatusError('429', request=request, response=httpx.Response(429, request=request))
    groq = AsyncMock(side_effect=[error, ('{"ok":true}', {'prompt_tokens': 1})])
    gemini = AsyncMock(return_value=('{"ok":true}', {}))
    monkeypatch.setattr(client, '_groq', groq)
    monkeypatch.setattr(client, '_gemini', gemini)
    timeout = 0.8
    result = await client.groq_json('claim_extract', {}, Output, timeout=timeout)
    assert groq.await_count == 2
    assert gemini.await_count == 0
    assert result.provider == 'groq'
    first = groq.await_args_list[0].args[-1]
    reserve = client.MIN_FALLBACK_BUDGET_S + client.FALLBACK_SLACK_S
    assert first == pytest.approx(timeout - reserve, abs=0.02)
    assert first > timeout / 2


async def test_one_bad_span_keeps_the_valid_claims(monkeypatch, kb, seg, caplog):
    moon = {k: v for k, v in rows('claims')[0]['hit']['claim'].items() if k != 'embedding'}
    other = 'Mars has two moons.'
    bad = 'NOT-IN-TRANSCRIPT'
    seg = seg.model_copy(update={'text': f"{moon['text']} {other}"})
    raw = {'checkworthy': True, 'claims': [moon, _extracted(bad), _extracted(other, entities=['Mars'], numbers=['two'])]}
    monkeypatch.setattr(claims, 'groq_json', AsyncMock(return_value=SimpleNamespace(data=raw, model_id='test-model', prompt_hash='hash')))
    emitted = []
    with caplog.at_level('DEBUG'):
        await claims.run_claims(seg, LaneContext(), kb, emitted.append, asyncio.Queue())
    records = [item for item in emitted if isinstance(item, ClaimRecord)]
    annotations = [item for item in emitted if isinstance(item, Annotation)]
    assert [record.claim.text for record in records] == [moon['text'], other]
    assert [annotation.span_text for annotation in annotations] == [moon['text']]
    assert records[0].annotation_id == annotations[0].annotation_id == 's:0:claims:0'
    assert records[1].annotation_id is None
    assert annotations[0].span_char_start == 0
    assert seg.text[annotations[0].span_char_start:annotations[0].span_char_end] == moon['text']
    assert bad not in caplog.text and seg.text not in caplog.text and other not in caplog.text


async def test_all_bad_spans_leave_the_segment_pending(monkeypatch, kb, seg, caplog):
    from app.hubs import Hub, RecordingClient
    from app.models import PendingMessage
    from app.session import SessionRunner
    bad = ['NOT-IN-TRANSCRIPT-1', 'NOT-IN-TRANSCRIPT-2', 'NOT-IN-TRANSCRIPT-3']
    raw = {'checkworthy': True, 'claims': [_extracted(text) for text in bad]}
    monkeypatch.setattr(claims, 'groq_json', AsyncMock(return_value=SimpleNamespace(data=raw, model_id='m', prompt_hash='h')))
    emitted = []
    with caplog.at_level('DEBUG'):
        with pytest.raises(ValueError, match='absent') as err:
            await claims.run_claims(seg, LaneContext(), kb, emitted.append, asyncio.Queue())
    assert emitted == []
    assert all(text not in caplog.text and text not in str(err.value) for text in bad)
    assert seg.text not in caplog.text
    op, au = Hub('operator'), Hub('audience')
    opc, auc = RecordingClient(), RecordingClient()
    op.add(opc)
    au.add(auc)
    runner = SessionRunner('s', 'fixture', operator=op, audience=au, kb=kb)
    await runner._lane(seg, 'claims', claims.run_claims(seg, LaneContext(), kb, runner.emit, asyncio.Queue()))
    assert runner.ctx.claim_ledger == {}
    pending = [PendingMessage.model_validate_json(message) for message in opc.messages]
    assert [(item.segment_id, item.lane, item.reason) for item in pending] == [(seg.segment_id, 'claims', 'error')]
    assert auc.messages == []
    assert all(text not in caplog.text for text in bad)
