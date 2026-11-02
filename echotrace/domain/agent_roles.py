"""Roles used by the node-driven Agent runtime."""

from enum import StrEnum


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    RESEARCHER = "researcher"
    VERIFIER = "verifier"
    CONCLUSION_FORMATTER = "conclusion_formatter"
    REPORT_COMPOSER = "report_composer"


__all__ = ["AgentRole"]
