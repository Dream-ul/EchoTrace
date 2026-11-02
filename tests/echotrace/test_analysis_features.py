from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
import os

from echotrace.application.analysis_features import (
    ANALYSIS_FEATURE_FACTUALITY,
    analyze_document,
    analyze_sentiment,
    analyze_stance,
    extract_event_time_window,
    extract_time_mentions,
    RuleSentimentAnalyzer,
    TransformersSentimentAnalyzer,
    AutoSentimentAnalyzer,
    build_sentiment_analyzer,
)
from echotrace.domain.models import Document


def test_extracts_chinese_full_date_and_clock_in_source_text():
    mentions = extract_time_mentions("事件于2026年9月22日上午9点发生。")

    assert len(mentions) == 1
    assert mentions[0].start is not None
    assert mentions[0].start.year == 2026
    assert mentions[0].start.hour == 9
    assert mentions[0].precision == "exact"
    assert mentions[0].basis == "explicit_source"


def test_month_day_without_year_stays_unresolved_but_relative_time_uses_reference():
    mentions = extract_time_mentions(
        "9月22日出现首条信息，昨天继续发酵。",
        reference_time=datetime(2026, 9, 23, 12, 0),
    )

    assert mentions[0].needs_review is True
    assert mentions[0].start is None
    assert mentions[0].precision == "unknown"
    assert mentions[0].basis == "unknown"
    relative = mentions[1]
    assert relative.start is not None
    assert relative.start.date().isoformat() == "2026-09-22"
    assert relative.basis == "derived_from_source"


def test_explicit_dates_connected_by_range_are_normalized_as_one_window():
    mentions = extract_time_mentions("影响从2026年9月22日至2026年9月24日持续。")

    assert len(mentions) == 1
    assert mentions[0].precision == "range"
    assert mentions[0].start.date().isoformat() == "2026-09-22"
    assert mentions[0].end.date().isoformat() == "2026-09-24"


def test_event_time_does_not_fall_back_to_publication_or_retrieval_time():
    document = Document(
        source="news",
        title="报道",
        content="文章只记录了相关讨论，没有明确事件日期。",
        published_at=datetime(2026, 9, 23, 10, 0),
        fetched_at=datetime(2026, 9, 23, 11, 0),
    )

    start, end, precision, basis = extract_event_time_window(document)

    assert (start, end) == (None, None)
    assert precision == basis == "unknown"
    features = analyze_document(document)
    time_feature = next(item for item in features if item["feature_type"] == "time_mentions")
    assert time_feature["value"]["published_time"] is not None
    assert time_feature["value"]["retrieved_time"] is not None
    assert not time_feature["value"]["mentions"]


def test_month_day_without_year_never_becomes_event_year_from_publication_date():
    document = Document(
        source="news",
        title="旧事件回顾",
        content="10月13日，学校发布处理通报。",
        published_at=datetime(2026, 10, 14, 10, 0),
        fetched_at=datetime(2026, 10, 14, 11, 0),
    )

    assert extract_event_time_window(document) == (None, None, "unknown", "unknown")


def test_analysis_features_are_chinese_first_and_never_fact_support():
    document = Document(
        source="official",
        title="市政府通报",
        content="上海市政府发布通告，公共安全风险得到改善，但居民仍然担忧。",
        metadata={"entities": ["上海市政府"]},
    )

    features = analyze_document(document, target="公共事件")
    assert {item["feature_type"] for item in features} == {
        "time_mentions", "entity_mentions", "sentiment", "stance", "topic",
    }
    assert all(item["factuality"] == ANALYSIS_FEATURE_FACTUALITY for item in features)
    entity_feature = next(item for item in features if item["feature_type"] == "entity_mentions")
    assert any(item["normalized"] == "上海市政府" for item in entity_feature["value"]["entities"])
    assert analyze_sentiment("事件得到改善，但居民仍然担忧", analyzer=RuleSentimentAnalyzer())["label_zh"] == "混合"
    assert analyze_stance("居民质疑处理方案", target="处理方案")["label_zh"] == "质疑"


def test_document_requires_content():
    with pytest.raises(ValueError):
        Document(source="test", title="empty", content="")


def test_sentiment_backend_is_injectable_and_records_backend_metadata():
    analyzer = RuleSentimentAnalyzer()
    result = analyze_sentiment("事件已经恢复，但居民仍然担忧", analyzer=analyzer)
    assert result["backend"] == "rule"
    assert result["model_name"] == "echotrace-zh-rules"
    assert result["probabilities"]


def test_transformers_backend_can_be_constructed_without_downloading_weights():
    analyzer = build_sentiment_analyzer(backend="transformers", model_path="missing-local-model")
    assert isinstance(analyzer, TransformersSentimentAnalyzer)
    with pytest.raises((RuntimeError, OSError, EnvironmentError)):
        analyzer.analyze("这是一条中文测试文本")


def test_auto_sentiment_fallback_is_safe_for_parallel_node_workers():
    analyzer = AutoSentimentAnalyzer(model_path="missing-local-model", local_files_only=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(analyzer.analyze, ["这是一条中文测试文本"] * 4))
    assert all(result["backend"] == "rule_fallback" for result in results)
    assert analyzer.fallback_reason


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("ECHOTRACE_RUN_REAL_SENTIMENT") != "1", reason="set ECHOTRACE_RUN_REAL_SENTIMENT=1 to run cached real model inference")
def test_real_chinese_sentiment_model_distinguishes_positive_negative_neutral():
    analyzer = build_sentiment_analyzer(
        backend="transformers",
        model_name="senlou/weibo-sentiment-chinese-bert",
        local_files_only=True,
    )
    samples = {
        "positive": "这项服务非常好，我对处理结果很满意。",
        "negative": "这起事故造成严重影响，居民非常担忧。",
        "neutral": "公告说明了处理流程，暂时没有表达明显态度。",
    }
    results = {expected: analyzer.analyze(text) for expected, text in samples.items()}
    assert results["positive"]["label"] == "positive"
    assert results["negative"]["label"] == "negative"
    assert results["neutral"]["label"] == "neutral"
    assert all(result["backend"] == "transformers" for result in results.values())
