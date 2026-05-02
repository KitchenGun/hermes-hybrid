"""Tests for src/job_factory/classifier.py."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.job_factory.classifier import JobClassification, JobClassifier
from src.job_factory.registry import (
    ClassifierConfig,
    JobType,
    JobTypeRegistry,
)
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
)


# ---- Helpers --------------------------------------------------------------


def _registry(
    job_types: list[JobType] | None = None,
    *,
    fast_keyword: bool = True,
    fallback: str = "simple_chat",
) -> JobTypeRegistry:
    types = job_types or [
        JobType(
            name="simple_chat",
            keywords_ko=("안녕", "고마워"),
            keywords_en=("hi", "thanks"),
        ),
        JobType(
            name="code_generation",
            keywords_ko=("코드 작성", "함수 만들어"),
            keywords_en=("write code", "implement"),
        ),
        JobType(
            name="code_review",
            keywords_ko=("코드 리뷰", "검토해줘"),
            keywords_en=("code review", "find bugs"),
        ),
        JobType(
            name="schedule_logging",
            keywords_ko=("기록", "방금 했어"),
            keywords_en=("log activity",),
        ),
    ]
    return JobTypeRegistry(
        job_types={jt.name: jt for jt in types},
        classifier=ClassifierConfig(
            fast_keyword_path=fast_keyword,
            llm_model="qwen2.5:3b-instruct",
            llm_timeout_seconds=5,
            fallback_job_type=fallback,
        ),
    )


@dataclass
class _CannedAdapter:
    """Returns a single canned text. Records the request it received."""

    text: str
    raise_exc: Exception | None = None
    last_request: AdapterRequest | None = None

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        self.last_request = request
        if self.raise_exc:
            raise self.raise_exc
        return AdapterResponse(
            text=self.text,
            provider="fake",
            model="fake-model",
            duration_ms=10,
            completion_tokens=5,
        )


# ---- Fast keyword path ----------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_match_korean():
    c = JobClassifier(_registry())
    res = await c.classify("코드 작성 좀 해줘")
    assert res.job_type == "code_generation"
    assert res.method == "keyword"
    assert res.confidence == 0.9
    assert res.matched_keyword == "코드 작성"


@pytest.mark.asyncio
async def test_keyword_match_english_case_insensitive():
    c = JobClassifier(_registry())
    res = await c.classify("CAN you WRITE CODE for me?")
    assert res.job_type == "code_generation"
    assert res.method == "keyword"


@pytest.mark.asyncio
async def test_keyword_longest_match_wins():
    """'코드 리뷰' should beat the shorter 'code review'-substring 'review'
    when both match — longest keyword wins."""
    c = JobClassifier(_registry([
        JobType(name="x", keywords_ko=("코드",)),
        JobType(name="y", keywords_ko=("코드 리뷰",)),
        JobType(name="simple_chat"),  # for fallback default
    ], fallback="simple_chat"))
    res = await c.classify("이 코드 리뷰 부탁해")
    assert res.job_type == "y"
    assert res.matched_keyword == "코드 리뷰"


@pytest.mark.asyncio
async def test_keyword_no_match_falls_through():
    """Without an LLM and no keyword hit, returns fallback."""
    c = JobClassifier(_registry())
    res = await c.classify("foo bar baz xyz")
    assert res.job_type == "simple_chat"
    assert res.method == "fallback"
    assert res.confidence == 0.3


@pytest.mark.asyncio
async def test_keyword_path_disabled_skips_keywords():
    """When fast_keyword_path=False, keyword stage is skipped entirely."""
    c = JobClassifier(_registry(fast_keyword=False))
    # Even with a perfect keyword match, no LLM, no fallback should fire.
    res = await c.classify("코드 작성 해줘")
    assert res.method == "fallback"
    assert res.job_type == "simple_chat"


# ---- LLM stage ------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_returns_pure_label():
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(text="schedule_logging"),
    )
    res = await c.classify("뭔가 했어")
    assert res.job_type == "schedule_logging"
    assert res.method == "llm"
    assert res.confidence == 0.6


@pytest.mark.asyncio
async def test_llm_extracts_label_from_chatty_response():
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(
            text="The answer is code_review because the user asks to check code.",
        ),
    )
    res = await c.classify("좀 검토 부탁")
    assert res.job_type == "code_review"
    assert res.method == "llm"


@pytest.mark.asyncio
async def test_llm_strips_quotes_and_punctuation():
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(text='`simple_chat`.'),
    )
    res = await c.classify("hi")
    assert res.job_type == "simple_chat"
    assert res.method == "llm"


@pytest.mark.asyncio
async def test_llm_unknown_label_falls_back():
    """LLM returns a label not in the registry → fallback."""
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(text="not_a_real_job_type"),
    )
    res = await c.classify("xyz")
    assert res.method == "fallback"
    assert res.job_type == "simple_chat"


@pytest.mark.asyncio
async def test_llm_unparseable_response_falls_back():
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(text="?!?!"),  # nothing label-shaped
    )
    res = await c.classify("xyz")
    assert res.method == "fallback"


@pytest.mark.asyncio
async def test_llm_exception_falls_back():
    """An LLM error must NOT crash the classifier — fall through."""
    c = JobClassifier(
        _registry(fast_keyword=False),
        llm_adapter=_CannedAdapter(
            text="ignored",
            raise_exc=RuntimeError("simulated LLM failure"),
        ),
    )
    res = await c.classify("xyz")
    assert res.method == "fallback"
    assert res.job_type == "simple_chat"


@pytest.mark.asyncio
async def test_llm_request_includes_all_labels():
    """The classifier prompt should list every job_type so the model
    has the choice space."""
    adapter = _CannedAdapter(text="simple_chat")
    c = JobClassifier(_registry(fast_keyword=False), llm_adapter=adapter)
    await c.classify("xyz")
    # Inspect the prompt that went to the LLM.
    sent = adapter.last_request
    assert sent is not None
    payload = sent.messages[0].content
    for label in ("simple_chat", "code_generation", "code_review",
                  "schedule_logging"):
        assert label in payload


@pytest.mark.asyncio
async def test_llm_timeout_passed_through():
    adapter = _CannedAdapter(text="simple_chat")
    c = JobClassifier(_registry(fast_keyword=False), llm_adapter=adapter)
    await c.classify("xyz")
    # timeout_s should match the registry's classifier.llm_timeout_seconds.
    assert adapter.last_request.timeout_s == 5.0


# ---- Priority: keyword > LLM > fallback -----------------------------------


@pytest.mark.asyncio
async def test_keyword_short_circuits_llm():
    """If keyword matches, the LLM must never be called."""
    adapter = _CannedAdapter(text="code_generation")
    c = JobClassifier(_registry(), llm_adapter=adapter)
    res = await c.classify("안녕! 잘 지내?")
    assert res.method == "keyword"
    assert res.job_type == "simple_chat"
    # adapter wasn't called.
    assert adapter.last_request is None
