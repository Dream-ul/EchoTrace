"""Chinese-first, source-grounded analysis features for Trace.

The first implementation is deliberately deterministic and dependency-light.
Optional NLP models can replace one analyzer later, but every result keeps its
input hash, model identity, source span and ``analysis_feature`` factuality so
it cannot be mistaken for verified evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo


ANALYSIS_MODEL_NAME = "echotrace-zh-rules"
ANALYSIS_MODEL_VERSION = "zh-rules.v1"
ANALYSIS_FEATURE_FACTUALITY = "analysis_feature"
SENTIMENT_BACKEND_ENV = "ECHOTRACE_SENTIMENT_BACKEND"
SENTIMENT_MODEL_ENV = "ECHOTRACE_SENTIMENT_MODEL"
SENTIMENT_MODEL_PATH_ENV = "ECHOTRACE_SENTIMENT_MODEL_PATH"
SENTIMENT_MAX_LENGTH_ENV = "ECHOTRACE_SENTIMENT_MAX_LENGTH"
SENTIMENT_LOCAL_ONLY_ENV = "ECHOTRACE_SENTIMENT_LOCAL_ONLY"
# This checkpoint is fine-tuned on Chinese public-discourse sentiment and has
# a native Chinese BERT tokenizer.
DEFAULT_SENTIMENT_MODEL = "senlou/weibo-sentiment-chinese-bert"

_FULL_DATE = re.compile(
    r"(?P<year>20\d{2})\s*[年/-]\s*(?P<month>\d{1,2})\s*[月/-]\s*(?P<day>\d{1,2})\s*日?"
)
_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?")
_CLOCK = re.compile(r"(?P<hour>[01]?\d|2[0-3])\s*[点时:]\s*(?P<minute>\d{1,2})?\s*分?")
_RELATIVE = re.compile(r"今天|昨日|昨天|前天|明天|当日|当天|当晚|昨晚|随后|次日")
_RANGE_CONNECTOR = re.compile(r"\s*(?:至|到|—|–|-)\s*")

_POSITIVE = ("支持", "赞成", "认可", "满意", "成功", "改善", "恢复", "获批", "感谢", "安全")
_NEGATIVE = ("事故", "故障", "投诉", "质疑", "反对", "担忧", "风险", "伤亡", "失败", "不满", "损坏", "违规")
_STANCE_SUPPORT = ("支持", "赞成", "认可", "同意", "拥护")
_STANCE_OPPOSE = ("反对", "拒绝", "不支持", "否认", "投诉", "担忧")
_STANCE_QUESTION = ("是否", "为何", "为什么", "能否", "质疑", "担心", "要求解释", "待核")
_TOPIC_KEYWORDS = {
    "公共安全": ("安全", "事故", "伤亡", "风险", "应急"),
    "产品与质量": ("产品", "型号", "质量", "缺陷", "召回", "批次"),
    "消费与服务": ("消费者", "退款", "售后", "订单", "服务", "投诉"),
    "政策与监管": ("政策", "规定", "监管", "部门", "通告", "条例"),
    "公共舆情": ("舆情", "热搜", "传播", "网友", "评论", "平台"),
}
_SENTIMENT_ZH = {
    "positive": "正向",
    "negative": "负向",
    "neutral": "中性",
    "mixed": "混合",
    "unclear": "不明确",
}
_STANCE_ZH = {
    "support": "支持",
    "oppose": "反对",
    "neutral": "中立",
    "questioning": "质疑",
    "unclear": "不明确",
}


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("UTC")


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _make_datetime(year: int | None, month: int, day: int, hour: int | None, minute: int | None, zone: ZoneInfo, reference: datetime | None) -> datetime | None:
    if year is None:
        if reference is None:
            return None
        year = _aware(reference, zone).year
    try:
        return datetime(year, month, day, hour or 0, minute or 0, tzinfo=zone)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class TimeMention:
    expression: str
    start: datetime | None
    end: datetime | None
    precision: str
    basis: str
    timezone: str
    source_span: tuple[int, int]
    needs_review: bool = False
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "precision": self.precision,
            "basis": self.basis,
            "timezone": self.timezone,
            "source_span": list(self.source_span),
            "needs_review": self.needs_review,
            "rationale": self.rationale,
        }


def extract_time_mentions(
    text: str,
    *,
    reference_time: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> list[TimeMention]:
    """Extract explicit Chinese dates without turning retrieval time into event time."""
    content = str(text or "")
    zone = _zone(timezone_name)
    reference = _aware(reference_time, zone) if reference_time else None
    mentions: list[TimeMention] = []
    occupied: list[tuple[int, int]] = []

    for match in _FULL_DATE.finditer(content):
        start = _make_datetime(
            int(match.group("year")), int(match.group("month")), int(match.group("day")),
            None, None, zone, reference,
        )
        tail = content[match.end():match.end() + 14]
        clock = _CLOCK.search(tail)
        if clock and start:
            start = start.replace(hour=int(clock.group("hour")), minute=int(clock.group("minute") or 0))
        end = start
        precision = "exact" if clock else "day"
        end_index = match.end() + (clock.end() if clock else 0)
        mentions.append(TimeMention(match.group(0) + (clock.group(0) if clock else ""), start, end, precision, "explicit_source", timezone_name, (match.start(), end_index)))
        occupied.append((match.start(), end_index))

    for match in _MONTH_DAY.finditer(content):
        if any(left <= match.start() < right for left, right in occupied):
            continue
        # A month/day without a year is not an event date we can safely
        # normalize from publication or retrieval time. Keep it visible as an
        # unresolved mention rather than silently shifting old events forward.
        start = None
        mentions.append(TimeMention(
            match.group(0), start, None, "unknown", "unknown", timezone_name,
            (match.start(), match.end()), needs_review=True,
            rationale="来源日期缺少年份；发布日期和抓取时间不能替代事件年份",
        ))
        occupied.append((match.start(), match.end()))

    for match in _RELATIVE.finditer(content):
        start = None
        if reference and match.group(0) in {"今天", "当日", "当天", "当晚"}:
            start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        elif reference and match.group(0) in {"昨日", "昨天", "昨晚"}:
            start = (reference - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif reference and match.group(0) == "前天":
            start = (reference - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif reference and match.group(0) == "明天":
            start = (reference + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif reference and match.group(0) in {"随后", "次日"}:
            start = (reference + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        mentions.append(TimeMention(
            match.group(0), start, start, "day" if start else "unknown", "derived_from_source" if start else "unknown",
            timezone_name, (match.start(), match.end()), needs_review=start is None,
            rationale="相对时间缺少可用参照时刻" if start is None else "根据来源参照时间归一化",
        ))

    # A connector between two explicit mentions denotes a range, not an
    # automatic causal relation.  Preserve both source spans and only widen
    # the earlier mention's end when both sides have normalized dates.
    mentions.sort(key=lambda item: item.source_span)
    merged: list[TimeMention] = []
    for current in mentions:
        if merged and _RANGE_CONNECTOR.fullmatch(content[merged[-1].source_span[1]:current.source_span[0]]) and merged[-1].start and current.start:
            previous = merged[-1]
            merged[-1] = TimeMention(
                expression=content[previous.source_span[0]:current.source_span[1]],
                start=previous.start,
                end=current.end,
                precision="range",
                basis="explicit_source",
                timezone=timezone_name,
                source_span=(previous.source_span[0], current.source_span[1]),
            )
        else:
            merged.append(current)
    return merged


def extract_event_time_window(document: Any) -> tuple[datetime | None, datetime | None, str, str]:
    """Return only explicit/derived source times; publication and retrieval stay separate."""
    metadata = document.metadata if isinstance(getattr(document, "metadata", None), dict) else {}
    reference = getattr(document, "published_at", None)
    mentions = extract_time_mentions(str(getattr(document, "content", "")), reference_time=reference, timezone_name=str(metadata.get("timezone") or "Asia/Shanghai"))
    normalized = [item for item in mentions if item.start is not None]
    if not normalized:
        return None, None, "unknown", "unknown"
    first = normalized[0]
    return first.start, first.end or first.start, first.precision, first.basis


@dataclass(frozen=True, slots=True)
class EntityMention:
    text: str
    entity_type: str
    source_span: tuple[int, int]
    normalized: str
    confidence: float
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "entity_type": self.entity_type,
            "source_span": list(self.source_span),
            "normalized": self.normalized,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
        }


_ENTITY_PATTERNS = (
    ("organization", re.compile(r"[一-鿿A-Za-z0-9]{2,40}(?:公司|集团|政府|部门|委员会|大学|医院|银行|平台|协会|研究院|局|厅|所)")),
    ("location", re.compile(r"[一-鿿A-Za-z0-9]{2,30}(?:省|市|县|区|镇|村|街道|路|港|机场|广场)")),
    ("quoted_term", re.compile(r"[“\"‘']([^”\"’']{2,60})[”\"’']")),
)


def extract_entities(text: str, *, known_entities: Iterable[str] = ()) -> list[EntityMention]:
    content = str(text or "")
    values: dict[tuple[str, str], EntityMention] = {}
    for entity in known_entities:
        value = str(entity).strip()
        if value and value in content:
            index = content.find(value)
            values.setdefault((value, "known_entity"), EntityMention(value, "known_entity", (index, index + len(value)), value, 0.98))
    for entity_type, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(content):
            value = match.group(1) if entity_type == "quoted_term" else match.group(0)
            value = value.strip()
            if not value:
                continue
            values.setdefault((value, entity_type), EntityMention(value, entity_type, match.span(), value, 0.72, entity_type == "quoted_term"))
    return sorted(values.values(), key=lambda item: (item.source_span[0], item.text))


def _label_counts(text: str, words: Iterable[str]) -> int:
    return sum(text.count(word) for word in words)


class SentimentAnalyzer(Protocol):
    """Pluggable sentiment contract; implementations must be source-safe."""

    def analyze(self, text: str) -> dict[str, Any]: ...


class RuleSentimentAnalyzer:
    """Dependency-free Chinese lexicon fallback."""

    model_name = ANALYSIS_MODEL_NAME
    model_version = ANALYSIS_MODEL_VERSION
    backend = "rule"

    def analyze(self, text: str) -> dict[str, Any]:
        content = str(text or "")
        positive = _label_counts(content, _POSITIVE)
        negative = _label_counts(content, _NEGATIVE)
        total = positive + negative
        if not total:
            label = "unclear"
        elif positive and negative:
            label = "mixed"
        elif positive > negative:
            label = "positive"
        elif negative > positive:
            label = "negative"
        else:
            label = "neutral"
        # This is a lexical signal, not a calibrated probability. Keep it
        # explicit so consumers cannot mistake it for model certainty.
        denominator = max(total, 1)
        probabilities = {
            "positive": round(positive / denominator, 4),
            "negative": round(negative / denominator, 4),
            "neutral": round(1 / denominator, 4) if not total else 0.0,
        }
        return {
            "label": label,
            "label_zh": _SENTIMENT_ZH[label],
            "positive_hits": positive,
            "negative_hits": negative,
            "confidence": round(min(0.95, 0.5 + min(total, 5) * 0.08), 3) if total else 0.2,
            "probabilities": probabilities,
            "method": "词典候选，不代表事实真伪",
            "backend": self.backend,
            "model_name": self.model_name,
            "model_version": self.model_version,
        }


def _canonical_model_label(raw: Any, index: int, count: int) -> str:
    value = str(raw or "").strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "very negative": "very_negative", "非常负面": "very_negative", "5": "very_negative",
        "negative": "negative", "负面": "negative", "负向": "negative",
        "neutral": "neutral", "中性": "neutral", "中立": "neutral",
        "positive": "positive", "正面": "positive", "正向": "positive",
        "very positive": "very_positive", "非常正面": "very_positive", "1": "very_positive",
    }
    # Match the complete config label. Substring matching would classify
    # ``Very Positive`` as ``Positive`` and collapse the five-class output.
    if value in aliases:
        return aliases[value]
    if count == 5:
        return ("very_negative", "negative", "neutral", "positive", "very_positive")[index]
    if count == 3:
        return ("negative", "neutral", "positive")[index]
    return "neutral"


class TransformersSentimentAnalyzer:
    """Lazy Hugging Face sequence classifier for Chinese/multilingual text.

    The optional dependency and model weights are loaded only on first use.
    ``local_files_only`` defaults to true to prevent a request worker from
    unexpectedly downloading a multi-hundred-megabyte checkpoint.
    """

    backend = "transformers"

    def __init__(self, *, model_name: str = DEFAULT_SENTIMENT_MODEL, model_path: str | None = None, max_length: int = 512, local_files_only: bool = True) -> None:
        self.model_name = str(model_path or model_name)
        self.model_version = "unknown"
        self.max_length = max(16, int(max_length))
        self.local_files_only = bool(local_files_only)
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._labels: list[str] = []

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("transformers sentiment backend requires torch and transformers") from exc
        source = self.model_name
        tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=self.local_files_only)
        model = AutoModelForSequenceClassification.from_pretrained(source, local_files_only=self.local_files_only)
        device_name = os.environ.get("ECHOTRACE_SENTIMENT_DEVICE", "cpu")
        device = torch.device(device_name if device_name != "auto" else ("cuda" if torch.cuda.is_available() else "cpu"))
        model.to(device)
        model.eval()
        id2label = getattr(model.config, "id2label", {}) or {}
        self._labels = [str(id2label.get(index, "")) for index in range(int(model.config.num_labels))]
        self.model_version = str(
            getattr(model.config, "_commit_hash", None)
            or getattr(model.config, "transformers_version", None)
            or "unknown"
        )
        self._tokenizer, self._model, self._torch = tokenizer, model, torch
        self._device = device

    def analyze(self, text: str) -> dict[str, Any]:
        self._load()
        content = str(text or "")
        encoded = self._tokenizer(content, return_tensors="pt", truncation=True, max_length=self.max_length)
        encoded = {key: value.to(self._device) for key, value in encoded.items()}
        with self._torch.no_grad():
            logits = self._model(**encoded).logits
            probabilities = self._torch.softmax(logits, dim=-1)[0].detach().cpu().tolist()
        labels = [_canonical_model_label(raw, index, len(probabilities)) for index, raw in enumerate(self._labels)]
        distribution: dict[str, float] = {}
        for label, probability in zip(labels, probabilities):
            distribution[label] = round(distribution.get(label, 0.0) + float(probability), 6)
        best_index = max(range(len(probabilities)), key=probabilities.__getitem__)
        label = labels[best_index]
        label_zh = {
            "very_negative": "非常负面", "negative": "负面", "neutral": "中性",
            "positive": "正面", "very_positive": "非常正面",
        }.get(label, "不明确")
        return {
            "label": label,
            "label_zh": label_zh,
            "confidence": round(float(probabilities[best_index]), 6),
            "probabilities": distribution,
            "method": "Hugging Face 序列分类模型",
            "backend": self.backend,
            "model_name": self.model_name,
            "model_version": self.model_version,
        }


class AutoSentimentAnalyzer:
    """Prefer the real model and record an explicit rule fallback on failure."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._delegate: SentimentAnalyzer | None = None
        self.fallback_reason: str | None = None
        self._initialization_lock = Lock()

    def analyze(self, text: str) -> dict[str, Any]:
        # The cached analyzer is shared by parallel node workers. Initialize
        # it once so a second worker cannot observe a half-initialized
        # Transformers delegate and bypass the fallback path.
        if self._delegate is None:
            with self._initialization_lock:
                if self._delegate is None:
                    try:
                        delegate = TransformersSentimentAnalyzer(**self._kwargs)
                        result = delegate.analyze(text)
                        self._delegate = delegate
                        return result
                    except Exception as exc:
                        self.fallback_reason = f"{exc.__class__.__name__}: {str(exc).strip()[:180]}"
                        self._delegate = RuleSentimentAnalyzer()
        result = self._delegate.analyze(text)
        if self.fallback_reason:
            result = {**result, "backend": "rule_fallback", "fallback_reason": self.fallback_reason,
                      "model_name": self._kwargs.get("model_path") or self._kwargs.get("model_name") or DEFAULT_SENTIMENT_MODEL}
        return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=8)
def _build_sentiment_analyzer(backend: str, model_name: str, model_path: str, max_length: int, local_files_only: bool) -> SentimentAnalyzer:
    kwargs = {"model_name": model_name, "model_path": model_path or None, "max_length": max_length, "local_files_only": local_files_only}
    if backend == "rule":
        return RuleSentimentAnalyzer()
    if backend == "transformers":
        return TransformersSentimentAnalyzer(**kwargs)
    if backend == "auto":
        return AutoSentimentAnalyzer(**kwargs)
    raise ValueError(f"unsupported sentiment backend: {backend}")


def build_sentiment_analyzer(*, backend: str | None = None, model_name: str | None = None, model_path: str | None = None, max_length: int | None = None, local_files_only: bool | None = None) -> SentimentAnalyzer:
    # Prefer real model inference; auto records an explicit rule fallback when
    # optional dependencies or local model weights are unavailable.
    selected_backend = str(backend or os.environ.get(SENTIMENT_BACKEND_ENV, "auto")).strip().lower()
    selected_model = str(model_name or os.environ.get(SENTIMENT_MODEL_ENV, DEFAULT_SENTIMENT_MODEL)).strip()
    selected_path = str(model_path or os.environ.get(SENTIMENT_MODEL_PATH_ENV, "")).strip()
    selected_length = int(max_length or os.environ.get(SENTIMENT_MAX_LENGTH_ENV, "512"))
    selected_local = _env_bool(SENTIMENT_LOCAL_ONLY_ENV, True) if local_files_only is None else bool(local_files_only)
    return _build_sentiment_analyzer(selected_backend, selected_model, selected_path, selected_length, selected_local)


def analyze_sentiment(text: str, *, analyzer: SentimentAnalyzer | None = None) -> dict[str, Any]:
    if analyzer is not None:
        return analyzer.analyze(text)
    return build_sentiment_analyzer().analyze(text)


def analyze_stance(text: str, *, target: str | None = None) -> dict[str, Any]:
    content = str(text or "")
    support = _label_counts(content, _STANCE_SUPPORT)
    oppose = _label_counts(content, _STANCE_OPPOSE)
    questioning = _label_counts(content, _STANCE_QUESTION)
    if questioning and not support and not oppose:
        label = "questioning"
    elif support > oppose:
        label = "support"
    elif oppose > support:
        label = "oppose"
    elif support or oppose:
        label = "neutral"
    else:
        label = "unclear"
    return {
        "target": target,
        "label": label,
        "label_zh": _STANCE_ZH[label],
        "support_hits": support,
        "oppose_hits": oppose,
        "questioning_hits": questioning,
        "confidence": round(min(0.9, 0.45 + min(support + oppose + questioning, 5) * 0.08), 3) if support + oppose + questioning else 0.2,
        "method": "规则候选，需要结合目标和人工抽检",
    }


def analyze_topics(text: str) -> dict[str, Any]:
    content = str(text or "")
    counts = {topic: _label_counts(content, words) for topic, words in _TOPIC_KEYWORDS.items()}
    matched = [topic for topic, count in counts.items() if count]
    keywords = [word for words in _TOPIC_KEYWORDS.values() for word in words if word in content]
    return {"topics": matched[:3], "keyword_counts": {key: value for key, value in counts.items() if value}, "keywords": list(dict.fromkeys(keywords))}


def analyze_document(document: Any, *, target: str | None = None, sentiment_analyzer: SentimentAnalyzer | None = None) -> list[dict[str, Any]]:
    """Build versioned analysis features for one normalized source document."""
    content = str(getattr(document, "content", "") or "")
    document_id = str(getattr(document, "document_id", ""))
    content_hash = str(getattr(document, "content_hash", "") or _input_hash(content))
    metadata = getattr(document, "metadata", {}) if isinstance(getattr(document, "metadata", None), dict) else {}
    mentions = extract_time_mentions(content, reference_time=getattr(document, "published_at", None), timezone_name=str(metadata.get("timezone") or "Asia/Shanghai"))
    entities = extract_entities(content, known_entities=metadata.get("entities", []) if isinstance(metadata.get("entities"), list) else [])
    analyzer = sentiment_analyzer or build_sentiment_analyzer()
    sentiment = analyze_sentiment(content, analyzer=analyzer)
    common = {
        "document_id": document_id,
        "input_hash": content_hash,
        "factuality": ANALYSIS_FEATURE_FACTUALITY,
    }
    rule_meta = {
        "backend": "rules",
        "model_name": "",
        "model_version": "",
    }
    features = [
        {**common, **rule_meta, "feature_type": "time_mentions", "value": {"mentions": [item.to_dict() for item in mentions], "published_time": getattr(document, "published_at", None).isoformat() if getattr(document, "published_at", None) else None, "retrieved_time": getattr(document, "fetched_at", None).isoformat() if getattr(document, "fetched_at", None) else None, "method": "规则抽取"}},
        {**common, **rule_meta, "feature_type": "entity_mentions", "value": {"entities": [item.to_dict() for item in entities], "method": "规则抽取"}},
        {**common, "feature_type": "sentiment", "value": sentiment},
        {**common, **rule_meta, "feature_type": "stance", "value": {**analyze_stance(content, target=target), "backend": "rules"}},
        {**common, **rule_meta, "feature_type": "topic", "value": {**analyze_topics(content), "backend": "rules", "method": "关键词候选"}},
    ]
    for feature in features:
        identity = f"{document_id}:{feature['feature_type']}:{content_hash}"
        feature["feature_id"] = f"feature_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
    return features


__all__ = [
    "ANALYSIS_FEATURE_FACTUALITY",
    "ANALYSIS_MODEL_NAME",
    "ANALYSIS_MODEL_VERSION",
    "DEFAULT_SENTIMENT_MODEL",
    "EntityMention",
    "AutoSentimentAnalyzer",
    "RuleSentimentAnalyzer",
    "TransformersSentimentAnalyzer",
    "SentimentAnalyzer",
    "build_sentiment_analyzer",
    "TimeMention",
    "analyze_document",
    "analyze_sentiment",
    "analyze_stance",
    "analyze_topics",
    "extract_entities",
    "extract_event_time_window",
    "extract_time_mentions",
]
