"""Opt-in acceptance run against the configured model, MySQL and Bocha search."""

from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.integration


def _case(case_id: str) -> dict:
    path = Path(__file__).parents[1] / "fixtures" / "real_investigations.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    return next(item for item in cases if item["case_id"] == case_id)


def _load_local_environment() -> None:
    from app.config import _read_file

    for key, value in _read_file().items():
        os.environ.setdefault(key, value)


def _plain(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _diagnostic_events(log_dir: Path, filename: str, run_id: str) -> list[dict]:
    events = []
    for path in log_dir.glob(f"{filename}*"):
        if not path.is_file() or path.name.endswith(".lock"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("run_id") == run_id:
                events.append(event)
    return events


@pytest.mark.skipif(
    os.environ.get("ECHOTRACE_RUN_REAL_AGENT") != "1",
    reason="set ECHOTRACE_RUN_REAL_AGENT=1 to run the live model/Bocha/MySQL acceptance",
)
def test_real_investigation_completes_the_real_node_loop():
    _load_local_environment()
    # Live acceptance needs room for source discovery, targeted supplements,
    # and a structured draft. The production defaults remain independently
    # configurable; this override is limited to the explicit acceptance run.
    acceptance_steps = int(os.getenv("ECHOTRACE_REAL_AGENT_MAX_STEPS", "192"))
    configured_steps = int(os.getenv("ECHOTRACE_AGENT_MAX_STEPS", "64"))
    acceptance_node_deadline = max(
        int(os.getenv("ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "1200")),
        int(os.getenv("ECHOTRACE_REAL_AGENT_NODE_DEADLINE_SECONDS", "7200")),
    )
    os.environ["ECHOTRACE_AGENT_MAX_STEPS"] = str(max(configured_steps, acceptance_steps))
    os.environ["ECHOTRACE_RESEARCH_AGENT_MAX_STEPS"] = str(acceptance_steps)
    os.environ["ECHOTRACE_NODE_TASK_DEADLINE_SECONDS"] = str(acceptance_node_deadline)
    os.environ["ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS"] = str(max(
        int(os.getenv("ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS", "2400")),
        acceptance_node_deadline,
    ))
    os.environ.setdefault("ECHOTRACE_FILE_LOGS", "1")
    from app.investigation_composition import build_investigation_runtime
    from echotrace.application.report_renderers import render_report_html
    from echotrace.application.report_templates import load_report_template
    from echotrace.domain.models import Event, InvestigationRun
    from echotrace.domain.reporting import ReportDocument
    from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork
    from echotrace.infrastructure.persistence.mysql.models import InvestigationTask
    from echotrace.infrastructure.coordination.redis.config import RedisConfig

    case = _case(os.getenv("ECHOTRACE_REAL_AGENT_CASE_ID", "wuhan-university-library"))
    runtime = build_investigation_runtime()
    worker_process = None
    worker_output = None
    try:
        redis_config = RedisConfig.from_env()
        if redis_config.host not in {"127.0.0.1", "localhost", "::1"} and not ipaddress.ip_address(
            redis_config.host
        ).is_private:
            raise AssertionError(
                f"real acceptance requires a local/private Redis service, got {redis_config.redacted_url()}"
            )
        runtime.redis_bus.client.ping()
        with runtime.database.engine.connect() as connection:
            database_name = connection.exec_driver_sql("SELECT DATABASE()").scalar_one()
            if database_name != "echotrace":
                raise AssertionError(f"real acceptance is restricted to local development database echotrace, got {database_name!r}")

        run = InvestigationRun(
            objective=case["objective"],
            event=Event(title=case["subject"], objective=case["objective"]),
            investigation_type=case["investigation_type"],
            subject=case["subject"],
            scope={"source_limit": case["acceptance"]["min_documents"] + 3},
        )
        runtime.run_store.save(run)
        log_dir = Path(os.getenv("ECHOTRACE_LOG_DIR", "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        worker_log_path = log_dir / f"real-acceptance-worker-{run.run_id}.log"
        worker_output = worker_log_path.open("ab")
        worker_process = subprocess.Popen(
            [sys.executable, "-m", "echotrace.worker"],
            cwd=Path(__file__).parents[2],
            env=os.environ.copy(),
            stdout=worker_output,
            stderr=subprocess.STDOUT,
        )
        time.sleep(1)
        if worker_process.poll() is not None:
            raise AssertionError(
                f"production worker exited during startup with {worker_process.returncode}; see {worker_log_path}"
            )
        runtime.enqueue_run(run.run_id, idempotency_key=f"{run.run_id}:real-acceptance:start")

        timeout_seconds = max(60, int(os.getenv("ECHOTRACE_REAL_AGENT_WALL_TIMEOUT_SECONDS", "43200")))
        deadline = time.monotonic() + timeout_seconds
        last_recovery = 0.0
        last_task_check = 0.0
        try:
            while True:
                completed = runtime.run_store.get(run.run_id)
                if completed is None:
                    raise AssertionError(f"run disappeared during acceptance: {run.run_id}")
                if completed.status.value in {"paused", "failed", "cancelled"}:
                    break
                if completed.status.value == "completed":
                    with SqlAlchemyUnitOfWork(runtime.database.session_factory) as uow:
                        task_rows = uow.session.query(InvestigationTask).filter_by(run_id=run.run_id).all()
                    if task_rows and all(task.status == "succeeded" for task in task_rows):
                        break
                if time.monotonic() - last_task_check >= 1.0:
                    with SqlAlchemyUnitOfWork(runtime.database.session_factory) as uow:
                        failed_tasks = uow.session.query(InvestigationTask).filter_by(
                            run_id=run.run_id, status="failed",
                        ).all()
                    if failed_tasks:
                        failures = [
                            {
                                "task_id": task.task_id,
                                "agent": task.agent,
                                "stage": task.stage,
                                "slot_id": task.slot_id,
                                "error_code": task.error_code,
                                "error_summary": task.error_summary,
                            }
                            for task in failed_tasks
                        ]
                        raise AssertionError(
                            f"real investigation contains failed role tasks; run_id={run.run_id}; "
                            f"failures={failures[:8]}"
                        )
                    last_task_check = time.monotonic()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"real investigation did not finish within {timeout_seconds}s; run_id={run.run_id}; "
                        f"worker_log={worker_log_path}"
                    )
                if worker_process.poll() is not None:
                    raise AssertionError(
                        f"production worker exited with {worker_process.returncode}; "
                        f"run_id={run.run_id}; worker_log={worker_log_path}"
                    )
                time.sleep(0.2)
        finally:
            if worker_process is not None and worker_process.poll() is None:
                worker_process.terminate()
                try:
                    worker_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker_process.kill()
                    worker_process.wait(timeout=10)
            if worker_output is not None:
                worker_output.close()

        completed = runtime.run_store.get(run.run_id)
        assert completed is not None
        assert completed.status.value == "completed", completed.error
        assert len(completed.documents) >= case["acceptance"]["min_documents"]

        rows = runtime.controller.node_store.list_nodes(completed.run_id)
        assert rows and all(row["status"] == "frozen" for row in rows)
        assert len(rows) == 6
        document_ids = {document.document_id for document in completed.documents}
        for row in rows:
            for revision in range(4):
                saved = runtime.controller.node_store.get_revision(row["node_id"], revision)
                assert saved is not None
                draft = saved["draft"]
                assert draft["claims"] and draft["evidence"]
                for claim in draft["claims"]:
                    assert set(claim["source_document_ids"]).issubset(document_ids)
                    linked = [item for item in draft["evidence"] if item["evidence_id"] in claim["evidence_ids"]]
                    assert linked
                    assert all(_plain(claim["text"]) != _plain(item["excerpt"]) for item in linked)
                if revision:
                    review = runtime.controller.node_store.get_review(row["node_id"], revision)
                    assert review is not None
                    payload = review["review"]
                    assert payload["rewrite_instructions"]
                    assert payload["final_editor_note"]
                    assert payload["revision"] == revision

        assert any(
            event.get("step") == "provider_call"
            and event.get("provider") == "search-web-bocha"
            and event.get("status") in {"succeeded", "fallback_succeeded"}
            for event in runtime.run_store.events(completed.run_id)
        )
        assert any(
            event.get("step") == "agent_turn"
            and (event.get("role") or event.get("agent")) == "orchestrator"
            for event in runtime.run_store.events(completed.run_id)
        )
        referenced_documents = {
            evidence["document_id"]
            for row in rows
            for evidence in (runtime.controller.node_store.get_revision(row["node_id"], 3)["draft"]["evidence"])
        }
        assert referenced_documents
        assert any(runtime.run_store.list_analysis_features(document_id) for document_id in document_ids)
        assert completed.report_document, "the Report Composer role stream did not persist a report"
        report = ReportDocument.from_dict(completed.report_document)
        report_template = load_report_template(completed.investigation_type)
        assert report.run_id == completed.run_id
        assert report.investigation_type == completed.investigation_type
        assert report.template_id == report_template.template_id
        assert report.template_version == report_template.version
        assert report.template_content_hash == report_template.content_hash
        assert len(report.chapters) == len(report_template.chapters)
        assert all(chapter.blocks for chapter in report.chapters)
        assert any(block.citations for chapter in report.chapters for block in chapter.blocks)
        rendered = render_report_html(report)
        assert rendered.startswith("<!doctype html>")
        assert "<section" in rendered and report.title in rendered

        with SqlAlchemyUnitOfWork(runtime.database.session_factory) as uow:
            task_rows = uow.session.query(InvestigationTask).filter_by(run_id=run.run_id).all()
        assert task_rows and all(task.status == "succeeded" for task in task_rows)
        assert any(task.agent == "researcher" for task in task_rows)
        assert any(task.agent == "verifier" for task in task_rows)
        assert any(task.agent == "report_composer" for task in task_rows)

        stream_groups = {}
        for role in ("orchestrator", "researcher", "verifier", "report_composer"):
            stream = runtime.redis_bus.keys.task_stream(role)
            group_name = runtime.redis_bus.keys.task_group(role)
            assert runtime.redis_bus.client.xlen(stream) > 0, f"no real messages were written to {role} stream"
            group = next(
                (item for item in runtime.redis_bus.client.xinfo_groups(stream) if item.get("name") == group_name),
                None,
            )
            assert group is not None, f"missing Redis consumer group for {role}"
            assert int(group.get("pending") or 0) == 0, f"unacknowledged messages remain in {role} stream"
            stream_groups[role] = group_name

        agent_turns = _diagnostic_events(log_dir, "agent-turns.jsonl", run.run_id)
        context_compactions = _diagnostic_events(log_dir, "context-compactions.jsonl", run.run_id)
        provider_errors = _diagnostic_events(log_dir, "provider-errors.jsonl", run.run_id)
        assert agent_turns, f"no agent-turn diagnostics recorded for run {run.run_id}"
        linked_fields = ("run_id", "task_id", "request_id", "prompt_hash", "context_pack_hash")
        unlinked_turns = [
            {key: event.get(key) for key in linked_fields if not event.get(key)}
            for event in agent_turns
            if any(not event.get(key) for key in linked_fields)
        ]
        assert not unlinked_turns, f"Agent request diagnostics lack correlation fields: {unlinked_turns[:3]}"
        unlinked_compactions = [
            {key: event.get(key) for key in linked_fields if not event.get(key)}
            for event in context_compactions
            if any(not event.get(key) for key in linked_fields)
        ]
        assert not unlinked_compactions, f"context compaction diagnostics lack correlation fields: {unlinked_compactions[:3]}"
        unlinked_errors = [
            {key: event.get(key) for key in linked_fields if not event.get(key)}
            for event in provider_errors
            if any(not event.get(key) for key in linked_fields)
        ]
        assert not unlinked_errors, f"provider error diagnostics lack correlation fields: {unlinked_errors[:3]}"
        print(
            "REAL_ACCEPTANCE "
            f"run_id={run.run_id} nodes={len(rows)} documents={len(completed.documents)} "
            f"report_chapters={len(report.chapters)} "
            f"report_blocks={sum(len(chapter.blocks) for chapter in report.chapters)} "
            f"agent_turns={len(agent_turns)} context_compactions={len(context_compactions)} "
            f"provider_errors={len(provider_errors)} streams={stream_groups} worker_log={worker_log_path}"
        )
    finally:
        if worker_process is not None and worker_process.poll() is None:
            worker_process.terminate()
            try:
                worker_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_process.kill()
                worker_process.wait(timeout=10)
        if worker_output is not None and not worker_output.closed:
            worker_output.close()
        runtime.close()
