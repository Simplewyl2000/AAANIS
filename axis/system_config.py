"""Load and validate the repository-wide AXIS orchestration configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path


AXIS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = AXIS_ROOT / "axis-config.json"
CONFIG_ENV = "AXIS_CONFIG"


class ConfigError(RuntimeError):
    pass


def _positive_int(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{path} 必须是大于或等于 1 的整数")


def _nonnegative_int(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{path} 必须是非负整数")


def validate(config):
    if config.get("schema_version") != 1:
        raise ConfigError("axis-config.json.schema_version 当前必须为 1")
    aliases = config.get("app_aliases", {})
    if not isinstance(aliases, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in aliases.items()):
        raise ConfigError("app_aliases 必须是非空字符串到非空字符串的映射")
    agent = config.get("coding_agent")
    workflow = config.get("workflow")
    if not isinstance(agent, dict) or not isinstance(workflow, dict):
        raise ConfigError("axis-config.json 必须包含 coding_agent 和 workflow")
    command = agent.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ConfigError("coding_agent.command 必须是非空字符串")
    model = agent.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError("coding_agent.model 必须是 null 或非空字符串")
    _positive_int(agent.get("max_parallel_processes"),
                  "coding_agent.max_parallel_processes")

    onboarding = workflow.get("onboarding")
    discovery = workflow.get("capability_discovery")
    review = workflow.get("capability_review")
    implementation = workflow.get("command_implementation")
    documentation = workflow.get("command_documentation")
    verification = workflow.get("verification")
    for name, row in (
            ("onboarding", onboarding),
            ("capability_discovery", discovery),
            ("capability_review", review),
            ("command_implementation", implementation),
            ("command_documentation", documentation),
            ("verification", verification)):
        if not isinstance(row, dict):
            raise ConfigError(f"workflow.{name} 必须是对象")

    for path, value in (
            ("workflow.onboarding.agent_timeout_seconds",
             onboarding.get("agent_timeout_seconds")),
            ("workflow.onboarding.script_timeout_seconds",
             onboarding.get("script_timeout_seconds")),
            ("workflow.onboarding.stage_attempt_limit",
             onboarding.get("stage_attempt_limit")),
            ("workflow.onboarding.redo_limit", onboarding.get("redo_limit")),
            ("workflow.capability_discovery.batch_size",
             discovery.get("batch_size")),
            ("workflow.capability_discovery.max_parallel_batches",
             discovery.get("max_parallel_batches")),
            ("workflow.capability_discovery.item_attempt_limit",
             discovery.get("item_attempt_limit")),
            ("workflow.capability_discovery.max_runtime_attempts_per_item",
             discovery.get("max_runtime_attempts_per_item")),
            ("workflow.capability_discovery.timeout_seconds",
             discovery.get("timeout_seconds")),
            ("workflow.capability_review.max_parallel_batches",
             review.get("max_parallel_batches")),
            ("workflow.capability_review.timeout_seconds",
             review.get("timeout_seconds")),
            ("workflow.command_implementation.batch_size",
             implementation.get("batch_size")),
            ("workflow.command_implementation.max_parallel_batches",
             implementation.get("max_parallel_batches")),
            ("workflow.command_implementation.timeout_seconds",
             implementation.get("timeout_seconds")),
            ("workflow.command_implementation.verify_timeout_seconds",
             implementation.get("verify_timeout_seconds")),
            ("workflow.command_documentation.timeout_seconds",
             documentation.get("timeout_seconds")),
            ("workflow.verification.jobs", verification.get("jobs"))):
        _positive_int(value, path)
    _nonnegative_int(implementation.get("max_retries"),
                     "workflow.command_implementation.max_retries")
    if not isinstance(documentation.get("enabled"), bool):
        raise ConfigError("workflow.command_documentation.enabled 必须是布尔值")
    pass_rate = onboarding.get("minimum_pass_rate")
    if (isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float))
            or not 0 < pass_rate <= 1):
        raise ConfigError(
            "workflow.onboarding.minimum_pass_rate 必须在 0 和 1 之间")
    allowed_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
    for name, value in (
            ("capability_discovery", discovery.get("sandbox")),
            ("capability_review", review.get("sandbox"))):
        if value not in allowed_sandboxes:
            raise ConfigError(
                f"workflow.{name}.sandbox 必须是 {sorted(allowed_sandboxes)} 之一")
    return config


def load(path=None):
    selected = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    if not selected.is_absolute():
        selected = (AXIS_ROOT / selected).resolve()
    try:
        with selected.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到 AXIS 配置文件：{selected}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"AXIS 配置文件不是合法 JSON：{selected}: {exc}") from exc
    validate(config)
    config["_path"] = str(selected)
    return config
