"""Runtime safety policy for investigation tools and conclusions."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InvestigationPolicy:
    max_documents: int = 200
    allow_write_actions: bool = False

    def check_document_count(self, count: int) -> None:
        if count > self.max_documents:
            raise ValueError(f"document limit exceeded: {count} > {self.max_documents}")

    def require_read_only(self, action: str) -> None:
        if not self.allow_write_actions:
            raise PermissionError(f"write action is disabled for investigation runtime: {action}")

    def check_tool_risk(self, risk: str, tool_name: str) -> None:
        if risk not in {"read", "write"}:
            raise ValueError(f"unsupported tool risk: {risk}")
        if risk == "write":
            self.require_read_only(tool_name)
