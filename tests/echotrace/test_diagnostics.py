from __future__ import annotations

import json

from echotrace.agent.diagnostics import JsonlDiagnostics, redact_diagnostic


def test_redaction_removes_secrets_source_text_and_url_query_tokens():
    value = redact_diagnostic({
        "api_key": "secret-key",
        "authorization": "Bearer secret",
        "content": "网页正文不应进入日志",
        "url": "https://example.test/page?token=secret&lang=zh",
        "nested": {"reasoning_content": "hidden reasoning"},
    })

    assert value["api_key"] == "[redacted]"
    assert value["content"] == "[redacted]"
    assert "secret" not in value["url"]
    assert "lang=zh" in value["url"]
    assert value["nested"]["reasoning_content"] == "[redacted]"


def test_jsonl_writer_bounds_records_and_rotates(tmp_path):
    writer = JsonlDiagnostics(
        tmp_path,
        file_max_bytes=420,
        backup_count=1,
        total_max_bytes=900,
        record_max_bytes=300,
        retention_days=None,
    )
    for index in range(12):
        writer.write("agent_turn", {
            "kind": "agent_turn",
            "index": index,
            "content": "large source body " * 100,
            "prompt": "full prompt should be removed",
        })

    files = sorted(tmp_path.glob("agent-turns.jsonl*"))
    assert files
    assert len(files) <= 2
    assert sum(path.stat().st_size for path in files) <= 900
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert len(line.encode("utf-8")) + 1 <= 300
            record = json.loads(line)
            assert record.get("content") in (None, "[redacted]")
            assert "full prompt" not in line


def test_writer_failure_isolated_from_caller(tmp_path):
    writer = JsonlDiagnostics(tmp_path / "missing", file_max_bytes=256)
    writer.write("agent_turn", {"kind": "agent_turn", "safe": True})
    assert (tmp_path / "missing" / "agent-turns.jsonl").exists()
