from __future__ import annotations

import os

from echotrace.runtime.environment import load_local_environment


def test_load_local_environment_preserves_process_values_and_skips_placeholders(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FROM_FILE=value\n"
        "EXISTING=from-file\n"
        "UNRESOLVED=${HOST_VALUE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "from-process")
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.delenv("UNRESOLVED", raising=False)

    load_local_environment(env_file)

    assert os.environ["FROM_FILE"] == "value"
    assert os.environ["EXISTING"] == "from-process"
    assert "UNRESOLVED" not in os.environ
