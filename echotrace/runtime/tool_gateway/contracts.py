"""Machine-readable contracts shared by the Harness and Skill packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _json_type_schema(expected: str) -> dict[str, Any]:
    """Translate the compact manifest type notation to JSON Schema."""

    if expected.endswith("[]"):
        return {"type": "array", "items": _json_type_schema(expected[:-2])}
    if expected == "any":
        return {}
    if expected in {"string", "integer", "number", "boolean", "object", "array"}:
        return {"type": expected}
    return {"type": "object"}


def _validate_schema_notation(schema: dict[str, str], *, label: str) -> None:
    """Reject silently downgraded/typoed compact schema types."""

    allowed = {"any", "string", "integer", "number", "boolean", "object", "array"}
    for field_name, raw_expected in schema.items():
        expected = str(raw_expected)
        if expected.endswith("?"):
            expected = expected[:-1]
        while expected.endswith("[]"):
            expected = expected[:-2]
        if expected not in allowed:
            raise ToolContractError(
                f"{label} field {field_name} has unsupported schema type: {raw_expected}"
            )


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """The planner-facing contract for one stable investigation capability."""

    name: str
    description: str
    use_when: tuple[str, ...] = ()
    avoid_when: tuple[str, ...] = ()
    input_schema: dict[str, str] = field(default_factory=dict)
    input_descriptions: dict[str, str] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    output_descriptions: dict[str, str] = field(default_factory=dict)
    agents: tuple[str, ...] = ()
    max_risk: str = "read"

    def json_schema(self, *, direction: str = "input") -> dict[str, Any]:
        """Return a provider-neutral JSON Schema for model/function calling."""

        if direction not in {"input", "output"}:
            raise ValueError("schema direction must be input or output")
        schema = self.input_schema if direction == "input" else self.output_schema
        descriptions = self.input_descriptions if direction == "input" else self.output_descriptions
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, expected in schema.items():
            optional = expected.endswith("?")
            properties[name] = {
                **_json_type_schema(expected.rstrip("?")),
                **({"description": descriptions[name]} if descriptions.get(name) else {}),
            }
            if not optional:
                required.append(name)
        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            result["required"] = required
        return result

    def planner_entry(self, *, available: bool, provider_count: int) -> dict[str, Any]:
        return {
            "capability": self.name,
            "description": self.description,
            "use_when": list(self.use_when),
            "avoid_when": list(self.avoid_when),
            "input_fields": [
                {
                    "name": name,
                    "type": expected.rstrip("?"),
                    "required": not expected.endswith("?"),
                    "description": self.input_descriptions.get(name, ""),
                }
                for name, expected in self.input_schema.items()
            ],
            "input_schema": self.json_schema(direction="input"),
            "output_schema": self.json_schema(direction="output"),
            "allowed_agents": list(self.agents),
            "max_risk": self.max_risk,
            "available": available,
        }

    def native_function(self, *, available: bool = True) -> dict[str, Any]:
        """Build an OpenAI-compatible function definition without provider data."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(direction="input"),
            },
        }


# This catalog is deliberately independent from installed providers.  Provider
# manifests describe execution; this table describes the stable intent the LLM
# is allowed to request.  Keeping it small makes the menu auditable and keeps
# provider churn out of the model's routing decision.
CAPABILITY_CATALOG: dict[str, CapabilitySpec] = {
    # Gateway-owned meta capability. It searches the deferred capability
    # index; it is not backed by a provider package and is always core.
    "search_tools": CapabilitySpec(
        "search_tools",
        "Load one or more deferred Tool capabilities by their exact names from deferred_tools.",
        ("The required operation is a deferred capability shown in deferred_tools.",),
        ("A known core Tool already fits the operation; call it directly.",),
        {"capabilities": "string[]"},
        {
            "capabilities": "Exact deferred capability names copied from deferred_tools. Do not use descriptions, aliases, provider names, or fuzzy queries.",
        },
        {"tools": "array", "count": "integer"},
        {"tools": "Loaded deferred capabilities with complete input/output schemas for JSON-mode callers; native callers receive loading metadata and get schemas in the next tools list.", "count": "Number of loaded capabilities."},
        (),
    ),
    "search_web": CapabilitySpec(
        "search_web",
        "Search public web sources, including first-party domains, for documents relevant to an investigation question.",
        ("A bounded public web search is needed to discover candidate or official sources.",),
        ("The source requires login, a private session, or a direct page fetch is already known.",),
        {"query": "string", "limit": "integer?", "domains": "string[]?"},
        {
            "query": "Specific factual question or search terms.",
            "limit": "Maximum number of candidates.",
            "domains": "Optional public domains used to constrain first-party or scoped discovery.",
        },
        {"documents": "array", "count": "integer?"},
        {"documents": "Candidate source records.", "count": "Number of candidates."},
        ("researcher",),
    ),
    "ingest_public_feed": CapabilitySpec(
        "ingest_public_feed",
        "Ingest a public RSS or Atom feed into normalized EchoTrace documents.",
        ("A public feed contains the source's entries and no authenticated session is needed.",),
        ("The feed is private, requires browser state, or the task needs claim verification.",),
        {"feed_url": "string", "limit": "integer?"},
        {"feed_url": "Public RSS or Atom URL.", "limit": "Maximum entries to ingest."},
        {"documents": "array", "count": "integer?"},
        {"documents": "Normalized feed documents.", "count": "Number of entries."},
        ("researcher",),
    ),
    "fetch_public_page": CapabilitySpec(
        "fetch_public_page",
        "Fetch and normalize one publicly reachable page while preserving source metadata and text locations.",
        ("A known public URL must be captured for later evidence use.",),
        ("The page requires authentication, disallowed automation, or a search result is sufficient.",),
        {"url": "string"},
        {"url": "Canonical public page URL."},
        {"document": "object"},
        {"document": "Captured page with provenance and text locations."},
        ("researcher",),
    ),
    "list_source_resources": CapabilitySpec(
        "list_source_resources",
        "List bounded source and evidence descriptors visible to the current node.",
        ("You need to discover which source cards or evidence references can support the current node.",),
        ("You already have the exact evidence or document ID.",),
        {"run_id": "string", "node_id": "string?", "query": "string?", "limit": "integer?", "cursor": "string?"},
        {"run_id": "Current investigation run ID.", "node_id": "Current node ID when available.", "query": "Optional terms used to narrow descriptors.", "limit": "Maximum descriptors to return.", "cursor": "Continuation cursor from a previous result."},
        {"resources": "array", "count": "integer", "next_cursor": "string?", "omissions": "array"},
        {"resources": "Self-describing source/evidence descriptors.", "count": "Returned descriptor count.", "next_cursor": "Cursor for the next bounded page.", "omissions": "Explicitly omitted records and reacquire instructions."},
        ("researcher", "verifier"),
    ),
    "get_source_excerpt": CapabilitySpec(
        "get_source_excerpt",
        "Read a bounded, citable source excerpt by evidence or document reference.",
        ("You need to verify wording, a locator, a time boundary, or a missing condition.",),
        ("A summary is sufficient or the source requires authentication.",),
        {"evidence_id": "string?", "document_id": "string?", "max_chars": "integer?", "before_chars": "integer?", "after_chars": "integer?"},
        {"evidence_id": "Known evidence ID.", "document_id": "Known document ID when no evidence ID is available.", "max_chars": "Maximum excerpt length.", "before_chars": "Optional preceding context length.", "after_chars": "Optional following context length."},
        {"resource_id": "string", "resource_kind": "string", "excerpt": "string", "citation": "object", "content_hash": "string", "truncated": "boolean", "next_cursor": "string?"},
        {"resource_id": "Stable evidence/document reference.", "resource_kind": "Resource kind.", "excerpt": "Bounded source text.", "citation": "Document/evidence locator.", "content_hash": "Source content hash.", "truncated": "Whether more text is available.", "next_cursor": "Continuation cursor when truncated."},
        ("researcher", "verifier"),
    ),
    "get_source_document": CapabilitySpec(
        "get_source_document",
        "Read a bounded normalized source document page by document ID.",
        ("A source card is relevant and a larger normalized passage is required.",),
        ("You only need a source count or analysis signal.",),
        {"document_id": "string", "max_chars": "integer?", "cursor": "string?"},
        {"document_id": "Known source document ID.", "max_chars": "Maximum normalized text length.", "cursor": "Continuation cursor."},
        {"resource_id": "string", "resource_kind": "string", "text": "string", "citation": "object", "content_hash": "string", "truncated": "boolean", "next_cursor": "string?"},
        {"resource_id": "Stable document reference.", "resource_kind": "Resource kind.", "text": "Bounded normalized source text.", "citation": "Document locator.", "content_hash": "Source content hash.", "truncated": "Whether more text is available.", "next_cursor": "Continuation cursor when truncated."},
        ("researcher", "verifier"),
    ),
    "get_analysis_context": CapabilitySpec(
        "get_analysis_context",
        "Read bounded analysis aggregates and sample definitions for the current node.",
        ("You need sentiment, stance, topic, propagation, coverage, or bias context.",),
        ("The output would be used as factual proof of an event.",),
        {"run_id": "string", "node_id": "string?", "feature_type": "string?", "limit": "integer?", "cursor": "string?"},
        {"run_id": "Current investigation run ID.", "node_id": "Current node ID.", "feature_type": "Optional feature type.", "limit": "Maximum features.", "cursor": "Continuation cursor."},
        {"features": "array", "count": "integer", "coverage": "object", "next_cursor": "string?"},
        {"features": "Versioned descriptive features.", "count": "Returned feature count.", "coverage": "Sample and limitation information.", "next_cursor": "Continuation cursor."},
        ("researcher", "verifier"),
    ),
    "get_transcript_context": CapabilitySpec(
        "get_transcript_context",
        "Read bounded summaries of recent actions after transcript compaction.",
        ("You need to recover a prior tool action, result reference, or unfinished control step.",),
        ("You need source facts; use source tools instead.",),
        {"run_id": "string", "node_id": "string?", "limit": "integer?", "cursor": "string?"},
        {"run_id": "Current investigation run ID.", "node_id": "Current node ID.", "limit": "Maximum transcript summaries.", "cursor": "Continuation cursor."},
        {"items": "array", "count": "integer", "next_cursor": "string?"},
        {"items": "Bounded diagnostic summaries, never full prompts or hidden reasoning.", "count": "Returned item count.", "next_cursor": "Continuation cursor."},
        ("researcher", "verifier"),
    ),
    "normalize_documents": CapabilitySpec(
        "normalize_documents",
        "Normalize captured public-source records into stable documents with provenance and text locations.",
        ("Raw provider records must be converted before claim extraction or evidence linking.",),
        ("The documents are already normalized or new public sources still need to be discovered.",),
        {"records": "array"},
        {"records": "Raw records returned by an approved source provider."},
        {"documents": "array", "count": "integer?"},
        {"documents": "Stable normalized documents.", "count": "Number of normalized documents."},
        ("researcher",),
    ),
    "cluster_sources": CapabilitySpec(
        "cluster_sources",
        "Group duplicate or syndicated documents into source clusters and expose independent-source counts.",
        ("Several documents may be copies, rewrites, or independent reports of one event.",),
        ("A single known source is being fetched or a claim is being verified.",),
        {"document_ids": "string[]"},
        {"document_ids": "Documents to compare for content and provenance overlap."},
        {"clusters": "array", "count": "integer?"},
        {"clusters": "Source clusters and independent-origin counts.", "count": "Number of clusters."},
        ("researcher", "verifier"),
    ),
    "check_source_independence": CapabilitySpec(
        "check_source_independence",
        "Assess whether evidence sources are sufficiently independent for a verification decision.",
        ("A claim's apparent support may come from duplicated or shared-origin reporting.",),
        ("Source material has not yet been normalized or clustered.",),
        {"document_ids": "string[]"},
        {"document_ids": "Evidence documents whose provenance should be compared."},
        {"independent": "boolean", "reason": "string"},
        {"independent": "Whether the evidence origins are sufficiently independent.", "reason": "Bounded explanation."},
        ("verifier",),
    ),
    "analyze_discourse": CapabilitySpec(
        "analyze_discourse",
        "Produce versioned sentiment, stance, or topic features as context for risk interpretation.",
        ("The investigation needs structured discourse features as secondary context.",),
        ("The result would be used as proof of whether a factual claim is true.",),
        {"document_ids": "string[]"},
        {"document_ids": "Documents to analyze without changing evidence strength."},
        {"features": "array", "coverage": "object", "count": "integer?"},
        {"features": "Versioned discourse features.", "coverage": "Coverage and bias summary.", "count": "Number of analyzed documents."},
        ("researcher",),
    ),
}


# This is intentionally small and explicit. New capabilities must be reviewed
# before they become available to an LLM planner.
FORMAL_CAPABILITIES = frozenset(CAPABILITY_CATALOG)
KNOWN_CAPABILITIES = FORMAL_CAPABILITIES


class ToolContractError(ValueError):
    """Raised when a Tool provider input or output violates its schema."""


class ToolUnavailable(RuntimeError):
    """Raised when a Tool provider cannot execute with current configuration."""

    def __init__(self, message: str, error_code: str = "tool_unavailable") -> None:
        super().__init__(message)
        self.error_code = error_code


class ToolPermissionError(PermissionError):
    """A policy or owner check rejected a Tool before execution."""

    def __init__(self, message: str, error_code: str = "permission_denied") -> None:
        super().__init__(message)
        self.error_code = error_code


class ToolBudgetExceeded(ValueError):
    """A global or per-Tool execution budget prevented a call."""

    def __init__(self, message: str, error_code: str = "budget_exceeded") -> None:
        super().__init__(message)
        self.error_code = error_code


class ToolTimeout(ToolUnavailable):
    """A Tool provider did not finish within its manifest timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="timeout")


@dataclass(frozen=True, slots=True)
class ToolProviderManifest:
    name: str
    version: str = "1.0.0"
    description: str = ""
    owners: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    input_schema: dict[str, str] = field(default_factory=dict)
    capability_inputs: dict[str, dict[str, str]] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    risk: str = "read"
    requires: tuple[str, ...] = ()
    max_calls: int = 1
    timeout_seconds: float = 30.0
    failure_mode: str = "fail"
    priority: int = 100
    fallback_providers: tuple[str, ...] = ()
    entrypoint: str = "handler.py:run"
    # Extension fields are appended to preserve the positional constructor
    # order used by older in-code registrations.
    input_descriptions: dict[str, str] = field(default_factory=dict)
    output_descriptions: dict[str, str] = field(default_factory=dict)
    use_when: tuple[str, ...] = ()
    avoid_when: tuple[str, ...] = ()
    exposure: str = "core"
    exposure_reason: str = ""
    allow_custom_capabilities: bool = False
    input_json_schema: dict[str, Any] | None = None
    output_json_schema: dict[str, Any] | None = None
    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool provider name is required")
        if not self.description.strip():
            raise ValueError("tool provider description is required")
        if not self.capabilities:
            raise ValueError("tool provider capabilities are required")
        if self.risk not in {"read", "write"}:
            raise ValueError(f"unsupported tool risk: {self.risk}")
        if self.failure_mode not in {"fail", "partial", "fallback"}:
            raise ValueError(f"unsupported tool failure mode: {self.failure_mode}")
        if self.exposure not in {"core", "deferred"}:
            raise ValueError("tool exposure must be core or deferred")
        if self.exposure == "core" and self.risk != "read":
            raise ValueError("core tools must be read-only; write tools must be deferred")
        if ":" not in self.entrypoint:
            raise ValueError("tool entrypoint must use path:callable syntax")
        if self.max_calls < 1 or self.timeout_seconds <= 0 or self.priority < 0:
            raise ValueError("tool budget values must be positive and priority cannot be negative")
        _validate_schema_notation(self.input_schema, label=f"tool {self.name} input")
        _validate_schema_notation(self.output_schema, label=f"tool {self.name} output")
        for capability, schema in self.capability_inputs.items():
            _validate_schema_notation(schema, label=f"tool {self.name} {capability} input")

    def input_schema_for(self, capability: str | None = None) -> dict[str, str]:
        if capability and capability in self.capability_inputs:
            return self.capability_inputs[capability]
        return self.input_schema

    def validate_input(self, payload: dict[str, Any], capability: str | None = None) -> None:
        validate_schema(self.input_schema_for(capability), payload, "input")

    def validate_output(self, output: Any) -> None:
        if not isinstance(output, dict):
            raise ToolContractError(f"tool {self.name} output must be an object")
        validate_schema(self.output_schema, output, "output")

    def json_schema(self, *, capability: str | None = None, direction: str = "input") -> dict[str, Any]:
        """Return the manifest's complete provider-facing JSON Schema."""

        if direction not in {"input", "output"}:
            raise ValueError("schema direction must be input or output")
        explicit = self.input_json_schema if direction == "input" else self.output_json_schema
        if explicit is not None:
            return dict(explicit)
        schema = self.input_schema_for(capability) if direction == "input" else self.output_schema
        descriptions = self.input_descriptions if direction == "input" else self.output_descriptions
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, expected in schema.items():
            optional = expected.endswith("?")
            properties[name] = {
                **_json_type_schema(expected.rstrip("?")),
                **({"description": descriptions[name]} if descriptions.get(name) else {}),
            }
            if not optional:
                required.append(name)
        result: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            result["required"] = required
        return result

    def validate_capabilities(
        self,
        known: frozenset[str] = KNOWN_CAPABILITIES,
    ) -> None:
        """Reject capability aliases and typos for every registration path."""
        unknown = sorted(set(self.capabilities) - set(known))
        if unknown and not self.allow_custom_capabilities:
            raise ToolContractError(
                f"tool provider {self.name} declares unknown capabilities: {', '.join(unknown)}"
            )
        if unknown:
            for capability in unknown:
                schema = self.input_schema_for(capability)
                if not schema or not self.output_schema:
                    raise ToolContractError(
                        f"custom capability {capability} requires explicit input and output contracts"
                    )

    def validate_capability_contracts(self) -> None:
        unknown = sorted(set(self.capability_inputs) - set(self.capabilities))
        if unknown:
            raise ToolContractError(
                f"tool provider {self.name} defines input contracts for undeclared capabilities: {', '.join(unknown)}"
            )
        for capability, schema in self.capability_inputs.items():
            spec = CAPABILITY_CATALOG.get(capability)
            if spec is None:
                continue
            for field_name, provider_type in schema.items():
                catalog_type = spec.input_schema.get(field_name)
                if catalog_type is None or provider_type.rstrip("?") != catalog_type.rstrip("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} input contract for {capability} conflicts at field: {field_name}"
                    )
                if not provider_type.endswith("?") and catalog_type.endswith("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} makes optional planner field required: {capability}.{field_name}"
                    )
        # A provider may omit optional planner fields, but it must accept every
        # required field and cannot change the catalog's type semantics.  This
        # prevents a valid planner payload from failing only after routing.
        for capability in self.capabilities:
            spec = CAPABILITY_CATALOG.get(capability)
            if spec is None:
                continue
            schema = self.input_schema_for(capability)
            for field_name, catalog_type in spec.input_schema.items():
                provider_type = schema.get(field_name)
                if provider_type is None:
                    if not catalog_type.endswith("?"):
                        raise ToolContractError(
                            f"tool provider {self.name} input contract missing required field: {capability}.{field_name}"
                        )
                    continue
                if provider_type.rstrip("?") != catalog_type.rstrip("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} input contract conflicts at field: {capability}.{field_name}"
                    )
                if catalog_type.endswith("?") and not provider_type.endswith("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} makes optional planner field required: {capability}.{field_name}"
                    )

    def validate_output_contracts(self) -> None:
        """Require package providers to preserve the stable capability result envelope."""

        for capability in self.capabilities:
            spec = CAPABILITY_CATALOG.get(capability)
            if spec is None:
                continue
            for field_name, catalog_type in spec.output_schema.items():
                provider_type = self.output_schema.get(field_name)
                if provider_type is None and not catalog_type.endswith("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} output contract missing required field: {capability}.{field_name}"
                    )
                if provider_type is not None and provider_type.rstrip("?") != catalog_type.rstrip("?"):
                    raise ToolContractError(
                        f"tool provider {self.name} output contract conflicts at field: {capability}.{field_name}"
                    )


def validate_schema(schema: dict[str, str], payload: dict[str, Any], label: str) -> None:
    unknown = sorted(set(payload) - set(schema))
    if unknown:
        raise ToolContractError(f"tool {label} has unknown fields: {', '.join(unknown)}")
    for key, expected in schema.items():
        optional = expected.endswith("?")
        expected = expected.rstrip("?")
        if key not in payload or payload[key] is None:
            if optional:
                continue
            raise ToolContractError(f"tool {label} missing field: {key}")
        if expected != "any" and not matches_type(payload[key], expected):
            raise ToolContractError(f"tool {label} field {key} must be {expected}")


def validate_capability_input(capability: str, payload: dict[str, Any]) -> None:
    """Validate public planner input before provider-specific validation."""
    spec = CAPABILITY_CATALOG.get(capability)
    if spec is not None:
        validate_schema(spec.input_schema, payload, f"capability {capability} input")


def matches_type(value: Any, expected: str) -> bool:
    if expected.endswith("[]"):
        return isinstance(value, list) and all(matches_type(item, expected[:-2]) for item in value)
    return {
        "any": True,
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }.get(expected, False)
