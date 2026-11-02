"""Model-facing Agent runtime for EchoTrace."""

from .contracts import (
    AgentAction,
    DecisionMode,
    AgentDecision,
    AgentDecisionBatch,
    AgentResult,
    AgentRuntimeError,
    AgentTraceEvent,
    AgentTraceSink,
    ModelClient,
    ToolGateway,
    ToolSchemaNotLoadedError,
)
from .context import AgentContextAssembler
from .context_pack import PACK_VERSION, build_context_pack, context_pack_hash
from .prompts import (
    DEFAULT_ROLE_PROMPTS,
    NODE_ORCHESTRATOR_PROMPT,
    NODE_RESEARCHER_PROMPT,
    NODE_ROLE_PROMPTS,
    NODE_VERIFIER_PROMPT,
    RolePrompt,
    role_prompt,
)
from .model_clients import (
    CallableModelClient,
    ModelResponseError,
    OpenAICompatibleModelClient,
    ReplayModelClient,
    parse_agent_decision,
)
from .runtime import AgentRuntime
from .skill_catalog import AgentSkillCatalog, AgentSkillSpec
from .trace import InvestigationRunTraceSink, RunTracePort
from .transcript import TranscriptCompaction, compact_transcript, transcript_hash
from .diagnostics import JsonlDiagnostics, redact_diagnostic

__all__ = [
    "AgentAction",
    "DecisionMode",
    "AgentContextAssembler",
    "PACK_VERSION",
    "build_context_pack",
    "context_pack_hash",
    "AgentDecision",
    "AgentDecisionBatch",
    "AgentResult",
    "AgentRuntimeError",
    "AgentTraceEvent",
    "AgentTraceSink",
    "AgentRuntime",
    "AgentSkillCatalog",
    "AgentSkillSpec",
    "InvestigationRunTraceSink",
    "RunTracePort",
    "ModelClient",
    "DEFAULT_ROLE_PROMPTS",
    "NODE_ORCHESTRATOR_PROMPT",
    "NODE_RESEARCHER_PROMPT",
    "NODE_VERIFIER_PROMPT",
    "NODE_ROLE_PROMPTS",
    "RolePrompt",
    "role_prompt",
    "CallableModelClient",
    "OpenAICompatibleModelClient",
    "ModelResponseError",
    "ReplayModelClient",
    "parse_agent_decision",
    "TranscriptCompaction",
    "compact_transcript",
    "transcript_hash",
    "JsonlDiagnostics",
    "redact_diagnostic",
    "ToolGateway",
    "ToolSchemaNotLoadedError",
]
