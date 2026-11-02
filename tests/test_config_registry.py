from app.config import ALLOWED_KEYS, DEFAULT_VALUES, SETTING_DEFINITIONS, snapshot


def test_node_task_deadline_is_a_registered_setting():
    definition = next(item for item in SETTING_DEFINITIONS if item["key"] == "ECHOTRACE_NODE_TASK_DEADLINE_SECONDS")

    assert definition["key"] in ALLOWED_KEYS
    assert DEFAULT_VALUES[definition["key"]] == "1200"


def test_llm_token_rate_window_matches_the_context_default():
    definition = next(item for item in SETTING_DEFINITIONS if item["key"] == "ECHOTRACE_LLM_TOKENS_PER_MINUTE")

    assert definition["key"] in ALLOWED_KEYS
    assert DEFAULT_VALUES[definition["key"]] == "1000000"


def test_settings_snapshot_uses_simplified_chinese_labels_and_categories():
    settings = {item["key"]: item for item in snapshot()}

    assert settings["ECHOTRACE_AGENT_CONTRACT_RETRIES"]["label"] == "智能体交付校验重试次数"
    assert settings["ECHOTRACE_AGENT_CONTRACT_RETRIES"]["category"] == "运行限制"
    assert settings["ECHOTRACE_AGENT_CONTRACT_RETRIES"]["description"].startswith("智能体交付内容")
    assert settings["ECHOTRACE_LLM_BASE_URL"]["label"] == "模型服务地址"
