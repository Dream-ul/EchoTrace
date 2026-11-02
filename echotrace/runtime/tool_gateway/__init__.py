"""Atomic Tool Gateway and provider contracts.

This package is the only runtime implementation for model-callable tools.
Professional Agent Skills live under ``echotrace/skills`` and are loaded by
``echotrace.agent.skill_catalog``.
"""

from .audit import ToolCallAudit
from .contracts import (
    CAPABILITY_CATALOG,
    FORMAL_CAPABILITIES,
    KNOWN_CAPABILITIES,
    CapabilitySpec,
    ToolBudgetExceeded,
    ToolContractError,
    ToolPermissionError,
    ToolProviderManifest,
    ToolTimeout,
    ToolUnavailable,
)
from .router import ToolRouter
__all__ = [
    "CAPABILITY_CATALOG",
    "FORMAL_CAPABILITIES",
    "KNOWN_CAPABILITIES",
    "CapabilitySpec",
    "ToolBudgetExceeded",
    "ToolContractError",
    "ToolCallAudit",
    "ToolPermissionError",
    "ToolProviderManifest",
    "ToolRouter",
    "ToolTimeout",
    "ToolUnavailable",
]
