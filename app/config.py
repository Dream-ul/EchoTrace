"""Explicit, repository-standard settings registry for the EchoTrace workbench."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

MASK = "********"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

SETTING_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"key": "ECHOTRACE_LLM_BASE_URL", "label": "Model Base URL", "category": "Agent", "description": "OpenAI-compatible model endpoint.", "secret": False},
    {"key": "ECHOTRACE_LLM_MODEL", "label": "Model name", "category": "Agent", "description": "Model used by the investigation Agents.", "secret": False},
    {"key": "ECHOTRACE_LLM_API_KEY", "label": "Model API key", "category": "Agent", "description": "Authentication key; only configured status is shown.", "secret": True},
    {"key": "TAVILY_API_KEY", "label": "Tavily API key", "category": "Search", "description": "Tavily web-search credential.", "secret": True},
    {"key": "TAVILY_BASE_URL", "label": "Tavily URL", "category": "Search", "description": "Tavily search endpoint.", "secret": False},
    {"key": "BOCHA_WEB_SEARCH_API_KEY", "label": "Bocha API key", "category": "Search", "description": "Bocha Chinese web-search credential.", "secret": True},
    {"key": "BOCHA_BASE_URL", "label": "Bocha URL", "category": "Search", "description": "Bocha web-search endpoint.", "secret": False},
    {"key": "ECHOTRACE_PUBLIC_FETCH_ENABLED", "label": "Fetch public pages", "category": "Search", "description": "Allow fetching the body of known public URLs.", "secret": False, "control": "select", "options": ["0", "1"]},
    {"key": "ECHOTRACE_PERSISTENCE", "label": "Persistence", "category": "Storage", "description": "MySQL is the only supported investigation store.", "secret": False, "control": "select", "options": ["mysql"]},
    {"key": "ECHOTRACE_DATABASE_URL", "label": "MySQL connection URL", "category": "Storage", "description": "Required for mysql persistence.", "secret": True},
    {"key": "ECHOTRACE_REDIS_HOST", "label": "Redis host", "category": "Storage", "description": "Coordination Redis host.", "secret": False},
    {"key": "ECHOTRACE_REDIS_PORT", "label": "Redis port", "category": "Storage", "description": "Coordination Redis port.", "secret": False},
    {"key": "ECHOTRACE_REDIS_PASSWORD", "label": "Redis password", "category": "Storage", "description": "Redis authentication password.", "secret": True},
    {"key": "ECHOTRACE_REDIS_DB", "label": "Redis database", "category": "Storage", "description": "Redis logical database number.", "secret": False},
    {"key": "ECHOTRACE_REDIS_TIMEOUT", "label": "Redis timeout (seconds)", "category": "Storage", "description": "Redis connection and operation timeout.", "secret": False},
    {"key": "ECHOTRACE_REDIS_URL", "label": "Redis URL", "category": "Storage", "description": "Complete Redis URL, when used by an adapter.", "secret": True},
    {"key": "ECHOTRACE_REDIS_TLS", "label": "Redis TLS", "category": "Storage", "description": "Use TLS for Redis connections when not encoded in the URL.", "secret": False, "control": "select", "options": ["0", "1"]},
    {"key": "S3_ENDPOINT_URL", "label": "Object store endpoint", "category": "Storage", "description": "Required S3-compatible endpoint, normally the RustFS service in Compose.", "secret": False},
    {"key": "S3_BUCKET", "label": "Object store bucket", "category": "Storage", "description": "Bucket for immutable source and media objects.", "secret": False},
    {"key": "S3_ACCESS_KEY_ID", "label": "Object store access key", "category": "Storage", "description": "S3-compatible access key.", "secret": True},
    {"key": "S3_SECRET_ACCESS_KEY", "label": "Object store secret", "category": "Storage", "description": "S3-compatible secret key.", "secret": True},
    {"key": "S3_REGION", "label": "Object store region", "category": "Storage", "description": "S3 signing region.", "secret": False},
    {"key": "S3_USE_TLS", "label": "Object store TLS", "category": "Storage", "description": "Use HTTPS for the S3-compatible endpoint.", "secret": False, "control": "select", "options": ["0", "1"]},
    {"key": "S3_MAX_OBJECT_BYTES", "label": "Maximum object bytes", "category": "Storage", "description": "Maximum raw or media object size.", "secret": False},
    {"key": "ECHOTRACE_LLM_TIMEOUT_SECONDS", "label": "Model timeout (seconds)", "category": "Run limits", "description": "Maximum wait for one model request attempt.", "secret": False},
    {"key": "ECHOTRACE_LLM_MAX_RETRIES", "label": "Model request retries", "category": "Run limits", "description": "Bounded transport-level retries per model request.", "secret": False},
    {"key": "ECHOTRACE_LLM_MAX_TOKENS", "label": "Maximum output tokens", "category": "Run limits", "description": "Output budget for investigation and report turns.", "secret": False},
    {"key": "ECHOTRACE_LLM_TOKENS_PER_MINUTE", "label": "LLM token rate window", "category": "Run limits", "description": "Redis token admission capacity per minute; defaults to the configured model context window.", "secret": False},
    {"key": "ECHOTRACE_LLM_CONTEXT_WINDOW", "label": "Model context window", "category": "Run limits", "description": "Provider context window used by the conservative request estimator.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_TARGET_RATIO", "label": "Context target ratio", "category": "Run limits", "description": "Target input ratio before provider context limit.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_SOFT_RATIO", "label": "Context soft compression ratio", "category": "Run limits", "description": "Start role-aware context reduction at this input ratio.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_HARD_RATIO", "label": "Context hard compression ratio", "category": "Run limits", "description": "Last reduction threshold before provider input limit.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_BUDGET_ORCHESTRATOR", "label": "Orchestrator context budget", "category": "Run limits", "description": "Optional role-specific estimated input token target.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_BUDGET_RESEARCHER", "label": "Researcher context budget", "category": "Run limits", "description": "Optional role-specific estimated input token target.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_BUDGET_VERIFIER", "label": "Verifier context budget", "category": "Run limits", "description": "Optional role-specific estimated input token target.", "secret": False},
    {"key": "ECHOTRACE_CONTEXT_BUDGET_REPORT_COMPOSER", "label": "Report context budget", "category": "Run limits", "description": "Optional role-specific estimated input token target.", "secret": False},
    {"key": "ECHOTRACE_LLM_THINKING", "label": "Thinking 模式", "category": "Agent", "description": "开启后模型先推理再回答；自动改用更大的输出预算（见 Thinking 模式输出预算）。默认关闭。", "secret": False, "control": "select", "options": ["0", "1"]},
    {"key": "ECHOTRACE_LLM_THINKING_MAX_TOKENS", "label": "Thinking 模式输出预算", "category": "Run limits", "description": "开启 Thinking 时的最大输出 token 数；需同时覆盖推理 token 与最终输出，否则交付会被截断。", "secret": False},
    {"key": "ECHOTRACE_AGENT_MAX_STEPS", "label": "Maximum Agent steps", "category": "Run limits", "description": "Tool/decision turns allowed for one Agent.", "secret": False},
    {"key": "ECHOTRACE_RESEARCH_AGENT_MAX_STEPS", "label": "Research Agent steps", "category": "Run limits", "description": "Bounded model turns for each source-collection task.", "secret": False},
    {"key": "ECHOTRACE_AGENT_MAX_MODEL_ERRORS", "label": "Agent model retries", "category": "Run limits", "description": "Bounded Agent re-decisions after a model response error.", "secret": False},
    {"key": "ECHOTRACE_AGENT_CONTRACT_RETRIES", "label": "Agent contract retries", "category": "Run limits", "description": "Bounded retries when a role deliverable does not satisfy its JSON contract.", "secret": False},
    {"key": "ECHOTRACE_AGENT_MAX_PARALLEL_TOOL_CALLS", "label": "Maximum parallel Tool calls", "category": "Run limits", "description": "Bounded number of independent native read-only Tool calls in one model turn.", "secret": False},
    {"key": "ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS", "label": "Agent task deadline (seconds)", "category": "Run limits", "description": "Maximum wall-clock time for one Agent task, including bounded retries.", "secret": False},
    {"key": "ECHOTRACE_NODE_TASK_DEADLINE_SECONDS", "label": "Node task deadline (seconds)", "category": "Run limits", "description": "Deadline shared by role work and broker redeliveries for one node revision; explicit recovery starts a fresh deadline.", "secret": False},
)

ALLOWED_KEYS = {item["key"] for item in SETTING_DEFINITIONS}
SECRET_KEYS = {item["key"] for item in SETTING_DEFINITIONS if item["secret"]}
DEFAULT_VALUES = {"ECHOTRACE_LLM_TIMEOUT_SECONDS": "1200", "ECHOTRACE_LLM_MAX_RETRIES": "2", "ECHOTRACE_LLM_MAX_TOKENS": "32768", "ECHOTRACE_LLM_CONTEXT_WINDOW": "1000000", "ECHOTRACE_LLM_TOKENS_PER_MINUTE": "1000000", "ECHOTRACE_CONTEXT_TARGET_RATIO": "0.9", "ECHOTRACE_CONTEXT_SOFT_RATIO": "0.85", "ECHOTRACE_CONTEXT_HARD_RATIO": "0.95", "ECHOTRACE_LLM_THINKING": "0", "ECHOTRACE_LLM_THINKING_MAX_TOKENS": "65536", "ECHOTRACE_AGENT_MAX_STEPS": "64", "ECHOTRACE_RESEARCH_AGENT_MAX_STEPS": "96", "ECHOTRACE_AGENT_MAX_MODEL_ERRORS": "2", "ECHOTRACE_AGENT_CONTRACT_RETRIES": "2", "ECHOTRACE_AGENT_MAX_PARALLEL_TOOL_CALLS": "4", "ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS": "2400", "ECHOTRACE_NODE_TASK_DEADLINE_SECONDS": "3600"}

_SETTING_ZH: dict[str, tuple[str, str, str]] = {
    "ECHOTRACE_LLM_BASE_URL": ("模型服务地址", "智能体", "兼容 OpenAI 接口的模型服务端点。"),
    "ECHOTRACE_LLM_MODEL": ("模型名称", "智能体", "用于调查与报告生成的模型。"),
    "ECHOTRACE_LLM_API_KEY": ("模型 API 密钥", "智能体", "模型服务的认证密钥；页面仅显示是否已配置。"),
    "TAVILY_API_KEY": ("Tavily 搜索密钥", "搜索", "Tavily 网页搜索服务的访问凭据。"),
    "TAVILY_BASE_URL": ("Tavily 服务地址", "搜索", "Tavily 搜索服务端点。"),
    "BOCHA_WEB_SEARCH_API_KEY": ("博查搜索密钥", "搜索", "博查中文网页搜索服务的访问凭据。"),
    "BOCHA_BASE_URL": ("博查服务地址", "搜索", "博查搜索服务端点。"),
    "ECHOTRACE_PUBLIC_FETCH_ENABLED": ("读取公开网页正文", "搜索", "允许抓取已知公开网址的网页正文。"),
    "ECHOTRACE_PERSISTENCE": ("调查数据存储方式", "存储", "调查数据目前使用 MySQL 持久化。"),
    "ECHOTRACE_DATABASE_URL": ("MySQL 连接地址", "存储", "MySQL 数据库连接配置。"),
    "ECHOTRACE_REDIS_HOST": ("Redis 主机", "存储", "用于任务协调的 Redis 主机地址。"),
    "ECHOTRACE_REDIS_PORT": ("Redis 端口", "存储", "Redis 服务端口号。"),
    "ECHOTRACE_REDIS_PASSWORD": ("Redis 密码", "存储", "Redis 身份验证密码。"),
    "ECHOTRACE_REDIS_DB": ("Redis 数据库编号", "存储", "Redis 逻辑数据库编号。"),
    "ECHOTRACE_REDIS_TIMEOUT": ("Redis 超时时间（秒）", "存储", "Redis 连接与操作的超时时间。"),
    "ECHOTRACE_REDIS_URL": ("Redis 完整连接地址", "存储", "适配器使用的完整 Redis 连接地址。"),
    "ECHOTRACE_REDIS_TLS": ("Redis 加密连接", "存储", "连接地址未指定时，是否使用 TLS 加密 Redis 连接。"),
    "S3_ENDPOINT_URL": ("对象存储服务地址", "存储", "S3 兼容对象存储端点，通常为 Compose 中的 RustFS 服务。"),
    "S3_BUCKET": ("对象存储桶名称", "存储", "用于保存不可变来源材料与媒体文件的存储桶。"),
    "S3_ACCESS_KEY_ID": ("对象存储访问密钥", "存储", "S3 兼容对象存储的访问密钥。"),
    "S3_SECRET_ACCESS_KEY": ("对象存储私密密钥", "存储", "S3 兼容对象存储的私密密钥。"),
    "S3_REGION": ("对象存储区域", "存储", "S3 请求签名使用的区域名称。"),
    "S3_USE_TLS": ("对象存储加密连接", "存储", "是否通过 HTTPS 连接 S3 兼容对象存储。"),
    "S3_MAX_OBJECT_BYTES": ("对象大小上限（字节）", "存储", "原始材料或媒体对象允许的最大字节数。"),
    "ECHOTRACE_LLM_TIMEOUT_SECONDS": ("模型请求超时（秒）", "运行限制", "单次模型请求允许等待的最长时间。"),
    "ECHOTRACE_LLM_MAX_RETRIES": ("模型请求重试次数", "运行限制", "单次模型请求发生传输错误时的最大重试次数。"),
    "ECHOTRACE_LLM_MAX_TOKENS": ("最大输出 Token 数", "运行限制", "调查与报告生成单轮模型回复的输出预算。"),
    "ECHOTRACE_LLM_TOKENS_PER_MINUTE": ("每分钟 Token 额度", "运行限制", "Redis 每分钟允许使用的 Token 额度；默认参考模型上下文窗口。"),
    "ECHOTRACE_LLM_CONTEXT_WINDOW": ("模型上下文窗口", "运行限制", "用于估算请求长度的模型上下文窗口大小。"),
    "ECHOTRACE_CONTEXT_TARGET_RATIO": ("上下文目标比例", "运行限制", "相对于模型上下文上限的输入目标比例。"),
    "ECHOTRACE_CONTEXT_SOFT_RATIO": ("上下文软压缩比例", "运行限制", "达到该输入比例后开始按智能体角色压缩上下文。"),
    "ECHOTRACE_CONTEXT_HARD_RATIO": ("上下文强制压缩比例", "运行限制", "达到该输入比例后执行最后一级压缩，以避免超过模型限制。"),
    "ECHOTRACE_CONTEXT_BUDGET_ORCHESTRATOR": ("编排智能体上下文预算", "运行限制", "可选；编排智能体的预估输入 Token 目标。"),
    "ECHOTRACE_CONTEXT_BUDGET_RESEARCHER": ("研究智能体上下文预算", "运行限制", "可选；研究智能体的预估输入 Token 目标。"),
    "ECHOTRACE_CONTEXT_BUDGET_VERIFIER": ("核验智能体上下文预算", "运行限制", "可选；核验智能体的预估输入 Token 目标。"),
    "ECHOTRACE_CONTEXT_BUDGET_REPORT_COMPOSER": ("报告智能体上下文预算", "运行限制", "可选；报告智能体的预估输入 Token 目标。"),
    "ECHOTRACE_LLM_THINKING": ("模型推理模式", "智能体", "开启后模型会先进行推理再作答，并使用更大的输出预算；默认关闭。"),
    "ECHOTRACE_LLM_THINKING_MAX_TOKENS": ("推理模式输出预算", "运行限制", "开启推理模式时的最大输出 Token 数；需同时容纳推理内容与最终回复，否则结果可能被截断。"),
    "ECHOTRACE_AGENT_MAX_STEPS": ("智能体最大步骤数", "运行限制", "单个智能体允许执行的工具调用与决策轮数。"),
    "ECHOTRACE_RESEARCH_AGENT_MAX_STEPS": ("研究任务最大步骤数", "运行限制", "每个来源采集任务允许进行的模型轮数。"),
    "ECHOTRACE_AGENT_MAX_MODEL_ERRORS": ("智能体模型错误重试次数", "运行限制", "模型回复出错后，智能体重新决策的最大次数。"),
    "ECHOTRACE_AGENT_CONTRACT_RETRIES": ("智能体交付校验重试次数", "运行限制", "智能体交付内容不符合 JSON 结构约定时的最大重试次数。"),
    "ECHOTRACE_AGENT_MAX_PARALLEL_TOOL_CALLS": ("工具最大并行调用数", "运行限制", "单轮模型调用中允许并行执行的只读工具数量。"),
    "ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS": ("智能体任务时限（秒）", "运行限制", "单个智能体任务（含有限重试）的最长运行时间。"),
    "ECHOTRACE_NODE_TASK_DEADLINE_SECONDS": ("节点任务时限（秒）", "运行限制", "单轮节点研究及其消息重投共享的时限；显式恢复失败任务时会重新计时。"),
}


def settings_path() -> Path:
    return Path(os.getenv("ECHOTRACE_SETTINGS_FILE", str(_PROJECT_ROOT / ".env"))).expanduser()


def _read_file(path: Path | None = None) -> dict[str, str]:
    path = path or settings_path()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in ALLOWED_KEYS:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _effective_values() -> dict[str, str]:
    file_values = _read_file()
    return {item["key"]: os.getenv(item["key"], file_values.get(item["key"], DEFAULT_VALUES.get(item["key"], ""))) for item in SETTING_DEFINITIONS}


def snapshot() -> list[dict[str, Any]]:
    values = _effective_values()
    return [
        {
            **item,
            "label": _SETTING_ZH.get(item["key"], (item["label"], item["category"], item["description"]))[0],
            "category": _SETTING_ZH.get(item["key"], (item["label"], item["category"], item["description"]))[1],
            "description": _SETTING_ZH.get(item["key"], (item["label"], item["category"], item["description"]))[2],
            "configured": bool(values[item["key"]]),
            "value": MASK if item["secret"] and values[item["key"]] else ("" if item["secret"] else values[item["key"]]),
        }
        for item in SETTING_DEFINITIONS
    ]


def save(values: dict[str, Any]) -> list[dict[str, Any]]:
    unknown = sorted(set(values) - ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unsupported settings: {', '.join(unknown)}")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_file(path)
    original_lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for key, raw in values.items():
        value = "" if raw is None else str(raw).strip()
        if key in SECRET_KEYS and (not value or value == MASK):
            continue
        current[key] = value
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    serialized: dict[str, str] = {}
    for item in SETTING_DEFINITIONS:
        key = item["key"]
        if current.get(key, ""):
            value = current[key].replace("\\", "\\\\").replace("\n", "\\n")
            if any(char in value for char in " #\t"):
                value = '"' + value.replace('"', '\\"') + '"'
            serialized[key] = f"{key}={value}"
    lines: list[str] = []
    seen: set[str] = set()
    for line in original_lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in ALLOWED_KEYS:
            if key in serialized:
                lines.append(serialized[key])
            seen.add(key)
        else:
            lines.append(line)
    for item in SETTING_DEFINITIONS:
        key = item["key"]
        if key in serialized and key not in seen:
            lines.append(serialized[key])
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write("\n".join(lines) + "\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)
    return snapshot()


__all__ = ["MASK", "SETTING_DEFINITIONS", "snapshot", "save", "settings_path"]
