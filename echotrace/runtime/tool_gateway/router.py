"""Atomic Tool Gateway for versioned provider packages.

The capability catalog and provider manifests in this module describe atomic
tools. Professional Agent Skills are loaded separately by ``echotrace.agent``.
This gateway owns provider eligibility, budgets, timeout, fallback, and audit.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
from threading import RLock
from time import monotonic
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4

from ..policies import InvestigationPolicy
from .audit import ToolCallAudit
from .contracts import (
    CAPABILITY_CATALOG,
    FORMAL_CAPABILITIES,
    KNOWN_CAPABILITIES,
    ToolBudgetExceeded,
    ToolContractError,
    ToolProviderManifest,
    ToolPermissionError,
    ToolTimeout,
    ToolUnavailable,
    validate_capability_input,
)


GATEWAY_META_CAPABILITIES = frozenset({"search_tools"})


class TraceSink(Protocol):
    def add_trace(self, step: str, status: str, **data: Any) -> Any:
        ...


@dataclass(slots=True)
class _Provider:
    manifest: ToolProviderManifest
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    package_dir: Path | None = None
    handler_path: Path | None = None


class ToolRouter:
    """Resolve tool capabilities to approved, observable provider calls."""

    def __init__(
        self,
        policy: InvestigationPolicy | None = None,
        config: dict[str, Any] | None = None,
        *,
        known_capabilities: Iterable[str] = KNOWN_CAPABILITIES,
        max_in_memory_audits: int = 10_000,
        audit_sink: Callable[[ToolCallAudit], Any] | None = None,
        provider_root: str | Path | None = None,
        provider_call_guard: Callable[[str, str, dict[str, Any], float, Callable[[], Any]], Any] | None = None,
    ) -> None:
        self.policy = policy or InvestigationPolicy()
        # Deployment environment is the default configuration source. Tests
        # and embedded callers can still pass an explicit mapping to keep
        # provider availability deterministic.
        self.config = dict(os.environ if config is None else config)
        self.known_capabilities = frozenset(known_capabilities)
        self.max_in_memory_audits = max(1, int(max_in_memory_audits))
        self.audit_sink = audit_sink
        self.provider_call_guard = provider_call_guard
        self._providers: dict[str, _Provider] = {}
        self._explicit_provider_names: set[str] = set()
        self._capabilities: dict[str, list[str]] = {}
        # Counters are scoped to one investigation run for diagnostics. Actual
        # provider protection is handled by Redis rate and concurrency limits.
        self._provider_call_counts: Counter[str] = Counter()
        self._total_call_count = 0
        self._scoped_provider_call_counts: dict[str, Counter[str]] = {}
        self._scoped_total_call_counts: Counter[str] = Counter()
        self._next_call_seq = 0
        self._lock = RLock()
        self.audits: list[ToolCallAudit] = []
        self.load_errors: list[dict[str, str]] = []
        self._default_provider_root = Path(__file__).resolve().parents[2] / "tool_providers"
        self._provider_root = Path(provider_root).resolve() if provider_root is not None else self._default_provider_root
        self._provider_root_discovered = False
        if provider_root is not None:
            self.load_packages(provider_root)
            self._provider_root_discovered = True

    def ensure_discovered(self) -> None:
        """Scan the configured provider root once at Agent startup.

        Explicit in-code registrations remain a supported extension point. A
        package with the same name never overwrites an already registered
        provider, so extensions can deliberately provide a test/deployment
        implementation while the canonical directory is still scanned.
        """

        if self._provider_root_discovered:
            return
        self._provider_root_discovered = True
        if self._provider_root.is_dir():
            self.load_packages(self._provider_root)

    def register(
        self,
        manifest: ToolProviderManifest,
        handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        *,
        package_dir: str | Path | None = None,
        handler_path: str | Path | None = None,
        _discovered: bool = False,
    ) -> None:
        """Register a validated manifest and executable handler."""
        if handler is not None and not callable(handler):
            raise TypeError(f"Tool provider handler must be callable: {manifest.name}")
        manifest.validate_capabilities(self.known_capabilities)
        manifest.validate_capability_contracts()
        manifest.validate_output_contracts()
        reserved = sorted(set(manifest.capabilities) & GATEWAY_META_CAPABILITIES)
        if reserved:
            raise ToolContractError(
                f"Tool provider cannot register gateway meta capability: {', '.join(reserved)}"
            )
        if manifest.name in self._providers:
            raise ValueError(f"tool provider already registered: {manifest.name}")
        if manifest.name in manifest.fallback_providers:
            raise ToolContractError(f"Tool provider cannot fall back to itself: {manifest.name}")
        self._providers[manifest.name] = _Provider(
            manifest,
            handler,
            Path(package_dir) if package_dir else None,
            Path(handler_path) if handler_path else None,
        )
        if not _discovered:
            self._explicit_provider_names.add(manifest.name)
        for capability in manifest.capabilities:
            self._capabilities.setdefault(capability, []).append(manifest.name)
            self._capabilities[capability].sort(key=self._sort_key)

    def describe(self) -> list[ToolProviderManifest]:
        """Return manifests in deterministic routing order."""
        return [self._providers[name].manifest for name in sorted(self._providers, key=self._sort_key)]

    def tool_context(
        self,
        agent: str | None = None,
        capabilities: Iterable[str] | None = None,
        *,
        loaded_capabilities: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Return the two-level model context for the current deployment.

        The provider directory is the source of truth for which capabilities
        exist and whether they are core or deferred.  Deferred tools expose
        only a short index until the Agent explicitly inspects one.
        """

        selected = set(capabilities) if capabilities is not None else None
        loaded = set(loaded_capabilities)
        core: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        loaded_details: list[dict[str, Any]] = []
        for capability in self._visible_capabilities(agent, selected, include_catalog=False):
            if capability in loaded and capability not in self._capabilities:
                raise ToolUnavailable(f"unknown loaded capability: {capability}", "unknown_capability")
            detail = self.capability_detail(capability, agent)
            exposure = self._capability_exposure(capability, agent)
            if exposure == "core" or capability in loaded:
                core.append(detail)
                if capability in loaded and exposure == "deferred":
                    loaded_details.append(detail)
            else:
                deferred.append({
                    "capability": capability,
                    "description": detail["description"],
                    "use_when": detail["use_when"],
                    "avoid_when": detail["avoid_when"],
                    "allowed_agents": detail["allowed_agents"],
                    "max_risk": detail["max_risk"],
                    "available": detail["available"],
                    "exposure": "deferred",
                })
        # ``search_tools`` is a gateway-owned core meta tool. It remains
        # callable throughout the run so a later task can load another
        # deferred capability without changing the control protocol.
        core.append(self.capability_detail("search_tools", agent))
        return {
            "core_tools": core,
            "deferred_tools": deferred,
            "loaded_tools": loaded_details,
            "catalog_version": self.tool_catalog_hash(agent, selected),
        }

    def native_tools(
        self,
        agent: str | None = None,
        capabilities: Iterable[str] | None = None,
        *,
        loaded_capabilities: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Build native function definitions for core and inspected tools."""

        context = self.tool_context(agent, capabilities, loaded_capabilities=loaded_capabilities)
        functions = [self._native_function(item) for item in context["core_tools"] if item.get("available", True)]
        return functions

    def tool_catalog_hash(self, agent: str | None = None, capabilities: Iterable[str] | None = None) -> str:
        selected = set(capabilities) if capabilities is not None else None
        entries = [self.capability_detail(name, agent) for name in self._visible_capabilities(agent, selected, include_catalog=False)]
        entries.append(self.capability_detail("search_tools", agent))
        return sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def diagnostics(self, capabilities: Iterable[str] | None = None) -> dict[str, Any]:
        """Return load and capability coverage diagnostics for operations."""
        requested = set(capabilities or CAPABILITY_CATALOG)
        missing = sorted(capability for capability in requested if not self._capabilities.get(capability))
        return {"missing_capabilities": missing, "load_errors": list(self.load_errors)}

    def capability_detail(self, capability: str, agent: str | None = None) -> dict[str, Any]:
        """Return planner contract plus eligible execution availability.

        This is the level-two contract read only after a capability has been
        selected. It deliberately does not disclose provider names or paths.
        """
        spec = CAPABILITY_CATALOG.get(capability)
        if capability in GATEWAY_META_CAPABILITIES:
            if spec is None:
                raise ToolUnavailable(f"unknown gateway capability: {capability}", "unknown_capability")
            entry = spec.planner_entry(available=True, provider_count=1)
            entry["exposure"] = "core"
            entry["meta"] = True
            return entry
        all_providers = [self._providers[name].manifest for name in self._capabilities.get(capability, ())]
        providers = [
            self._providers[name].manifest
            for name in self._capabilities.get(capability, ())
            if agent is None or self._owner_matches(agent, self._providers[name].manifest)
        ]
        if spec is None and not providers:
            if all_providers and agent is not None:
                raise ToolPermissionError(
                    f"agent {agent} cannot request capability: {capability}",
                    "capability_denied",
                )
            raise ToolUnavailable(f"unknown capability: {capability}", "unknown_capability")
        if agent is not None and spec.agents and agent not in spec.agents:
            raise ToolPermissionError(f"agent {agent} cannot request capability: {capability}", "capability_denied")
        if spec is None:
            manifest = providers[0]
            entry = {
                "capability": capability,
                "description": manifest.description,
                "use_when": list(manifest.use_when),
                "avoid_when": list(manifest.avoid_when),
                "input_fields": [],
                "input_schema": manifest.json_schema(capability=capability, direction="input"),
                "output_schema": manifest.json_schema(capability=capability, direction="output"),
                "allowed_agents": list(manifest.owners),
                "max_risk": manifest.risk,
                "available": any(self._is_configured(item) for item in providers),
                "exposure": self._capability_exposure(capability, agent),
            }
            return entry
        entry = spec.planner_entry(
            available=any(self._is_configured(manifest) for manifest in providers),
            provider_count=len(providers),
        )
        if providers:
            # Execution manifests are the source of truth for concrete calling
            # schemas; the catalog remains the source of stable intent and
            # routing guidance. Validation above guarantees compatibility.
            entry["input_schema"] = providers[0].json_schema(capability=capability, direction="input")
            entry["output_schema"] = providers[0].json_schema(capability=capability, direction="output")
        entry["exposure"] = self._capability_exposure(capability, agent)
        return entry

    def load_package(self, package_dir: str | Path) -> ToolProviderManifest:
        """Load one Tool provider package without importing its handler."""
        package = Path(package_dir)
        manifest_path = package / "manifest.yaml"
        if not manifest_path.is_file():
            raise ToolContractError(f"Tool provider manifest is missing: {package}")
        if (package / "SKILL.md").exists():
            raise ToolContractError(
                f"Tool provider packages must not contain SKILL.md: {package}"
            )
        manifest = _read_manifest(manifest_path, package)
        if manifest.name != package.name:
            raise ToolContractError(f"manifest name does not match package directory: {package.name}")
        entrypoint_file, _ = _split_entrypoint(manifest.entrypoint)
        resolved_handler = (package / entrypoint_file).resolve()
        try:
            resolved_handler.relative_to(package.resolve())
        except ValueError as exc:
            raise ToolContractError(f"Tool entrypoint escapes package: {manifest.entrypoint}") from exc
        if not resolved_handler.is_file():
            raise ToolContractError(f"Tool entrypoint does not exist: {resolved_handler}")
        # Discovery validates metadata and indexes the package; code execution
        # is deferred until the first Tool call.
        self.register(manifest, None, package_dir=package, handler_path=resolved_handler, _discovered=True)
        return manifest

    def load_packages(self, root: str | Path) -> list[ToolProviderManifest]:
        """Load all packages, retaining valid packages when one package is bad."""
        root_path = Path(root)
        if not root_path.is_dir():
            raise ToolUnavailable(f"Tool provider package root does not exist: {root_path}", "package_root_missing")
        # Calling load_packages explicitly selects this directory for the
        # current router. Agent startup must not silently merge the canonical
        # root afterward; callers that need both roots can load the second
        # one explicitly.
        self._provider_root = root_path.resolve()
        self._provider_root_discovered = True
        manifests: list[ToolProviderManifest] = []
        loaded_names: set[str] = set()
        for package in sorted(root_path.iterdir(), key=lambda item: item.name):
            if not package.is_dir() or package.name.startswith("_"):
                continue
            # Explicit registrations are extensions, not a second provider
            # with the same package identity.
            if package.name in self._providers:
                continue
            try:
                manifest = self.load_package(package)
                manifests.append(manifest)
                loaded_names.add(manifest.name)
            except Exception as exc:
                self.load_errors.append(
                    {
                        "package": package.name,
                        "error_code": _error_code(exc),
                        "error": _safe_error(exc),
                    }
                )
        # Fallback references can only be checked after every package has been
        # discovered.  Do this as a quarantine pass: a malformed package is
        # removed from the routing index and reported, while unrelated valid
        # packages remain available.  Iteration also catches dependants that
        # become invalid after their fallback target is quarantined.
        while True:
            errors = self._fallback_errors()
            invalid = sorted(set(errors) & loaded_names)
            if not invalid:
                # A pre-existing registration may be the source of an error;
                # do not silently hide it, but do not evict packages outside
                # this discovery operation either.
                for name, message in sorted(errors.items()):
                    self.load_errors.append(
                        {
                            "package": name,
                            "error_code": "invalid_fallback",
                            "error": message,
                        }
                    )
                break
            for name in invalid:
                message = errors[name]
                self._unregister(name)
                loaded_names.remove(name)
                self.load_errors.append(
                    {
                        "package": name,
                        "error_code": "invalid_fallback",
                        "error": message,
                    }
                )
        return [manifest for manifest in manifests if manifest.name in self._providers]

    def call(
        self,
        agent: str,
        capability: str,
        payload: dict[str, Any] | None = None,
        *,
        run: TraceSink | None = None,
        attempt_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a capability through the deterministic routing funnel."""
        payload = dict(payload or {})
        input_hash = _hash_payload(payload)
        group_id = attempt_group_id or f"attempt_{uuid4().hex[:16]}"
        if capability == "search_tools":
            return self._search_tools(agent, payload, run=run, attempt_group_id=group_id)
        try:
            self._authorize_capability(agent, capability)
        except ToolPermissionError as exc:
            audit = self._preflight_audit(
                agent, capability, capability, group_id, "denied", exc,
                input_hash=input_hash,
            )
            self._record(audit, run)
            raise
        candidates = self._candidate_names(capability)
        if not candidates:
            error = ToolUnavailable(f"capability unavailable: {capability}", "capability_unavailable")
            audit = self._preflight_audit(
                agent, capability, capability, group_id, "unavailable", error,
                input_hash=input_hash,
            )
            self._record(audit, run)
            raise error

        # A deployment may install more than one configured production
        # provider for a capability. Select configured implementations before
        # attempting an unconfigured provider, while explicit registrations
        # remain strict so missing requirements fail loudly.
        configured_candidates = [
            candidate for candidate in candidates
            if self._providers.get(candidate) is not None
            and self._is_configured(self._providers[candidate].manifest)
        ]
        if configured_candidates and not any(candidate in self._explicit_provider_names for candidate in candidates):
            candidates = configured_candidates

        first_failed_provider: str | None = None
        previous_error: Exception | None = None
        for index, candidate in enumerate(candidates, start=1):
            provider = self._providers.get(candidate)
            if provider is None:
                error = ToolUnavailable(f"unknown Tool provider: {candidate}", "unknown_provider")
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, "unavailable", error, index,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                previous_error = error
                first_failed_provider = first_failed_provider or candidate
                continue

            manifest = provider.manifest
            try:
                self._authorize(agent, manifest)
                if capability in CAPABILITY_CATALOG:
                    validate_capability_input(capability, payload)
                manifest.validate_input(payload, capability)
                call_id = self._reserve_call(manifest, candidate, run=run)
            except ToolPermissionError as exc:
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, "denied", exc, index, manifest,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                raise
            except PermissionError as exc:
                wrapped = ToolPermissionError(str(exc))
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, "denied", wrapped, index, manifest,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                raise wrapped from exc
            except ToolContractError as exc:
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, "rejected_input", exc, index, manifest,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                raise
            except ToolBudgetExceeded as exc:
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, "budget_exceeded", exc, index, manifest,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                previous_error = exc
                first_failed_provider = first_failed_provider or candidate
                if exc.error_code == "global_budget_exceeded":
                    raise
                continue
            except ToolUnavailable as exc:
                audit_status = "timed_out" if exc.error_code == "timeout" else "unavailable"
                audit = self._preflight_audit(
                    agent, capability, candidate, group_id, audit_status, exc, index, manifest,
                    input_hash=input_hash,
                )
                self._record(audit, run)
                # Missing resources are an installation/configuration error,
                # not a provider execution failure. Falling through to an
                # unrelated fallback would make a deployment appear healthy
                # while silently changing the evidence path.
                if exc.error_code == "missing_configuration":
                    raise
                previous_error = exc
                first_failed_provider = first_failed_provider or candidate
                continue

            try:
                return self._execute(
                    provider,
                    agent,
                    capability,
                    payload,
                    call_id=call_id,
                    attempt_group_id=group_id,
                    attempt=index,
                    fallback_from=first_failed_provider,
                    previous_error_code=_error_code(previous_error) if first_failed_provider and previous_error else None,
                    run=run,
                )
            except (ToolPermissionError, PermissionError, ToolContractError, ToolBudgetExceeded):
                raise
            except Exception as exc:
                previous_error = exc
                first_failed_provider = first_failed_provider or candidate
                if manifest.failure_mode == "fallback" and index < len(candidates):
                    continue
                raise

        raise previous_error or ToolUnavailable(f"capability unavailable: {capability}", "capability_unavailable")

    def _search_tools(
        self,
        agent: str,
        payload: dict[str, Any],
        *,
        run: TraceSink | None,
        attempt_group_id: str,
    ) -> dict[str, Any]:
        """Execute the gateway-owned Tool search meta capability."""

        started = monotonic()
        try:
            validate_capability_input("search_tools", payload)
            requested = [str(item).strip() for item in payload["capabilities"]]
            if not requested or any(not item for item in requested):
                raise ToolContractError("tool search capabilities must contain at least one exact name")
            if len(set(requested)) != len(requested):
                raise ToolContractError("tool search capabilities must not contain duplicates")
            candidates: list[dict[str, Any]] = []
            visible = set(self._visible_capabilities(agent, None, include_catalog=False))
            for capability in requested:
                if capability not in visible:
                    raise ToolUnavailable(f"unknown deferred capability: {capability}", "capability_unavailable")
                if capability == "search_tools" or self._capability_exposure(capability, agent) != "deferred":
                    raise ToolContractError(f"capability is not a deferred Tool: {capability}")
                detail = self.capability_detail(capability, agent)
                candidates.append(detail)
            result = {"tools": candidates, "count": len(candidates)}
            audit = ToolCallAudit(
                call_id=self._new_audit_call_id(), agent=agent, capability="search_tools",
                provider="__gateway__", version="1.0.0", status="succeeded",
                input_hash=_hash_payload(payload), output_summary=_summarize(result),
                duration_ms=round((monotonic() - started) * 1000),
                attempt_group_id=attempt_group_id, attempt=1,
            )
            self._record(audit, run)
            return result
        except (ToolContractError, ToolUnavailable, ToolPermissionError, ToolBudgetExceeded) as exc:
            status = {
                "capability_unavailable": "unavailable",
                "unknown_capability": "unavailable",
                "capability_denied": "denied",
                "permission_denied": "denied",
                "budget_exceeded": "budget_exceeded",
                "global_budget_exceeded": "budget_exceeded",
                "provider_budget_exceeded": "budget_exceeded",
            }.get(getattr(exc, "error_code", "contract_error"), "rejected_input")
            audit = self._preflight_audit(
                agent,
                "search_tools",
                "__gateway__",
                attempt_group_id,
                status,
                exc,
                input_hash=_hash_payload(payload),
            )
            self._record(audit, run)
            raise
        except Exception as exc:
            error = ToolContractError(str(exc))
            audit = self._preflight_audit(
                agent,
                "search_tools",
                "__gateway__",
                attempt_group_id,
                "rejected_input",
                error,
                input_hash=_hash_payload(payload),
            )
            self._record(audit, run)
            raise error from exc

    def _visible_capabilities(
        self,
        agent: str | None,
        selected: set[str] | None,
        *,
        include_catalog: bool = True,
    ) -> list[str]:
        """Enumerate capabilities discovered from manifests, with catalog metadata when available."""

        names = set(self._capabilities)
        # In-code registrations may intentionally expose a catalog capability
        # before a provider is configured; keep it visible as unavailable.
        if selected is None:
            if include_catalog:
                names.update(CAPABILITY_CATALOG)
        else:
            names.intersection_update(selected)
        result: list[str] = []
        for capability in sorted(names):
            spec = CAPABILITY_CATALOG.get(capability)
            if spec is not None and agent is not None and spec.agents and agent not in spec.agents:
                continue
            if spec is None and agent is not None and not any(
                self._owner_matches(agent, self._providers[name].manifest)
                for name in self._capabilities.get(capability, ())
            ):
                continue
            if not self._capabilities.get(capability) and capability not in CAPABILITY_CATALOG:
                continue
            result.append(capability)
        return result

    def _capability_exposure(self, capability: str, agent: str | None = None) -> str:
        manifests = [
            self._providers[name].manifest
            for name in self._capabilities.get(capability, ())
            if agent is None or self._owner_matches(agent, self._providers[name].manifest)
        ]
        if not manifests:
            return "core"
        # A capability is core only when every eligible implementation marks it
        # core. This prevents a deferred provider from accidentally becoming a
        # permanently injected tool when a package is added later.
        return "core" if all(item.exposure == "core" for item in manifests) else "deferred"

    @staticmethod
    def _native_function(detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": str(detail["capability"]),
                "description": str(detail.get("description", "")),
                "parameters": dict(detail.get("input_schema") or {"type": "object", "properties": {}}),
                "strict": True,
            },
        }

    def _candidate_names(self, capability: str) -> tuple[str, ...]:
        names: list[str] = []
        providers = list(self._capabilities.get(capability, ()))
        for name in providers:
            self._append_unique(names, name)
            for fallback_name in self._providers[name].manifest.fallback_providers:
                self._append_unique(names, fallback_name)
        return tuple(names)

    def _authorize(self, agent: str, manifest: ToolProviderManifest) -> None:
        if not self._owner_matches(agent, manifest):
            raise ToolPermissionError(
                f"agent {agent} cannot call Tool provider: {manifest.name}",
                "owner_denied",
            )
        try:
            self.policy.check_tool_risk(manifest.risk, manifest.name)
        except PermissionError as exc:
            raise ToolPermissionError(str(exc), "write_denied") from exc
        missing = [key for key in manifest.requires if not self.config.get(key)]
        if missing:
            raise ToolUnavailable(
                f"Tool provider {manifest.name} missing requirements: {', '.join(missing)}",
                "missing_configuration",
            )

    def _authorize_capability(self, agent: str, capability: str) -> None:
        """Enforce the catalog's role boundary before provider resolution."""
        spec = CAPABILITY_CATALOG.get(capability)
        if spec is not None and spec.agents and agent not in spec.agents:
            raise ToolPermissionError(
                f"agent {agent} cannot request capability: {capability}",
                "capability_denied",
            )

    @staticmethod
    def _scope_key(run: TraceSink | None) -> str:
        if run is None:
            return "__global__"
        owner = getattr(run, "run", run)
        return str(getattr(owner, "run_id", "__global__"))

    def _reserve_call(self, manifest: ToolProviderManifest, provider_name: str, *, run: TraceSink | None = None) -> str:
        with self._lock:
            scope = self._scope_key(run)
            provider_counts = self._scoped_provider_call_counts.setdefault(scope, Counter())
            total_count = self._scoped_total_call_counts[scope]
            if provider_counts[provider_name] >= manifest.max_calls:
                raise ToolBudgetExceeded(
                    f"Tool call budget exceeded: {provider_name} {manifest.max_calls}",
                    "provider_budget_exceeded",
                )
            self._scoped_total_call_counts[scope] = total_count + 1
            provider_counts[provider_name] += 1
            # Preserve the old counters for unscoped callers and diagnostics.
            if scope == "__global__":
                self._total_call_count = self._scoped_total_call_counts[scope]
                self._provider_call_counts[provider_name] = provider_counts[provider_name]
            self._next_call_seq += 1
            return f"tool_call_{self._next_call_seq:08d}_{uuid4().hex[:8]}"

    def _execute(
        self,
        provider: _Provider,
        agent: str,
        capability: str,
        payload: dict[str, Any],
        *,
        call_id: str,
        attempt_group_id: str,
        attempt: int,
        fallback_from: str | None,
        previous_error_code: str | None,
        run: TraceSink | None,
    ) -> dict[str, Any]:
        started = monotonic()
        input_hash = _hash_payload(payload)
        executor: ThreadPoolExecutor | None = None
        future = None
        try:
            handler = self._ensure_handler(provider)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="echotrace-tool")
            operation = lambda: handler(payload)
            if self.provider_call_guard is None:
                future = executor.submit(operation)
            else:
                future = executor.submit(
                    self.provider_call_guard,
                    provider.manifest.name,
                    capability,
                    payload,
                    provider.manifest.timeout_seconds,
                    operation,
                )
            result = future.result(timeout=provider.manifest.timeout_seconds)
            provider.manifest.validate_output(result)
            audit = ToolCallAudit(
                call_id=call_id,
                agent=agent,
                capability=capability,
                provider=provider.manifest.name,
                version=provider.manifest.version,
                status="fallback_succeeded" if fallback_from else "succeeded",
                input_hash=input_hash,
                output_summary=_summarize(result),
                duration_ms=round((monotonic() - started) * 1000),
                fallback_from=fallback_from,
                previous_error_code=previous_error_code,
                attempt_group_id=attempt_group_id,
                attempt=attempt,
                manifest_hash=_manifest_hash(provider.manifest),
            )
            self._record(audit, run)
            return result
        except FutureTimeout as exc:
            if future is not None:
                future.cancel()
            error = ToolTimeout(
                f"Tool provider {provider.manifest.name} exceeded timeout: {provider.manifest.timeout_seconds}s"
            )
            audit = self._execution_audit(
                call_id, agent, capability, provider.manifest, input_hash, started,
                "timed_out", error, fallback_from, previous_error_code,
                attempt_group_id, attempt,
            )
            self._record(audit, run)
            raise error from exc
        except Exception as exc:
            audit = self._execution_audit(
                call_id, agent, capability, provider.manifest, input_hash, started,
                "failed", exc, fallback_from, previous_error_code,
                attempt_group_id, attempt,
            )
            self._record(audit, run)
            raise
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def _execution_audit(
        self,
        call_id: str,
        agent: str,
        capability: str,
        manifest: ToolProviderManifest,
        input_hash: str,
        started: float,
        status: str,
        error: Exception,
        fallback_from: str | None,
        previous_error_code: str | None,
        attempt_group_id: str,
        attempt: int,
    ) -> ToolCallAudit:
        return ToolCallAudit(
            call_id=call_id,
            agent=agent,
            capability=capability,
            provider=manifest.name,
            version=manifest.version,
            status=status,
            input_hash=input_hash,
            duration_ms=round((monotonic() - started) * 1000),
            error=_safe_error(error),
            error_code=_error_code(error),
            fallback_from=fallback_from,
            previous_error_code=previous_error_code,
            attempt_group_id=attempt_group_id,
            attempt=attempt,
            manifest_hash=_manifest_hash(manifest),
        )

    def _preflight_audit(
        self,
        agent: str,
        capability: str,
        provider_name: str,
        attempt_group_id: str,
        status: str,
        error: Exception,
        attempt: int = 1,
        manifest: ToolProviderManifest | None = None,
        input_hash: str = "",
    ) -> ToolCallAudit:
        return ToolCallAudit(
            call_id=self._new_audit_call_id(),
            agent=agent,
            capability=capability,
            provider=provider_name,
            version=manifest.version if manifest else "unknown",
            status=status,
            input_hash=input_hash,
            duration_ms=0,
            error=_safe_error(error),
            error_code=_error_code(error),
            attempt_group_id=attempt_group_id,
            attempt=attempt,
            manifest_hash=_manifest_hash(manifest) if manifest else None,
        )

    def _record(self, audit: ToolCallAudit, run: TraceSink | None) -> None:
        with self._lock:
            self.audits.append(audit)
            if len(self.audits) > self.max_in_memory_audits:
                del self.audits[: len(self.audits) - self.max_in_memory_audits]
        if self.audit_sink is not None:
            self.audit_sink(audit)
        if run is not None:
            payload = audit.to_dict()
            # ``status`` is the positional trace status; passing the same key
            # again as a keyword raises before the call can be audited.
            payload.pop("status", None)
            run.add_trace("provider_call", audit.status, **payload)

    def _new_audit_call_id(self) -> str:
        with self._lock:
            self._next_call_seq += 1
            return f"tool_event_{self._next_call_seq:08d}_{uuid4().hex[:8]}"

    def _call_count(self, run: TraceSink | None = None) -> int:
        return self._scoped_total_call_counts.get(self._scope_key(run), self._total_call_count)

    def _is_configured(self, manifest: ToolProviderManifest) -> bool:
        return all(self.config.get(key) for key in manifest.requires)

    def _ensure_handler(self, provider: _Provider) -> Callable[[dict[str, Any]], dict[str, Any]]:
        if provider.handler is not None:
            return provider.handler
        if provider.handler_path is None:
            raise ToolUnavailable(f"Tool provider has no executable handler: {provider.manifest.name}", "handler_missing")
        _, callable_name = _split_entrypoint(provider.manifest.entrypoint)
        module_suffix = sha256(str(provider.handler_path.resolve()).encode("utf-8")).hexdigest()[:12]
        module_name = f"echotrace_provider_{provider.manifest.name.replace('-', '_')}_{module_suffix}"
        spec = importlib.util.spec_from_file_location(module_name, provider.handler_path)
        if spec is None or spec.loader is None:
            raise ToolContractError(f"cannot load Tool handler: {provider.handler_path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ToolUnavailable(
                f"failed to load Tool handler: {provider.package_dir}", "handler_load_failed"
            ) from exc
        handler = getattr(module, callable_name, None)
        if not callable(handler):
            raise ToolContractError(
                f"Tool handler must export {callable_name}(payload): {provider.handler_path}"
            )
        with self._lock:
            provider.handler = handler
        return handler

    def _validate_fallback_targets(self) -> None:
        errors = self._fallback_errors()
        if errors:
            raise ToolContractError(next(iter(errors.values())))

    def _fallback_errors(self) -> dict[str, str]:
        """Return package-local fallback errors without mutating the index."""
        errors: dict[str, str] = {}
        for name, provider in sorted(self._providers.items()):
            for fallback in provider.manifest.fallback_providers:
                if fallback not in self._providers:
                    errors[name] = f"Tool provider {name} references unknown fallback: {fallback}"
                    break
                missing_capabilities = sorted(
                    set(provider.manifest.capabilities)
                    - set(self._providers[fallback].manifest.capabilities)
                )
                if missing_capabilities:
                    errors[name] = (
                        f"Tool provider {name} fallback {fallback} does not implement: "
                        f"{', '.join(missing_capabilities)}"
                    )
                    break

        # Mark every node participating in a cycle.  A DFS is preferable to
        # raising at the first edge because load_packages can quarantine all
        # members in one pass and then re-check their dependants.
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(name: str) -> None:
            if state.get(name) == 1:
                try:
                    start = stack.index(name)
                except ValueError:
                    start = 0
                for cycle_name in stack[start:]:
                    errors.setdefault(cycle_name, f"fallback cycle detected at: {cycle_name}")
                return
            if state.get(name) == 2 or name not in self._providers:
                return
            state[name] = 1
            stack.append(name)
            for fallback in self._providers[name].manifest.fallback_providers:
                if fallback in self._providers:
                    visit(fallback)
            stack.pop()
            state[name] = 2

        for name in sorted(self._providers):
            visit(name)
        return errors

    def _unregister(self, provider_name: str) -> None:
        """Remove a quarantined package from both the provider and capability indexes."""
        self._providers.pop(provider_name, None)
        for capability, names in list(self._capabilities.items()):
            filtered = [name for name in names if name != provider_name]
            if filtered:
                self._capabilities[capability] = filtered
            else:
                self._capabilities.pop(capability, None)

    @staticmethod
    def _owner_matches(agent: str, manifest: ToolProviderManifest) -> bool:
        return not manifest.owners or agent in manifest.owners or "*" in manifest.owners

    @staticmethod
    def _append_unique(names: list[str], value: str) -> None:
        if value not in names:
            names.append(value)

    def _sort_key(self, provider_name: str) -> tuple[int, int, str]:
        manifest = self._providers[provider_name].manifest
        # Explicit registrations are deployment/test overrides. They remain
        # visible in the same capability catalog but win over discovered
        # packages deterministically, independent of package priority.
        explicit_rank = 0 if provider_name in self._explicit_provider_names else 1
        return explicit_rank, manifest.priority, manifest.name


def _read_manifest(
    path: Path,
    package_dir: Path | None = None,
) -> ToolProviderManifest:
    try:
        import yaml
    except ImportError as exc:
        raise ToolUnavailable("PyYAML is required to load Tool manifests", "manifest_dependency_missing") from exc
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ToolContractError(f"Tool manifest must be an object: {path}")
    kind = str(payload.get("kind", "tool_provider")).strip()
    if kind != "tool_provider":
        raise ToolContractError(f"Tool manifest kind must be tool_provider: {path}")
    manifest_name = str(payload.get("name", "")).strip()
    manifest_description = str(payload.get("description") or "").strip()
    if not manifest_description:
        raise ToolContractError(f"Tool description is required in manifest: {path}")
    if "exposure" not in payload:
        raise ToolContractError(f"Tool exposure is required in manifest: {path}")
    exposure_reason = str(payload.get("exposure_reason") or "").strip()
    if not exposure_reason:
        raise ToolContractError(f"Tool exposure_reason is required in manifest: {path}")
    budget = payload.get("budget") or {}
    if not isinstance(budget, dict):
        raise ToolContractError(f"Tool budget must be an object: {path}")
    try:
        manifest = ToolProviderManifest(
            name=manifest_name,
            version=str(payload.get("version", "1.0.0")),
            description=manifest_description,
            owners=_as_tuple(payload.get("owners", ())),
            capabilities=_as_tuple(payload.get("capabilities", ())),
            input_schema=dict(payload.get("input", {})),
            capability_inputs=_as_schema_map(payload.get("capability_inputs")),
            input_json_schema=_read_json_schema(package_dir / "schemas" / "input.schema.json") if package_dir else None,
            input_descriptions={str(key): str(value) for key, value in dict(payload.get("input_descriptions") or {}).items()},
            output_schema=dict(payload.get("output", {})),
            output_json_schema=_read_json_schema(package_dir / "schemas" / "output.schema.json") if package_dir else None,
            output_descriptions={str(key): str(value) for key, value in dict(payload.get("output_descriptions") or {}).items()},
            use_when=_as_tuple(payload.get("use_when", ())),
            avoid_when=_as_tuple(payload.get("avoid_when", ())),
            exposure=str(payload.get("exposure", "core")),
            exposure_reason=exposure_reason,
            allow_custom_capabilities=bool(payload.get("allow_custom_capabilities", False)),
            risk=str(payload.get("risk", "read")),
            requires=_as_tuple(payload.get("requires", ())),
            max_calls=int(budget.get("max_calls", 1)),
            timeout_seconds=float(budget.get("timeout_seconds", 30)),
            failure_mode=str(payload.get("failure_mode", "fail")),
            priority=int(payload.get("priority", 100)),
            fallback_providers=_as_tuple(payload.get("fallback_providers", ())),
            entrypoint=str(payload.get("entrypoint", "handler.py:run")),
        )
        manifest.validate_capabilities(FORMAL_CAPABILITIES)
        manifest.validate_capability_contracts()
        manifest.validate_output_contracts()
        return manifest
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ToolContractError):
            raise
        raise ToolContractError(f"invalid Tool manifest: {path}") from exc


def _read_json_schema(path: Path) -> dict[str, Any] | None:
    """Read an optional full JSON Schema sidecar without importing a handler."""

    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolContractError(f"invalid Tool JSON Schema: {path}") from exc
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ToolContractError(f"Tool JSON Schema must be an object schema: {path}")
    return value


def _split_entrypoint(value: str) -> tuple[str, str]:
    file_path, separator, callable_name = value.partition(":")
    if not separator or not file_path or not callable_name or callable_name.strip() != callable_name:
        raise ToolContractError(f"invalid Tool entrypoint: {value}")
    if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
        raise ToolContractError(f"invalid Tool entrypoint path: {value}")
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", callable_name):
        raise ToolContractError(f"invalid Tool entrypoint callable: {value}")
    return file_path, callable_name


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _as_schema_map(value: Any) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolContractError("capability_inputs must be an object")
    result: dict[str, dict[str, str]] = {}
    for capability, schema in value.items():
        if not isinstance(schema, dict):
            raise ToolContractError(f"capability input contract must be an object: {capability}")
        result[str(capability)] = {str(key): str(expected) for key, expected in schema.items()}
    return result


def _error_code(error: Exception) -> str:
    explicit = getattr(error, "error_code", None)
    if explicit:
        return str(explicit)
    if isinstance(error, ToolContractError):
        return "contract_error"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "execution_error"


def _safe_error(error: Exception) -> str:
    value = " ".join(str(error).split())
    return value[:240] if value else error.__class__.__name__


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def _manifest_hash(manifest: ToolProviderManifest) -> str:
    """Hash the execution contract without retaining handler code or secrets."""
    payload = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "owners": list(manifest.owners),
        "capabilities": list(manifest.capabilities),
        "input_schema": manifest.input_schema,
        "capability_inputs": manifest.capability_inputs,
        "output_schema": manifest.output_schema,
        "risk": manifest.risk,
        "requires": list(manifest.requires),
        "failure_mode": manifest.failure_mode,
        "input_descriptions": manifest.input_descriptions,
        "output_descriptions": manifest.output_descriptions,
        "input_json_schema": manifest.input_json_schema,
        "output_json_schema": manifest.output_json_schema,
        "use_when": list(manifest.use_when),
        "avoid_when": list(manifest.avoid_when),
        "exposure": manifest.exposure,
        "exposure_reason": manifest.exposure_reason,
        "allow_custom_capabilities": manifest.allow_custom_capabilities,
        "max_calls": manifest.max_calls,
        "timeout_seconds": manifest.timeout_seconds,
        "priority": manifest.priority,
        "fallback_providers": list(manifest.fallback_providers),
        "entrypoint": manifest.entrypoint,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(result),
        "item_counts": {key: len(value) for key, value in result.items() if isinstance(value, list)},
    }
