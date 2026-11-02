"""Node-driven Trace contracts and built-in investigation templates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping

from .reporting import AnalysisDataset, MetricObservation
from .errors import TraceContractError


NODE_TEMPLATE_VERSION = "trace-template.v1"
MAX_NODE_REVISIONS = 3
REVIEW_RUBRIC_AXES = ("coverage", "evidence", "synthesis")
REVIEW_ROUND_GUIDANCE: dict[int, dict[str, Any]] = {
    1: {
        "axis": "coverage",
        "label": "覆盖面与视角",
        "objective": "补齐 NodeSpec 要求的主体、时间、行动、结果、影响范围和不同立场，形成多角度信息版图。",
        "feedback_fields": ("missing_aspects", "next_research_tasks", "rewrite_instructions"),
    },
    2: {
        "axis": "evidence",
        "label": "证据深度与交叉印证",
        "objective": "核对每条主张的原始片段、来源独立性、时间和范围边界，补足支持、反驳或限定材料。",
        "feedback_fields": ("evidence_followups", "unclear_claims", "rewrite_instructions"),
    },
    3: {
        "axis": "synthesis",
        "label": "综合结论与编辑定稿",
        "objective": "合并重复主张，重组结论逻辑，让读者直接看懂主体、行动、结果和材料边界。",
        "feedback_fields": ("redundant_claims", "unclear_claims", "rewrite_instructions"),
    },
}


class TraceEdgeRelation(StrEnum):
    PRECEDES = "precedes"
    CAUSES = "causes"
    MAY_CONTRIBUTE = "may_contribute"
    PROPAGATES = "propagates"
    RESPONDS_TO = "responds_to"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    SAME_INCIDENT_AS = "same_incident_as"
    UNKNOWN_RELATION = "unknown_relation"


class NodeMediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class NodeEvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    MENTIONS = "mentions"


class NodeReviewStatus(StrEnum):
    REVISE = "revise"
    FINALIZE = "finalize"


def _text(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TraceContractError(f"{field_name} cannot be empty")
    return result


def _strings(values: Any, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TraceContractError(f"{field_name} must be a list of strings")
    result = tuple(str(item).strip() for item in values)
    if any(not item for item in result):
        raise TraceContractError(f"{field_name} cannot contain empty values")
    if len(set(result)) != len(result):
        raise TraceContractError(f"{field_name} cannot contain duplicates")
    if required and not result:
        raise TraceContractError(f"{field_name} cannot be empty")
    return result


def _review_rubric(value: Any, field_name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise TraceContractError(f"{field_name} must be an object with coverage, evidence and synthesis axes")
    if set(value) != set(REVIEW_RUBRIC_AXES):
        raise TraceContractError(f"{field_name} must contain exactly {REVIEW_RUBRIC_AXES}")
    return {
        axis: _strings(value[axis], f"{field_name}.{axis}", required=True)
        for axis in REVIEW_RUBRIC_AXES
    }


def _datetime(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TraceContractError(f"{field_name} must be an ISO datetime") from exc
    raise TraceContractError(f"{field_name} must be an ISO datetime or null")


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceContractError("contract payload must be an object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class NodeMediaPolicy:
    """Whether a node may include media and why it would be useful."""

    allowed_types: tuple[str, ...] = (NodeMediaType.IMAGE.value, NodeMediaType.VIDEO.value)
    purpose: str = ""

    def __post_init__(self) -> None:
        allowed = _strings(self.allowed_types, "media_policy.allowed_types", required=True)
        invalid = set(allowed) - {item.value for item in NodeMediaType}
        if invalid:
            raise TraceContractError("media_policy.allowed_types contains unsupported media types")
        object.__setattr__(self, "allowed_types", allowed)
        object.__setattr__(self, "purpose", str(self.purpose or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeMediaPolicy":
        return cls(**_payload(payload))


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    from_slot_id: str
    to_slot_id: str
    relation: TraceEdgeRelation | str
    label_goal: str
    requires_explicit_support: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_slot_id", _text(self.from_slot_id, "edge_spec.from_slot_id"))
        object.__setattr__(self, "to_slot_id", _text(self.to_slot_id, "edge_spec.to_slot_id"))
        if self.from_slot_id == self.to_slot_id:
            raise TraceContractError("edge_spec cannot connect a slot to itself")
        try:
            relation = self.relation if isinstance(self.relation, TraceEdgeRelation) else TraceEdgeRelation(str(self.relation))
        except ValueError as exc:
            raise TraceContractError(f"unsupported edge_spec relation: {self.relation}") from exc
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "label_goal", _text(self.label_goal, "edge_spec.label_goal"))
        if not isinstance(self.requires_explicit_support, bool):
            raise TraceContractError("edge_spec.requires_explicit_support must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EdgeSpec":
        return cls(**_payload(payload))


@dataclass(frozen=True, slots=True)
class NodeSpec:
    slot_id: str
    order: int
    title: str
    goal: str
    aspects: tuple[str, ...]
    claim_types: tuple[str, ...]
    preferred_sources: tuple[str, ...]
    research_questions: tuple[str, ...]
    review_rubric: dict[str, tuple[str, ...]]
    allow_multiple_instances: bool = False
    media_policy: NodeMediaPolicy = NodeMediaPolicy()
    depends_on_slot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot_id", _text(self.slot_id, "node_spec.slot_id"))
        if not isinstance(self.order, int) or self.order < 1:
            raise TraceContractError("node_spec.order must be a positive integer")
        object.__setattr__(self, "title", _text(self.title, "node_spec.title"))
        object.__setattr__(self, "goal", _text(self.goal, "node_spec.goal"))
        for field_name in ("aspects", "claim_types", "research_questions"):
            object.__setattr__(self, field_name, _strings(getattr(self, field_name), f"node_spec.{field_name}", required=True))
        object.__setattr__(self, "review_rubric", _review_rubric(self.review_rubric, "node_spec.review_rubric"))
        object.__setattr__(self, "preferred_sources", _strings(self.preferred_sources, "node_spec.preferred_sources"))
        object.__setattr__(self, "depends_on_slot_ids", _strings(self.depends_on_slot_ids, "node_spec.depends_on_slot_ids"))
        if not isinstance(self.allow_multiple_instances, bool):
            raise TraceContractError("node_spec.allow_multiple_instances must be boolean")
        policy = self.media_policy if isinstance(self.media_policy, NodeMediaPolicy) else NodeMediaPolicy.from_dict(self.media_policy)
        object.__setattr__(self, "media_policy", policy)

    def to_dict(self) -> dict[str, Any]:
        value = _jsonable(asdict(self))
        value["media_policy"] = self.media_policy.to_dict()
        return value

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeSpec":
        value = _payload(payload)
        value["review_rubric"] = {
            axis: tuple(items)
            for axis, items in dict(value.get("review_rubric") or {}).items()
        }
        if isinstance(value.get("media_policy"), Mapping):
            value["media_policy"] = NodeMediaPolicy.from_dict(value["media_policy"])
        return cls(**value)


@dataclass(frozen=True, slots=True)
class TraceTemplate:
    template_id: str
    template_version: str
    investigation_type: str
    display_name: str
    purpose: str
    node_specs: tuple[NodeSpec, ...]
    edge_specs: tuple[EdgeSpec, ...]
    language: str = "zh-CN"
    max_node_revisions: int = MAX_NODE_REVISIONS

    def __post_init__(self) -> None:
        for field_name in ("template_id", "template_version", "investigation_type", "display_name", "purpose", "language"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), f"template.{field_name}"))
        if not self.node_specs:
            raise TraceContractError("template.node_specs cannot be empty")
        nodes = tuple(item if isinstance(item, NodeSpec) else NodeSpec.from_dict(item) for item in self.node_specs)
        edges = tuple(item if isinstance(item, EdgeSpec) else EdgeSpec.from_dict(item) for item in self.edge_specs)
        object.__setattr__(self, "node_specs", nodes)
        object.__setattr__(self, "edge_specs", edges)
        if self.max_node_revisions != MAX_NODE_REVISIONS:
            raise TraceContractError(f"template.max_node_revisions must be {MAX_NODE_REVISIONS}")
        slot_ids = [item.slot_id for item in nodes]
        if len(set(slot_ids)) != len(slot_ids):
            raise TraceContractError("template node slot_id values must be unique")
        orders = [item.order for item in nodes]
        if sorted(orders) != list(range(1, len(nodes) + 1)):
            raise TraceContractError("template node orders must be contiguous and start at 1")
        known_slots = set(slot_ids)
        for node in nodes:
            if not set(node.depends_on_slot_ids).issubset(known_slots):
                raise TraceContractError(f"node_spec dependencies reference an unknown slot: {node.slot_id}")
            if node.slot_id in node.depends_on_slot_ids:
                raise TraceContractError(f"node_spec cannot depend on itself: {node.slot_id}")
        for edge in edges:
            if edge.from_slot_id not in known_slots or edge.to_slot_id not in known_slots:
                raise TraceContractError("edge_spec references an unknown slot")
        self._validate_dependencies(nodes)

    @staticmethod
    def _validate_dependencies(nodes: tuple[NodeSpec, ...]) -> None:
        remaining = {item.slot_id: set(item.depends_on_slot_ids) for item in nodes}
        resolved: set[str] = set()
        while remaining:
            ready = [slot_id for slot_id, deps in remaining.items() if deps.issubset(resolved)]
            if not ready:
                raise TraceContractError("template node dependencies contain a cycle")
            for slot_id in ready:
                resolved.add(slot_id)
                remaining.pop(slot_id)

    def node_spec(self, slot_id: str) -> NodeSpec:
        for node in self.node_specs:
            if node.slot_id == slot_id:
                return node
        raise TraceContractError(f"unknown template slot_id: {slot_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "investigation_type": self.investigation_type,
            "display_name": self.display_name,
            "purpose": self.purpose,
            "node_specs": [item.to_dict() for item in self.node_specs],
            "edge_specs": [item.to_dict() for item in self.edge_specs],
            "language": self.language,
            "max_node_revisions": self.max_node_revisions,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TraceTemplate":
        value = _payload(payload)
        value["node_specs"] = [NodeSpec.from_dict(item) for item in value.get("node_specs", [])]
        value["edge_specs"] = [EdgeSpec.from_dict(item) for item in value.get("edge_specs", [])]
        return cls(**value)


@dataclass(slots=True)
class NodeClaim:
    claim_id: str
    claim_type: str
    text: str
    subject: str = ""
    predicate: str = ""
    object_value: str = ""
    time_scope: str = ""
    source_document_ids: list[str] = None  # type: ignore[assignment]
    evidence_ids: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.claim_id = _text(self.claim_id, "claim.claim_id")
        self.claim_type = _text(self.claim_type, "claim.claim_type")
        self.text = _text(self.text, "claim.text")
        self.subject = str(self.subject or "").strip()
        self.predicate = str(self.predicate or "").strip()
        self.object_value = str(self.object_value or "").strip()
        self.time_scope = str(self.time_scope or "").strip()
        self.source_document_ids = list(_strings(self.source_document_ids or [], "claim.source_document_ids"))
        self.evidence_ids = list(_strings(self.evidence_ids or [], "claim.evidence_ids", required=True))

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeClaim":
        value = _payload(payload)
        allowed = {
            "claim_id", "claim_type", "text", "subject", "predicate", "object_value",
            "time_scope", "source_document_ids", "evidence_ids",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise TraceContractError(f"claim contains unsupported fields: {', '.join(unknown)}")
        return cls(**value)


@dataclass(slots=True)
class NodeEvidence:
    evidence_id: str
    claim_id: str
    document_id: str
    excerpt: str
    locator: dict[str, Any]
    relation: NodeEvidenceRelation | str = NodeEvidenceRelation.SUPPORTS

    def __post_init__(self) -> None:
        self.evidence_id = _text(self.evidence_id, "evidence.evidence_id")
        self.claim_id = _text(self.claim_id, "evidence.claim_id")
        self.document_id = _text(self.document_id, "evidence.document_id")
        self.excerpt = _text(self.excerpt, "evidence.excerpt")
        if not isinstance(self.locator, dict):
            raise TraceContractError("evidence.locator must be an object")
        if not self.locator:
            raise TraceContractError("evidence.locator cannot be empty")
        try:
            self.relation = self.relation if isinstance(self.relation, NodeEvidenceRelation) else NodeEvidenceRelation(str(self.relation))
        except ValueError as exc:
            raise TraceContractError(f"unsupported evidence relation: {self.relation}") from exc

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeEvidence":
        value = _payload(payload)
        allowed = {"evidence_id", "claim_id", "document_id", "excerpt", "locator", "relation"}
        extra = sorted(set(value) - allowed)
        if extra:
            raise TraceContractError(
                "evidence contains unsupported fields: " + ", ".join(extra)
                + "; allowed fields are " + ", ".join(sorted(allowed))
            )
        return cls(**value)


@dataclass(slots=True)
class NodeMedia:
    media_id: str
    media_type: NodeMediaType | str
    document_id: str
    caption: str
    why_relevant: str
    source_url: str = ""
    object_key: str = ""
    published_at: datetime | None = None
    captured_at: datetime | None = None

    def __post_init__(self) -> None:
        self.media_id = _text(self.media_id, "media.media_id")
        try:
            self.media_type = self.media_type if isinstance(self.media_type, NodeMediaType) else NodeMediaType(str(self.media_type))
        except ValueError as exc:
            raise TraceContractError(f"unsupported media type: {self.media_type}") from exc
        self.document_id = _text(self.document_id, "media.document_id")
        self.caption = _text(self.caption, "media.caption")
        self.why_relevant = _text(self.why_relevant, "media.why_relevant")
        self.source_url = str(self.source_url or "").strip()
        self.object_key = str(self.object_key or "").strip()
        if bool(self.source_url) == bool(self.object_key):
            raise TraceContractError("media requires exactly one of source_url or object_key")
        self.published_at = _datetime(self.published_at, "media.published_at")
        self.captured_at = _datetime(self.captured_at, "media.captured_at")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeMedia":
        value = _payload(payload)
        allowed = {
            "media_id", "media_type", "document_id", "caption", "why_relevant",
            "source_url", "object_key", "published_at", "captured_at",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            detail = "; use media_type for the media type" if "type" in unknown else ""
            raise TraceContractError(f"unsupported media fields: {', '.join(unknown)}{detail}")
        required = {"media_id", "media_type", "document_id", "caption", "why_relevant"}
        missing = sorted(required - set(value))
        if missing:
            raise TraceContractError(f"media is missing required fields: {', '.join(missing)}")
        return cls(**value)


@dataclass(slots=True)
class ConclusionIR:
    """Model-produced presentation structure for one node conclusion.

    The IR is a reader-facing editorial rendering of the node conclusion.
    It may reorganize and polish the conclusion, but never add factual claims.
    """

    schema_version: str
    blocks: list[dict[str, Any]]
    source_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "conclusion-ir.v1":
            raise TraceContractError("unsupported conclusion IR schema version")
        if not isinstance(self.blocks, list) or not self.blocks:
            raise TraceContractError("conclusion_ir.blocks cannot be empty")
        if len(self.blocks) > 96:
            raise TraceContractError("conclusion_ir.blocks cannot contain more than 96 blocks")
        self.source_hash = str(self.source_hash or "").strip()
        normalized_blocks: list[dict[str, Any]] = []
        for index, block in enumerate(self.blocks):
            if not isinstance(block, Mapping):
                raise TraceContractError(f"conclusion_ir.blocks[{index}] must be an object")
            value = dict(block)
            # Older persisted IRs carried source_fragment as a forced verbatim
            # provenance field. It is not part of the reader-facing contract.
            value.pop("source_fragment", None)
            block_type = str(value.get("type") or "").strip()
            if block_type == "heading":
                _validate_ir_heading(value, index)
            elif block_type == "paragraph":
                _validate_ir_text_block(value, index, "paragraph")
            elif block_type == "list":
                _validate_ir_list(value, index)
            elif block_type == "table":
                _validate_ir_table(value, index)
            else:
                raise TraceContractError(
                    f"conclusion_ir.blocks[{index}].type must be heading, paragraph, list, or table"
                )
            normalized_blocks.append(value)
        self.blocks = normalized_blocks

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "blocks": [_jsonable(dict(block)) for block in self.blocks],
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConclusionIR":
        value = _payload(payload)
        allowed = {"schema_version", "blocks", "source_hash"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise TraceContractError(f"conclusion_ir contains unsupported fields: {', '.join(unknown)}")
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            blocks=list(value.get("blocks") or []),
            source_hash=str(value.get("source_hash") or ""),
        )


def _validate_ir_text_block(value: Mapping[str, Any], index: int, block_type: str) -> None:
    allowed = {"type", "text"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TraceContractError(f"conclusion_ir.blocks[{index}] contains unsupported fields: {', '.join(unknown)}")
    if not str(value.get("text") or "").strip():
        raise TraceContractError(f"conclusion_ir.blocks[{index}].text is required for {block_type}")
def _validate_ir_heading(value: Mapping[str, Any], index: int) -> None:
    allowed = {"type", "level", "text"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TraceContractError(f"conclusion_ir.blocks[{index}] contains unsupported fields: {', '.join(unknown)}")
    if not str(value.get("text") or "").strip():
        raise TraceContractError(f"conclusion_ir.blocks[{index}].text is required for heading")
    level = value.get("level")
    if type(level) is not int or level not in {1, 2, 3}:
        raise TraceContractError(f"conclusion_ir.blocks[{index}].level must be 1, 2, or 3")


def _validate_ir_list(value: Mapping[str, Any], index: int) -> None:
    allowed = {"type", "ordered", "items"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TraceContractError(f"conclusion_ir.blocks[{index}] contains unsupported fields: {', '.join(unknown)}")
    if type(value.get("ordered")) is not bool:
        raise TraceContractError(f"conclusion_ir.blocks[{index}].ordered must be boolean")
    items = value.get("items")
    if not isinstance(items, list) or not items or any(not str(item).strip() for item in items):
        raise TraceContractError(f"conclusion_ir.blocks[{index}].items must be a non-empty string array")
    if len(items) > 64:
        raise TraceContractError(f"conclusion_ir.blocks[{index}].items cannot contain more than 64 items")
def _validate_ir_table(value: Mapping[str, Any], index: int) -> None:
    allowed = {"type", "title", "columns", "rows"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TraceContractError(f"conclusion_ir.blocks[{index}] contains unsupported fields: {', '.join(unknown)}")
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or len(columns) < 2 or any(not str(item).strip() for item in columns):
        raise TraceContractError(f"conclusion_ir.blocks[{index}].columns must contain at least two strings")
    if not isinstance(rows, list) or not rows:
        raise TraceContractError(f"conclusion_ir.blocks[{index}].rows cannot be empty")
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns) or any(not str(item).strip() for item in row):
            raise TraceContractError(
                f"conclusion_ir.blocks[{index}].rows[{row_index}] must match the column count"
            )


@dataclass(slots=True)
class NodeDraft:
    node_id: str
    slot_id: str
    revision: int
    claims: list[NodeClaim]
    evidence: list[NodeEvidence]
    conclusion: str
    media: list[NodeMedia]
    metrics: list[MetricObservation] = field(default_factory=list)
    datasets: list[AnalysisDataset] = field(default_factory=list)
    analysis_features: list[dict[str, Any]] = field(default_factory=list)
    conclusion_ir: ConclusionIR | None = None

    def __post_init__(self) -> None:
        self.node_id = _text(self.node_id, "draft.node_id")
        self.slot_id = _text(self.slot_id, "draft.slot_id")
        if not isinstance(self.revision, int) or not 0 <= self.revision <= MAX_NODE_REVISIONS:
            raise TraceContractError(f"draft.revision must be between 0 and {MAX_NODE_REVISIONS}")
        self.claims = [item if isinstance(item, NodeClaim) else NodeClaim.from_dict(item) for item in self.claims]
        self.evidence = [item if isinstance(item, NodeEvidence) else NodeEvidence.from_dict(item) for item in self.evidence]
        self.media = [item if isinstance(item, NodeMedia) else NodeMedia.from_dict(item) for item in self.media]
        self.metrics = [item if isinstance(item, MetricObservation) else MetricObservation.from_dict(item) for item in (self.metrics or [])]
        self.datasets = [item if isinstance(item, AnalysisDataset) else AnalysisDataset.from_dict(item) for item in (self.datasets or [])]
        if self.analysis_features is None:
            self.analysis_features = []
        if not isinstance(self.analysis_features, list):
            raise TraceContractError("draft.analysis_features must be a list")
        if not all(isinstance(item, dict) for item in self.analysis_features):
            raise TraceContractError("draft.analysis_features must contain objects")
        if self.conclusion_ir is not None and not isinstance(self.conclusion_ir, ConclusionIR):
            self.conclusion_ir = ConclusionIR.from_dict(self.conclusion_ir)
        feature_ids = {
            str(item.get("feature_id") or "").strip()
            for item in self.analysis_features
            if str(item.get("feature_id") or "").strip()
        }
        self.conclusion = _text(self.conclusion, "draft.conclusion")
        if self.conclusion_ir is not None:
            expected_hash = sha256(self.conclusion.encode("utf-8")).hexdigest()
            if self.conclusion_ir.source_hash != expected_hash:
                raise TraceContractError("draft.conclusion_ir source_hash does not match draft.conclusion")
        if not self.claims:
            raise TraceContractError("draft.claims cannot be empty")
        if not self.evidence:
            raise TraceContractError("draft.evidence cannot be empty")
        claim_ids = {item.claim_id for item in self.claims}
        evidence_ids = {item.evidence_id for item in self.evidence}
        evidence_documents = {item.document_id for item in self.evidence}
        dataset_citations = claim_ids | evidence_ids | evidence_documents
        metric_ids = {item.metric_id for item in self.metrics}
        dataset_ids = {item.dataset_id for item in self.datasets}
        if len(claim_ids) != len(self.claims) or len(evidence_ids) != len(self.evidence):
            raise TraceContractError("draft claim_id and evidence_id values must be unique")
        for claim in self.claims:
            if not set(claim.evidence_ids).issubset(evidence_ids):
                raise TraceContractError(f"claim references unknown evidence: {claim.claim_id}")
            if not claim.source_document_ids:
                raise TraceContractError(f"claim requires source_document_ids: {claim.claim_id}")
            linked_documents = {
                item.document_id for item in self.evidence
                if item.evidence_id in set(claim.evidence_ids)
            }
            if not set(claim.source_document_ids).issubset(linked_documents):
                raise TraceContractError(f"claim source_document_ids are not covered by its evidence: {claim.claim_id}")
        for evidence in self.evidence:
            if evidence.claim_id not in claim_ids:
                raise TraceContractError(f"evidence references unknown claim: {evidence.evidence_id}")
        if len(metric_ids) != len(self.metrics) or len(dataset_ids) != len(self.datasets):
            raise TraceContractError("draft metric_id and dataset_id values must be unique")
        for metric in self.metrics:
            if metric.evidence_id not in evidence_ids:
                raise TraceContractError(f"metric references unknown evidence: {metric.metric_id}")
            if metric.source_document_id not in {item.document_id for item in self.evidence if item.evidence_id == metric.evidence_id}:
                raise TraceContractError(f"metric source document is not covered by evidence: {metric.metric_id}")
        for dataset in self.datasets:
            if not set(dataset.input_metric_ids).issubset(metric_ids):
                raise TraceContractError(f"dataset references unknown metric: {dataset.dataset_id}")
            if not set(dataset.source_feature_ids).issubset(feature_ids):
                raise TraceContractError(f"dataset references unknown analysis feature: {dataset.dataset_id}")
            if not set(dataset.citation_ids).issubset(dataset_citations):
                raise TraceContractError(f"dataset references unknown source citation: {dataset.dataset_id}")
            if "citation_ids" in dataset.columns:
                for index, row in enumerate(dataset.rows):
                    citations = row.get("citation_ids")
                    if not isinstance(citations, list) or not citations:
                        raise TraceContractError(f"dataset row requires citation_ids: {dataset.dataset_id}[{index}]")
                    if not set(citations).issubset(dataset_citations):
                        raise TraceContractError(f"dataset row references unknown source citation: {dataset.dataset_id}[{index}]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "slot_id": self.slot_id,
            "revision": self.revision,
            "claims": [item.to_dict() for item in self.claims],
            "evidence": [item.to_dict() for item in self.evidence],
            "conclusion": self.conclusion,
            "media": [item.to_dict() for item in self.media],
            "metrics": [item.to_dict() for item in self.metrics],
            "datasets": [item.to_dict() for item in self.datasets],
            "analysis_features": list(self.analysis_features),
            "conclusion_ir": self.conclusion_ir.to_dict() if self.conclusion_ir else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeDraft":
        value = _payload(payload)
        value["claims"] = [NodeClaim.from_dict(item) for item in value.get("claims", [])]
        value["evidence"] = [NodeEvidence.from_dict(item) for item in value.get("evidence", [])]
        value["media"] = [NodeMedia.from_dict(item) for item in value.get("media", [])]
        value["metrics"] = [MetricObservation.from_dict(item) for item in value.get("metrics", [])]
        value["datasets"] = [AnalysisDataset.from_dict(item) for item in value.get("datasets", [])]
        return cls(**value)


@dataclass(slots=True)
class NodeReview:
    review_id: str
    node_id: str
    slot_id: str
    revision: int
    status: NodeReviewStatus | str
    covered_aspects: list[str]
    missing_aspects: list[str]
    redundant_claims: list[str]
    unclear_claims: list[str]
    evidence_followups: list[str]
    rewrite_instructions: list[str]
    next_research_tasks: list[str]
    final_editor_note: str

    def __post_init__(self) -> None:
        self.review_id = _text(self.review_id, "review.review_id")
        self.node_id = _text(self.node_id, "review.node_id")
        self.slot_id = _text(self.slot_id, "review.slot_id")
        if not isinstance(self.revision, int) or not 1 <= self.revision <= MAX_NODE_REVISIONS:
            raise TraceContractError(f"review.revision must be between 1 and {MAX_NODE_REVISIONS}")
        try:
            self.status = self.status if isinstance(self.status, NodeReviewStatus) else NodeReviewStatus(str(self.status))
        except ValueError as exc:
            raise TraceContractError(f"unsupported review status: {self.status}") from exc
        expected = NodeReviewStatus.FINALIZE if self.revision == MAX_NODE_REVISIONS else NodeReviewStatus.REVISE
        if self.status != expected:
            raise TraceContractError(f"review status for revision {self.revision} must be {expected.value}")
        for field_name in (
            "covered_aspects", "missing_aspects", "redundant_claims", "unclear_claims",
            "evidence_followups", "rewrite_instructions", "next_research_tasks",
        ):
            setattr(self, field_name, list(_strings(getattr(self, field_name), f"review.{field_name}")))
        self.final_editor_note = _text(self.final_editor_note, "review.final_editor_note")
        if self.status == NodeReviewStatus.FINALIZE and (self.missing_aspects or self.evidence_followups or self.next_research_tasks):
            raise TraceContractError("final review cannot retain missing aspects or follow-up tasks")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeReview":
        return cls(**_payload(payload))


@dataclass(slots=True)
class NodeRevision:
    node_id: str
    slot_id: str
    revision: int
    draft: NodeDraft
    review: NodeReview | None = None
    parent_revision: int | None = None

    def __post_init__(self) -> None:
        self.node_id = _text(self.node_id, "revision.node_id")
        self.slot_id = _text(self.slot_id, "revision.slot_id")
        if not isinstance(self.revision, int) or not 0 <= self.revision <= MAX_NODE_REVISIONS:
            raise TraceContractError(f"revision.revision must be between 0 and {MAX_NODE_REVISIONS}")
        self.draft = self.draft if isinstance(self.draft, NodeDraft) else NodeDraft.from_dict(self.draft)
        if self.draft.node_id != self.node_id or self.draft.slot_id != self.slot_id or self.draft.revision != self.revision:
            raise TraceContractError("revision draft identity does not match revision")
        if self.revision == 0:
            if self.review is not None or self.parent_revision is not None:
                raise TraceContractError("initial node revision cannot have review or parent_revision")
        else:
            self.review = self.review if isinstance(self.review, NodeReview) else NodeReview.from_dict(self.review or {})
            if self.review.node_id != self.node_id or self.review.slot_id != self.slot_id or self.review.revision != self.revision:
                raise TraceContractError("revision review identity does not match revision")
            if self.parent_revision != self.revision - 1:
                raise TraceContractError("revision parent_revision must point to the previous revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "slot_id": self.slot_id,
            "revision": self.revision,
            "draft": self.draft.to_dict(),
            "review": self.review.to_dict() if self.review else None,
            "parent_revision": self.parent_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeRevision":
        value = _payload(payload)
        value["draft"] = NodeDraft.from_dict(value["draft"])
        if value.get("review") is not None:
            value["review"] = NodeReview.from_dict(value["review"])
        return cls(**value)


@dataclass(slots=True)
class FrozenTraceNode:
    node_id: str
    slot_id: str
    draft: NodeDraft
    revisions: list[NodeRevision]
    frozen_at: datetime | None = None

    def __post_init__(self) -> None:
        self.node_id = _text(self.node_id, "frozen_node.node_id")
        self.slot_id = _text(self.slot_id, "frozen_node.slot_id")
        self.draft = self.draft if isinstance(self.draft, NodeDraft) else NodeDraft.from_dict(self.draft)
        self.revisions = [item if isinstance(item, NodeRevision) else NodeRevision.from_dict(item) for item in self.revisions]
        self.frozen_at = _datetime(self.frozen_at, "frozen_node.frozen_at")
        if self.draft.node_id != self.node_id or self.draft.slot_id != self.slot_id or self.draft.revision != MAX_NODE_REVISIONS:
            raise TraceContractError("frozen node must contain the third revision draft")
        if len(self.revisions) != MAX_NODE_REVISIONS + 1:
            raise TraceContractError("frozen node must retain initial draft and exactly three revisions")
        expected = list(range(MAX_NODE_REVISIONS + 1))
        actual = [item.revision for item in self.revisions]
        if actual != expected:
            raise TraceContractError("frozen node revisions must be ordered from 0 through 3")
        if any(item.node_id != self.node_id or item.slot_id != self.slot_id for item in self.revisions):
            raise TraceContractError("frozen node revision identity does not match frozen node")
        if self.revisions[-1].review is None or self.revisions[-1].review.status != NodeReviewStatus.FINALIZE:
            raise TraceContractError("frozen node requires a final third-round review")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "node_id": self.node_id,
            "slot_id": self.slot_id,
            "draft": self.draft.to_dict(),
            "revisions": [item.to_dict() for item in self.revisions],
            "frozen_at": self.frozen_at,
        }
        return _jsonable(value)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FrozenTraceNode":
        value = _payload(payload)
        value["draft"] = NodeDraft.from_dict(value["draft"])
        value["revisions"] = [NodeRevision.from_dict(item) for item in value.get("revisions", [])]
        return cls(**value)


def _spec(
    slot_id: str,
    order: int,
    title: str,
    goal: str,
    aspects: tuple[str, ...],
    claim_types: tuple[str, ...],
    preferred_sources: tuple[str, ...],
    research_questions: tuple[str, ...],
    review_rubric: tuple[str, ...],
    *,
    depends_on: tuple[str, ...] = (),
    media_purpose: str = "仅在能帮助用户识别现场、文件或处置结果时保留。",
) -> NodeSpec:
    rubric = {
        "coverage": (*review_rubric, "第一轮必须指出缺少的主体、方面或视角，并给出可执行的补充检索方向。"),
        "evidence": (*review_rubric, "第二轮必须指出需要核实的主张、原始片段、独立来源或边界修正。"),
        "synthesis": (*review_rubric, "第三轮必须给出主张排序、合并/拆分和结论重组动作，不再创建开放检索任务。"),
    }
    return NodeSpec(
        slot_id=slot_id,
        order=order,
        title=title,
        goal=goal,
        aspects=aspects,
        claim_types=claim_types,
        preferred_sources=preferred_sources,
        research_questions=research_questions,
        review_rubric=rubric,
        media_policy=NodeMediaPolicy(purpose=media_purpose),
        depends_on_slot_ids=depends_on,
    )


def review_round_context(node_spec: NodeSpec, round_no: int) -> dict[str, Any]:
    """Return the fixed editorial focus and node-specific rubric for one round."""
    try:
        guidance = REVIEW_ROUND_GUIDANCE[round_no]
    except KeyError as exc:
        raise TraceContractError(f"unsupported review round: {round_no}") from exc
    axis = str(guidance["axis"])
    return {
        "round": round_no,
        "axis": axis,
        "label": guidance["label"],
        "objective": guidance["objective"],
        "rubric": list(node_spec.review_rubric[axis]),
        "feedback_fields": list(guidance["feedback_fields"]),
    }


def _sequential_edges(nodes: tuple[NodeSpec, ...]) -> tuple[EdgeSpec, ...]:
    return tuple(
        EdgeSpec(
            from_slot_id=left.slot_id,
            to_slot_id=right.slot_id,
            relation=TraceEdgeRelation.PRECEDES,
            label_goal="说明调查脉络中的先后关系，不自动表示因果。",
        )
        for left, right in zip(nodes, nodes[1:])
    )


def _template(investigation_type: str, display_name: str, purpose: str, nodes: tuple[NodeSpec, ...]) -> TraceTemplate:
    return TraceTemplate(
        template_id=f"{investigation_type}.trace",
        template_version=NODE_TEMPLATE_VERSION,
        investigation_type=investigation_type,
        display_name=display_name,
        purpose=purpose,
        node_specs=nodes,
        edge_specs=_sequential_edges(nodes),
    )


_PUBLIC_EVENT_NODES = (
    _spec("origin_early_signals", 1, "起点与早期信号", "最早出现了什么信号，何时何地由谁观察或发布？", ("最早材料", "初始描述", "时间", "地点", "主体", "早期不确定性"), ("observation", "attribution", "uncertainty"), ("official", "news", "firsthand"), ("最早可定位材料是什么？", "谁在何时何地首先描述了什么？"), ("是否明确区分事件时间与来源发布时间？", "是否保留早期不确定性？")),
    _spec("event_scope", 2, "事件事实与范围", "哪些事实已经形成较清晰的共同认识，范围是什么？", ("发生了什么", "涉及主体", "影响范围", "官方确认范围", "事实边界"), ("fact", "scope", "attribution"), ("official", "news", "firsthand"), ("哪些事实被多个独立材料共同支持？", "哪些范围仍不能确认？"), ("主张是否互相补充而非重复？", "是否清楚区分材料陈述与事实结论?"), depends_on=("origin_early_signals",)),
    _spec("spread_amplification", 3, "传播与发酵", "信息如何扩散，哪些平台或来源簇推动了讨论？", ("首次扩散", "传播波次", "观点分化", "传播指标", "来源依赖"), ("spread", "sample", "viewpoint", "metric"), ("news", "social", "discourse"), ("信息经过哪些可观察的传播节点？", "哪些传播数据只是样本内信号？"), ("是否把传播量与事实证明分开？", "是否说明样本范围和来源依赖？")),
    _spec("turning_point", 4, "关键转折", "叙事、事实认识或处置状态发生了什么可观察变化？", ("前状态", "后状态", "触发材料", "声明修订", "争议变化"), ("change", "statement", "comparison"), ("official", "news", "firsthand"), ("什么材料显示状态发生变化？", "转折是事实变化、叙事变化还是处置变化？"), ("是否说明转折前后状态？", "是否避免把时间先后写成因果？"), depends_on=("event_scope",)),
    _spec("response_treatment", 5, "响应与处置", "不同主体分别做了什么，过程和结果如何？", ("监管行动", "机构行动", "企业行动", "平台行动", "处置结果"), ("action", "response", "result"), ("official", "institution", "company", "platform"), ("谁采取了什么行动？", "行动产生了什么结果，哪些结果仍待确认？"), ("用户能否一眼看出主体、行动和结果？", "是否有不同主体和不同方面的互补主张？"), depends_on=("event_scope", "turning_point")),
    _spec("current_outcome", 6, "当前结果", "截至调查时已经确定了什么，当前状态是什么？", ("已完成事项", "正在进行事项", "现实影响", "剩余问题"), ("status", "result", "boundary"), ("official", "news", "firsthand"), ("截至调查时哪些事项已经完成？", "当前状态和剩余问题是什么？"), ("结论是否综合主张并明确已知与未知？", "是否没有引入节点材料外的新事实？"), depends_on=("response_treatment",)),
)


_PUBLIC_OPINION_NODES = (
    _spec("trigger_content", 1, "触发内容", "什么内容触发了讨论，原始材料表达了什么？", ("原始内容", "发布主体", "发布时间", "讨论对象", "内容边界"), ("content", "statement", "attribution"), ("firsthand", "social", "news"), ("触发讨论的原始内容是什么？", "它是事实陈述、观点、提问还是转述？"), ("是否保留原文和发布主体？", "是否把观点误写成事实？")),
    _spec("initial_spread", 2, "首次扩散", "内容如何首次扩散，哪些来源形成了可观察的传播链？", ("首发与转发", "平台", "来源簇", "时间窗口", "样本范围"), ("spread", "sample", "metric"), ("social", "news", "discourse"), ("哪些记录是原创，哪些是转发或转载？", "独立来源和放大数量分别是多少？"), ("是否说明采样限制？", "是否避免把热度当作事实或民意？"), depends_on=("trigger_content",)),
    _spec("core_positions", 3, "核心观点与立场", "讨论中有哪些主要观点、立场和分歧？", ("主题", "立场对象", "支持观点", "质疑观点", "代表样本"), ("viewpoint", "stance", "sample"), ("social", "news", "discourse"), ("不同观点分别在讨论什么？", "每种观点有哪些可定位代表样本？"), ("是否区分文本立场与外部事实？", "是否避免用少数样本代表全部公众？")),
    _spec("narrative_shift", 4, "讨论峰值或叙事转向", "讨论状态或叙事发生了什么可观察变化？", ("讨论峰值", "叙事变化", "触发材料", "观点重组", "时间关系"), ("change", "metric", "comparison"), ("social", "news", "official"), ("什么材料显示讨论发生转向？", "变化是传播量、主题还是立场的变化？"), ("是否把观测到的变化与解释分开？", "转向是否有时间和样本依据？"), depends_on=("initial_spread", "core_positions")),
    _spec("response_repair", 5, "回应与修复", "相关主体如何回应，回应是否改变了讨论状态？", ("主体回应", "回应内容", "修复行动", "传播反馈", "未解决争议"), ("response", "action", "result"), ("official", "company", "platform", "news"), ("谁回应了什么？", "哪些修复行动和反馈可以被材料支持？"), ("是否区分回应内容和回应效果？", "是否明确效果仍未知的部分？")),
    _spec("current_opinion_state", 6, "当前舆情状态", "截至调查时讨论处于什么状态，哪些问题仍在持续？", ("当前主题", "持续争议", "讨论降温", "样本边界", "待核问题"), ("status", "viewpoint", "boundary"), ("social", "news", "discourse"), ("当前讨论还围绕什么？", "哪些结论只适用于本次样本？"), ("是否给出可读的综合结论？", "是否清楚说明覆盖范围和限制？"), depends_on=("response_repair",)),
)


_PRODUCT_SAFETY_NODES = (
    _spec("product_scope", 1, "产品、版本与使用范围", "调查对象是什么，涉及哪些型号、版本、批次和使用条件？", ("产品身份", "版本批次", "销售范围", "使用条件", "适用时间"), ("identity", "scope", "attribution"), ("company", "official", "regulator", "consumer"), ("哪些产品身份和范围可以确认？", "是否存在版本或批次差异？"), ("是否把不同型号或批次分开？", "范围是否都有来源依据？")),
    _spec("first_anomaly", 2, "首次异常或事故", "最早出现了什么异常，表现和影响是什么？", ("首次反馈", "异常表现", "时间地点", "使用条件", "直接影响"), ("observation", "incident", "symptom"), ("consumer", "news", "official"), ("最早异常记录是什么？", "异常表现是否来自当事人、检测还是转述？"), ("是否区分反馈与已确认缺陷？", "是否保留时间和使用条件？"), depends_on=("product_scope",)),
    _spec("affected_scope", 3, "问题聚集与受影响范围", "问题是否聚集，涉及多少产品或用户，范围如何估计？", ("问题样本", "受影响范围", "数量口径", "来源独立性", "范围边界"), ("scope", "metric", "comparison"), ("consumer", "regulator", "company", "news"), ("数量和范围的统计口径是什么？", "哪些材料相互独立，哪些只是转载？"), ("是否说明样本不是总体估计？", "是否区分投诉数量与缺陷证明？"), depends_on=("first_anomaly",)),
    _spec("investigation_findings", 4, "检测、调查与责任说明", "检测和调查发现了什么，责任或原因能否被材料支持？", ("检测方法", "调查结果", "责任说明", "反方材料", "结论边界"), ("finding", "statement", "comparison"), ("regulator", "lab", "company", "official"), ("哪些调查或检测结果可定位？", "原因和责任分别能推出到什么程度？"), ("是否给出方法和依据？", "是否避免从相关性直接推出原因？"), depends_on=("affected_scope",)),
    _spec("remediation_recall", 5, "企业处置、召回与整改", "企业和监管采取了什么处置、召回或整改行动？", ("召回", "下架", "维修替换", "整改措施", "行动结果"), ("action", "response", "result"), ("company", "regulator", "official", "news"), ("谁采取了什么措施？", "措施覆盖范围和已产生结果是什么？"), ("是否把承诺和已完成行动分开？", "不同主体的行动是否清楚？"), depends_on=("investigation_findings",)),
    _spec("retest_closure", 6, "复测、结案与剩余风险", "复测、结案和剩余风险目前能确认到什么程度？", ("复测结果", "结案状态", "剩余风险", "持续措施", "未知边界"), ("status", "result", "boundary"), ("regulator", "lab", "company", "official"), ("是否已经复测或结案？", "哪些风险仍需要观察？"), ("结论是否直接回答当前状态？", "未知边界是否明确？"), depends_on=("remediation_recall",)),
)


_CONSUMER_DISPUTE_NODES = (
    _spec("transaction_service", 1, "交易或服务关系", "消费者与商家或平台之间形成了什么交易、服务和履约关系？", ("交易对象", "时间", "合同或规则", "付款履约", "主体关系"), ("transaction", "scope", "attribution"), ("consumer", "company", "platform", "official"), ("交易或服务关系如何形成？", "哪些履约条件有材料支持？"), ("是否区分消费者陈述与订单材料？", "主体关系是否清楚？")),
    _spec("first_feedback", 2, "首次反馈", "消费者首次提出了什么问题、要求或证据？", ("首次反馈", "问题表现", "诉求", "时间", "反馈渠道"), ("complaint", "request", "observation"), ("consumer", "platform", "company"), ("消费者最初反馈了什么？", "诉求和问题表现是否分开？"), ("是否保留原始反馈依据？", "是否避免把单方陈述写成已确认事实？"), depends_on=("transaction_service",)),
    _spec("merchant_platform_response", 3, "商家或平台首次处理", "商家、平台分别如何首次回应和处理？", ("回应主体", "回应内容", "处理动作", "承诺", "处理结果"), ("response", "action", "result"), ("company", "platform", "consumer"), ("谁回应了什么？", "承诺和已完成动作分别是什么？"), ("是否清楚标记未履行承诺？", "是否涵盖商家和平台不同方面？"), depends_on=("first_feedback",)),
    _spec("escalation_intervention", 4, "升级、监管或司法介入", "争议如何升级，是否有监管、调解或司法介入？", ("升级原因", "监管介入", "调解司法", "争议转折", "程序结果"), ("change", "action", "decision"), ("regulator", "court", "platform", "news"), ("什么材料显示争议升级？", "介入机构做了什么，程序结果是什么？"), ("是否区分程序启动和实体结论？", "是否有独立材料支持介入结果？"), depends_on=("merchant_platform_response",)),
    _spec("remedy_fulfillment", 5, "退款、换货、赔偿或履约结果", "退款、换货、赔偿和其他履约结果是什么？", ("处理方案", "履行状态", "金额或对象", "双方确认", "结果边界"), ("remedy", "result", "status"), ("consumer", "company", "platform", "official"), ("提出了什么方案，实际履行到哪一步？", "结果由谁确认？"), ("是否把承诺、执行和确认分开？", "结论是否可以直接展示？"), depends_on=("escalation_intervention",)),
    _spec("unresolved_items", 6, "未解决事项", "截至调查时哪些事项仍未解决或缺少可核实材料？", ("未解决要求", "待补材料", "责任边界", "后续行动", "当前状态"), ("boundary", "status", "request"), ("consumer", "company", "platform", "official"), ("哪些事项仍未完成？", "缺少什么材料才能继续确认？"), ("是否把材料边界写成清晰结论？", "是否没有把未知写成结论性事实？"), depends_on=("remedy_fulfillment",)),
)


_OFFICIAL_CLAIM_NODES = (
    _spec("original_statement", 1, "原始声明", "声明原文是什么，由谁在何时以什么形式发布？", ("原文", "发布主体", "发布时间", "发布渠道", "版本"), ("statement", "attribution", "identity"), ("official", "firsthand", "news"), ("原始声明和版本如何定位？", "哪些内容是声明原文而非媒体转述？"), ("是否保留原文依据？", "是否区分发布主体和报道主体？")),
    _spec("claim_breakdown", 2, "声明中的主张拆分", "声明包含哪些可以逐条核验的主张？", ("主张边界", "主体", "时间", "数量", "原因结果"), ("claim", "scope", "statement"), ("official", "news", "firsthand"), ("一段声明可以拆成哪些独立主张？", "哪些只是态度或预测而非事实主张？"), ("主张是否单一清晰？", "是否保留原文范围和限定词？"), depends_on=("original_statement",)),
    _spec("evidence_comparison", 3, "逐条材料对照", "每条主张能被哪些材料支持、限制或反驳？", ("支持材料", "反驳材料", "范围差异", "来源独立性", "不可推出部分"), ("comparison", "support", "contradiction"), ("official", "news", "regulator", "firsthand"), ("每条主张的材料依据在哪里？", "材料支持到什么范围？"), ("是否逐条绑定依据片段？", "是否避免把声明本身当作独立证明？"), depends_on=("claim_breakdown",)),
    _spec("clarification_revision", 4, "补充、澄清或修订", "声明是否被补充、澄清、修订或撤回？", ("新版本", "澄清内容", "变化范围", "发布主体", "时间顺序"), ("change", "statement", "comparison"), ("official", "news", "firsthand"), ("后续版本改变了什么？", "澄清解决了哪些边界？"), ("是否保留版本差异？", "是否区分措辞变化和事实变化？"), depends_on=("claim_breakdown",)),
    _spec("current_assessment", 5, "当前可表述结论", "截至调查时每条主张可以如何准确表述？", ("支持范围", "不支持范围", "冲突", "当前措辞", "剩余问题"), ("assessment", "boundary", "status"), ("official", "regulator", "news", "firsthand"), ("哪些主张可以支持、限制或无法确认？", "用户应该看到什么准确措辞？"), ("结论是否逐条对应材料？", "是否明确证据边界而不使用全局门禁替代表达？"), depends_on=("evidence_comparison", "clarification_revision")),
)


_POLICY_NODES = (
    _spec("policy_signal", 1, "政策信号", "政策议题最早出现了什么信号，谁提出了什么方向？", ("信号来源", "议题", "提出主体", "时间", "非正式性质"), ("signal", "statement", "attribution"), ("official", "legislature", "news", "expert"), ("最早信号来自哪里？", "它是提议、讨论还是正式政策？"), ("是否明确非正式信号不等于已生效政策？", "来源主体和时间是否可定位？")),
    _spec("formal_release", 2, "正式发布", "正式政策文本发布了什么内容，由谁发布？", ("正式文本", "发布主体", "发布日期", "版本", "政策目标"), ("policy", "statement", "scope"), ("official", "regulator", "legislature"), ("正式文本和版本如何定位？", "政策目标和适用对象如何表述？"), ("是否以正式文本为依据？", "是否区分发布与生效？"), depends_on=("policy_signal",)),
    _spec("effective_scope", 3, "生效时间与适用范围", "政策何时生效，适用于哪些对象和范围？", ("生效时间", "适用对象", "地域范围", "例外条件", "过渡安排"), ("scope", "status", "date"), ("official", "regulator", "company"), ("生效日期和适用范围是什么？", "是否存在过渡或例外条件？"), ("是否严格区分发布时间和生效时间？", "范围是否有正式依据？"), depends_on=("formal_release",)),
    _spec("implementation", 4, "执行口径与实际执行", "执行机构如何解释和执行政策，实际案例显示了什么？", ("执行口径", "执行机构", "案例", "差异", "执行状态"), ("implementation", "action", "comparison"), ("regulator", "official", "company", "news"), ("执行口径和案例分别是什么？", "正式要求与实际执行是否存在差异？"), ("是否把个案与普遍效果分开？", "是否有执行材料支持？"), depends_on=("effective_scope",)),
    _spec("impact_feedback", 5, "反馈、影响与受影响主体", "政策产生了哪些可观察影响，哪些主体受到影响或提出反馈？", ("受影响主体", "实际影响", "反馈", "指标口径", "影响边界"), ("impact", "feedback", "metric"), ("regulator", "company", "consumer", "news"), ("哪些影响有材料支持？", "反馈和指标的范围、口径是什么？"), ("是否区分政策目标、执行和效果？", "是否说明样本和时段限制？"), depends_on=("implementation",)),
    _spec("revision_withdrawal", 6, "修订、延期或废止", "政策是否被修订、延期、暂停或废止，当前版本是什么？", ("修订内容", "延期暂停", "废止状态", "当前版本", "剩余影响"), ("change", "status", "policy"), ("official", "regulator", "legislature", "news"), ("后续文本改变了什么？", "截至调查时哪个版本有效？"), ("是否清楚说明版本时间线？", "结论是否不超出当前材料？"), depends_on=("impact_feedback",)),
)


TRACE_TEMPLATES: dict[str, TraceTemplate] = {
    "public_event": _template("public_event", "公共事件 / 突发事件", "重建事件从早期信号到当前结果的可读脉络。", _PUBLIC_EVENT_NODES),
    "public_opinion": _template("public_opinion", "公共舆情 / 品牌声誉", "区分触发内容、传播样本、观点变化和回应修复。", _PUBLIC_OPINION_NODES),
    "product_safety": _template("product_safety", "产品安全", "围绕产品身份、异常、调查、处置和复测建立问题链。", _PRODUCT_SAFETY_NODES),
    "consumer_dispute": _template("consumer_dispute", "消费纠纷", "围绕交易关系、反馈、处理、升级和履约结果组织个案。", _CONSUMER_DISPUTE_NODES),
    "official_claim": _template("official_claim", "官方声明 / 事实核验", "把声明拆成逐条主张并对照材料边界。", _OFFICIAL_CLAIM_NODES),
    "policy_regulation": _template("policy_regulation", "政策 / 监管影响", "追踪政策从信号、发布、生效到执行反馈和版本变化。", _POLICY_NODES),
}


def get_trace_template(investigation_type: str) -> TraceTemplate:
    try:
        return TRACE_TEMPLATES[str(investigation_type).strip()]
    except KeyError as exc:
        raise TraceContractError(f"unsupported node-driven investigation template: {investigation_type}") from exc


def list_trace_templates() -> tuple[TraceTemplate, ...]:
    return tuple(TRACE_TEMPLATES.values())


__all__ = [
    "TraceContractError", "TraceEdgeRelation",
    "ConclusionIR",
    "EdgeSpec",
    "FrozenTraceNode",
    "MAX_NODE_REVISIONS",
    "REVIEW_RUBRIC_AXES", "REVIEW_ROUND_GUIDANCE", "review_round_context",
    "NODE_TEMPLATE_VERSION",
    "NodeClaim",
    "NodeDraft",
    "NodeEvidence",
    "NodeEvidenceRelation",
    "NodeMedia",
    "NodeMediaPolicy",
    "NodeMediaType",
    "NodeRevision",
    "NodeReview",
    "NodeReviewStatus",
    "NodeSpec",
    "TRACE_TEMPLATES",
    "TraceTemplate",
    "get_trace_template",
    "list_trace_templates",
]
