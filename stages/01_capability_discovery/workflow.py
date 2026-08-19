#!/usr/bin/env python3
"""Deterministic controller for AXIS stage-1 capability discovery.

The controller owns scope, batching, Codex invocation, structured-output
validation, evidence validation, semantic filtering, resume state, and the
completion gate. Codex investigates assigned items but cannot decide which
items exist or whether the stage is complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import copy
import resource
from concurrent.futures import ThreadPoolExecutor, as_completed


STAGE_DIR = Path(__file__).resolve().parent
AXIS_ROOT = STAGE_DIR.parents[1]
sys.path.insert(0, str(AXIS_ROOT))
from axis import system_config  # noqa: E402

CONFIG_PATH = STAGE_DIR / "config.json"
DISCOVERY_SCHEMA = STAGE_DIR / "schemas" / "discovery_result.schema.json"
REVIEW_SCHEMA = STAGE_DIR / "schemas" / "review_result.schema.json"
DISCOVERY_PROMPT = STAGE_DIR / "prompts" / "discover_batch.md"
REVIEW_PROMPT = STAGE_DIR / "prompts" / "review_batch.md"
REVIEW_ASSIGNMENT = STAGE_DIR / "prompts" / "review_assignment.md"
GOOD_PRACTICE = (
    AXIS_ROOT / "experiments" / "recursive_discovery" / "REPORT.md")
RUNS_DIR = STAGE_DIR / "runs"
TERMINAL_DISCOVERED = {
    "executable", "equivalent", "out_of_scope", "unsupported",
}
ITEM_ATTEMPT_LIMIT = 2
TERMINAL_ITEM_STATES = {"accepted", "filtered", "unfinished"}


class WorkflowError(RuntimeError):
    pass


def load_config():
    """读取全局规则，并从 apps/ 自动登记已经完成普查和账本的新软件。

    历史别名来自配置文件；工作流本身不保存软件名称映射。
    新软件不应为了进入第一阶段而修改控制器或这份旧名单；它自己的 runtime.json
    是实现模型已经验证过的声明，目录结构则给出 census/ledger 的标准位置。
    """
    config = copy.deepcopy(load_json(CONFIG_PATH))
    apps_root = AXIS_ROOT / "apps"
    for app_dir in sorted(apps_root.iterdir() if apps_root.is_dir() else []):
        if not app_dir.is_dir() or app_dir.name in config["apps"]:
            continue
        runtime_path = app_dir / "runtime.json"
        census_path = app_dir / "census" / "raw_ops.json"
        ledger_path = app_dir / "capabilities" / "ledger.json"
        if not (runtime_path.is_file() and census_path.is_file()
                and ledger_path.is_file()):
            continue
        runtime_decl = load_json(runtime_path)
        config["apps"][app_dir.name] = {
            "app_dir": app_dir.name,
            "runtime_family": runtime_decl.get(
                "runtime_family", "model_selected_programmatic_entrypoints"),
            "existing_census": str(census_path.relative_to(AXIS_ROOT)),
            "existing_ledger": str(ledger_path.relative_to(AXIS_ROOT)),
        }
    return config


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def write_jsonl(path: Path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_jsonl(path: Path):
    values = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                values.append(json.loads(line))
    return values


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_apps(config, raw):
    if raw == "all":
        return list(config["apps"])
    requested = [value.strip() for value in raw.split(",") if value.strip()]
    unknown = sorted(set(requested) - set(config["apps"]))
    if unknown:
        raise WorkflowError(f"未知软件：{', '.join(unknown)}")
    return requested


def stable_item_id(app, index, entry):
    record_identity = entry.get("record_sha256")
    if not record_identity:
        record_identity = json.dumps(
            entry.get("record"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
    key = "\0".join((
        app, str(entry.get("channel", "")), str(entry.get("symbol", "")),
        record_identity))
    short = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{app}:{index:05d}:{short}"


def existing_commands_are_materialized(entry, app_root):
    commands = entry.get("commands") or []
    if not commands:
        return False
    return all(
        command.get("command")
        and (app_root / "commands" / command["command"] / "spec.json").is_file()
        and (app_root / "commands" / command["command"] / "impl.py").is_file()
        for command in commands
    )


def classify_entry(entry, config, app_root=None):
    status = str(entry.get("status", ""))
    reason = str(entry.get("reason", ""))
    if status in set(config["revisit_statuses"]):
        return "queue", f"status={status}"
    if status in {"generated", "generated_and_verified"}:
        if app_root is not None and not existing_commands_are_materialized(
                entry, app_root):
            return "queue", "账本声称已经生成，但命令文件或命令记录不完整"
        return "reuse", f"复用已有终态 {status}"
    if status == "merged_equivalent":
        if not reason.strip():
            return "queue", "账本声称已由等价能力覆盖，但没有记录理由"
        return "reuse", "复用已有等价合并结论"
    if status == "excluded_with_reason":
        lowered = reason.lower()
        phrases = [value.lower()
                   for value in config["suspicious_exclusion_phrases"]]
        if any(phrase in lowered for phrase in phrases):
            return "queue", "排除理由来自失败、超时或上下文不足，需要重新发现"
        return "reuse", "复用已有范围排除及其理由"
    # Unknown states are never silently accepted.
    return "queue", f"未知或非终态 status={status!r}"


def make_items(app, ledger, config, app_root=None):
    items = []
    for index, entry in enumerate(ledger.get("entries", []), 1):
        action, why = classify_entry(entry, config, app_root)
        items.append({
            "item_id": stable_item_id(app, index, entry),
            "app": app,
            "ledger_index": index,
            "channel": entry.get("channel", ""),
            "symbol": entry.get("symbol", ""),
            "original_status": entry.get("status", ""),
            "original_reason": entry.get("reason", ""),
            "record": entry.get("record"),
            "source_channels": entry.get(
                "channels", [entry.get("channel", "")]),
            "source_locations": entry.get("locations", []),
            "occurrences": entry.get("occurrences", 1),
            "existing_commands": entry.get("commands") or [],
            "failed_commands": entry.get("failed_commands") or [],
            "stage1_action": action,
            "stage1_reason": why,
        })
    return items


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def new_run_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prepare_run(args, global_config=None):
    config = load_config()
    global_config = global_config or system_config.load(args.config)
    discovery_config = global_config["workflow"]["capability_discovery"]
    apps = selected_apps(config, args.apps)
    run_id = args.run_id or new_run_id()
    run_dir = RUNS_DIR / run_id
    if run_dir.exists():
        raise WorkflowError(
            f"运行目录已存在：{run_dir}；使用 --run-dir 恢复，不要覆盖")
    run_dir.mkdir(parents=True)
    inputs = {
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "axis_root": str(AXIS_ROOT),
        "apps": apps,
        "batch_size": args.batch_size or discovery_config["batch_size"],
        "global_config": global_config["_path"],
        "global_config_sha256": sha256(Path(global_config["_path"])),
        "config_sha256": sha256(CONFIG_PATH),
        "controller_files": {
            str(path.relative_to(STAGE_DIR)): sha256(path)
            for path in (
                Path(__file__).resolve(),
                CONFIG_PATH,
                STAGE_DIR / "AGENTS.md",
                REVIEW_PROMPT,
                REVIEW_SCHEMA,
            )
        },
        "files": {},
    }
    batches = []
    counts = {}
    for app in apps:
        app_config = config["apps"][app]
        census = AXIS_ROOT / app_config["existing_census"]
        ledger_path = AXIS_ROOT / app_config["existing_ledger"]
        for path in (census, ledger_path):
            if not path.is_file():
                raise WorkflowError(f"缺少第一阶段输入：{path}")
        ledger = load_json(ledger_path)
        app_root = AXIS_ROOT / "apps" / app_config["app_dir"]
        items = make_items(app, ledger, config, app_root)
        queued = [item for item in items if item["stage1_action"] == "queue"]
        app_run = run_dir / app
        write_jsonl(app_run / "inventory.jsonl", items)
        write_jsonl(app_run / "queue.jsonl", queued)
        batch_size = int(inputs["batch_size"])
        for number, batch_items in enumerate(
                chunks(queued, batch_size), 1):
            batch_id = f"{app}-{number:05d}"
            batch_dir = app_run / "batches" / batch_id
            batch = {
                "schema_version": 1,
                "run_id": run_id,
                "batch_id": batch_id,
                "app": app,
                "runtime_family": app_config["runtime_family"],
                "items": batch_items,
            }
            atomic_json(batch_dir / "batch.json", batch)
            batches.append({
                "batch_id": batch_id, "app": app,
                "path": str(batch_dir.relative_to(run_dir)),
                "status": "pending", "attempts": 0,
                "items": {
                    item["item_id"]: {
                        "status": "pending", "attempts": 0,
                        "accepted_attempt": "", "error": "",
                    }
                    for item in batch_items
                },
            })
        counts[app] = {
            "ledger_entries": len(items),
            "queued": len(queued),
            "reused": len(items) - len(queued),
            "batches": sum(batch["app"] == app for batch in batches),
        }
        inputs["files"][app] = {
            "census": str(census),
            "census_sha256": sha256(census),
            "ledger": str(ledger_path),
            "ledger_sha256": sha256(ledger_path),
        }
    atomic_json(run_dir / "inputs.json", inputs)
    atomic_json(run_dir / "state.json", {
        "run_id": run_id, "status": "prepared", "counts": counts,
        "batches": batches, "updated_at": time.time(),
    })
    return run_dir


def load_run(run_dir):
    run_dir = Path(run_dir).resolve()
    if not (run_dir / "inputs.json").is_file():
        raise WorkflowError(f"不是有效的第一阶段运行目录：{run_dir}")
    return run_dir, load_json(run_dir / "inputs.json"), load_json(
        run_dir / "state.json")


def verify_frozen_inputs(inputs):
    problems = []
    global_path = inputs.get("global_config")
    global_digest = inputs.get("global_config_sha256")
    if global_path and global_digest:
        path = Path(global_path)
        if not path.is_file() or sha256(path) != global_digest:
            problems.append(str(path))
    if sha256(CONFIG_PATH) != inputs["config_sha256"]:
        problems.append(str(CONFIG_PATH))
    for relative, expected in inputs.get("controller_files", {}).items():
        path = STAGE_DIR / relative
        if not path.is_file() or sha256(path) != expected:
            problems.append(str(path))
    for app, files in inputs["files"].items():
        for label in ("census", "ledger"):
            path = Path(files[label])
            expected = files[f"{label}_sha256"]
            if not path.is_file() or sha256(path) != expected:
                problems.append(f"{app}:{path}")
    if problems:
        raise WorkflowError(
            "准备后输入发生变化，不能把两个版本混入同一次运行："
            + ", ".join(problems))


def adopt_current_controller(run_dir, inputs, reason):
    """Record an explicit controller migration without changing app inputs."""
    previous = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reason": reason,
        "config_sha256": inputs.get("config_sha256"),
        "controller_files": inputs.get("controller_files", {}),
    }
    inputs.setdefault("controller_migrations", []).append(previous)
    inputs["config_sha256"] = sha256(CONFIG_PATH)
    global_path = Path(inputs.get(
        "global_config", system_config.DEFAULT_CONFIG_PATH))
    inputs["global_config"] = str(global_path)
    inputs["global_config_sha256"] = sha256(global_path)
    inputs["controller_files"] = {
        str(path.relative_to(STAGE_DIR)): sha256(path)
        for path in (
            Path(__file__).resolve(), CONFIG_PATH,
            STAGE_DIR / "AGENTS.md", REVIEW_PROMPT, REVIEW_SCHEMA,
        )
    }
    atomic_json(run_dir / "inputs.json", inputs)


def render_prompt(path, values):
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftovers = [part.split("}}", 1)[0]
                 for part in text.split("{{")[1:] if "}}" in part]
    if leftovers:
        raise WorkflowError(f"Prompt 仍有未替换变量：{leftovers}")
    return text


def codex_command(work_dir, schema, output, model, sandbox):
    command = [
        shutil.which("codex") or "codex", "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", sandbox, "--json",
        "--output-schema", str(schema),
        "--output-last-message", str(output),
        "-C", str(work_dir), "-",
    ]
    if model:
        command[2:2] = ["--model", model]
    return command


def configured_codex_command(executable, work_dir, schema, output,
                             model, sandbox):
    command = codex_command(work_dir, schema, output, model, sandbox)
    command[0] = shutil.which(executable) or executable
    return command


def invoke_codex(prompt, work_dir, schema, output, events, stderr,
                 model, timeout, sandbox="workspace-write", executable="codex"):
    command = configured_codex_command(
        executable, work_dir, schema, output, model, sandbox)
    runtime_dir = work_dir / ".runtime"
    runtime_paths = {
        "XDG_CACHE_HOME": runtime_dir / "cache",
        "XDG_CONFIG_HOME": runtime_dir / "config",
        "XDG_RUNTIME_DIR": runtime_dir / "run",
        "TMPDIR": runtime_dir / "tmp",
    }
    for path in runtime_paths.values():
        path.mkdir(parents=True, exist_ok=True)
    runtime_paths["XDG_RUNTIME_DIR"].chmod(0o700)
    environment = dict(os.environ)
    environment.update({
        name: str(path) for name, path in runtime_paths.items()
    })
    with events.open("w", encoding="utf-8") as event_handle, \
            stderr.open("w", encoding="utf-8") as error_handle:
        try:
            result = subprocess.run(
                command, input=prompt, text=True, stdout=event_handle,
                stderr=error_handle, timeout=timeout, env=environment,
                preexec_fn=disable_core_dumps)
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                f"Codex 子任务超时（{timeout}s）：{work_dir}") from exc
    if result.returncode != 0:
        tail = stderr.read_text(encoding="utf-8", errors="replace")[-1200:]
        raise WorkflowError(
            f"Codex 子任务失败（exit={result.returncode}）：{tail}")
    if not output.is_file():
        raise WorkflowError(f"Codex 没有生成结构化结果：{output}")


def run_assigned_review(batch, result_path, attempt_dir, model_override,
                        global_config, batch_path=None):
    """Run one lightweight semantic-filter batch with the configured agent."""
    del result_path
    agent = global_config["coding_agent"]
    review_config = global_config["workflow"]["capability_review"]
    review_prompt = render_prompt(REVIEW_PROMPT, {
        "BATCH_PATH": batch_path or attempt_dir / "batch.json",
    })
    review_prompt += REVIEW_ASSIGNMENT.read_text(encoding="utf-8")
    review_path = attempt_dir / "review_result.json"
    invoke_codex(
        review_prompt, attempt_dir, REVIEW_SCHEMA, review_path,
        attempt_dir / "review_events.jsonl",
        attempt_dir / "review_stderr.log",
        model_override or agent.get("model"),
        review_config["timeout_seconds"],
        sandbox=review_config["sandbox"], executable=agent["command"])
    review = load_json(review_path)
    validate_result_envelope(batch, review, "复核结果")
    return review


def disable_core_dumps():
    """调查动作崩溃时保留日志，但不写数十 MB 的进程内存转储。"""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def exact_ids(batch, report):
    expected = [item["item_id"] for item in batch["items"]]
    actual = [item.get("item_id") for item in report.get("items", [])]
    if len(actual) != len(set(actual)):
        raise WorkflowError("Codex 结果包含重复 item_id")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise WorkflowError(
            f"Codex 结果未覆盖精确任务集；missing={missing}, extra={extra}")


def safe_evidence_path(attempt_dir, raw):
    candidate = (attempt_dir / raw).resolve()
    try:
        candidate.relative_to(attempt_dir.resolve())
    except ValueError as exc:
        raise WorkflowError(f"证据路径越出批次目录：{raw}") from exc
    if not candidate.is_file():
        raise WorkflowError(f"证据文件不存在：{raw}")
    return candidate


def validate_discovery(batch, report, attempt_dir, max_attempts):
    validate_result_envelope(batch, report, "调查结果")
    problems = discovery_item_errors(
        batch, report, attempt_dir, max_attempts)
    if problems:
        raise WorkflowError("；".join(
            f"{item_id}：{reason}" for item_id, reason in problems.items()
        )[:8000])


def validate_result_envelope(batch, report, label):
    """Validate only batch-wide identity; item completeness is item-scoped."""
    if report.get("batch_id") != batch["batch_id"]:
        raise WorkflowError(f"{label} batch_id 不匹配")
    if report.get("app") != batch["app"]:
        raise WorkflowError(f"{label} app 不匹配")
    if not isinstance(report.get("items"), list):
        raise WorkflowError(f"{label} items 不是列表")


def discovery_item_errors(batch, report, attempt_dir, max_attempts):
    """Return independently attributable discovery errors by assigned item."""
    expected = {item["item_id"] for item in batch["items"]}
    grouped = {}
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        grouped.setdefault(item.get("item_id"), []).append(item)
    problems = {}
    extras = sorted(
        str(item_id) for item_id in grouped if item_id not in expected)
    if extras:
        raise WorkflowError(f"调查结果包含批次外 item_id：{extras}")
    for item_id in expected:
        matches = grouped.get(item_id, [])
        if not matches:
            problems[item_id] = "调查结果缺少此项目"
            continue
        if len(matches) > 1:
            problems[item_id] = "调查结果重复提交此项目"
            continue
        item = matches[0]
        item_problems = []
        if not isinstance(item, dict):
            problems[item_id] = "调查项目不是对象"
            continue
        disposition = item.get("disposition")
        if not isinstance(disposition, str) or not disposition:
            problems[item_id] = "调查项目缺少 disposition"
            continue
        attempts = item.get("attempts", [])
        evidence = item.get("evidence", [])
        if not isinstance(attempts, list):
            problems[item_id] = "attempts 不是列表"
            continue
        if not isinstance(evidence, list):
            problems[item_id] = "evidence 不是列表"
            continue
        if len(attempts) > max_attempts:
            item_problems.append(
                f"尝试数 {len(attempts)} > {max_attempts}")
        if not attempts:
            item_problems.append("没有记录真实尝试")
        if disposition in TERMINAL_DISCOVERED and not evidence:
            item_problems.append("终态没有证据")
        for record in evidence:
            if not isinstance(record, dict) or not record.get("path"):
                item_problems.append("证据记录缺少路径")
                continue
            try:
                safe_evidence_path(attempt_dir, record["path"])
            except WorkflowError as exc:
                item_problems.append(str(exc))
        if disposition == "executable":
            if not any(
                    isinstance(value, dict) and value.get("exit_code") == 0
                    for value in attempts):
                item_problems.append("没有成功运行记录")
            if not item.get("command_candidates"):
                item_problems.append("没有完整命令候选清单")
            for parameter in item.get("parameters", []):
                if not isinstance(parameter, dict):
                    item_problems.append("参数记录不是对象")
                    continue
                if not parameter.get("value_kind"):
                    item_problems.append(
                        f"参数 {parameter.get('name')} 没有机器可读 value_kind")
                if (parameter.get("value_kind") == "object_reference"
                        and not parameter.get("target_type")):
                    item_problems.append(
                        f"对象引用参数 {parameter.get('name')} 没有 target_type")
            for candidate in item.get("command_candidates", []):
                if not isinstance(candidate, dict):
                    item_problems.append("命令候选不是对象")
                    continue
                if not candidate.get("implementation_route"):
                    item_problems.append(
                        f"候选 {candidate.get('name')} 没有 implementation_route")
                if not candidate.get("persistence_check"):
                    item_problems.append(
                        f"候选 {candidate.get('name')} 没有 persistence_check")
        if disposition == "equivalent" and not item.get(
                "equivalent_commands"):
            item_problems.append("没有等价命令清单")
        if disposition == "unsupported" and len(attempts) < 2:
            item_problems.append("只有一次尝试，不能证明运行时不支持")
        if item_problems:
            problems[item_id] = "；".join(item_problems)
    return problems


def validate_review(batch, review):
    validate_result_envelope(batch, review, "复核结果")
    exact_ids(batch, review)


def review_item_errors(batch, review):
    """Return missing or duplicate filter decisions without rejecting peers."""
    expected = {item["item_id"] for item in batch["items"]}
    grouped = {}
    for item in review.get("items", []):
        if not isinstance(item, dict):
            continue
        grouped.setdefault(item.get("item_id"), []).append(item)
    extras = sorted(
        str(item_id) for item_id in grouped if item_id not in expected)
    if extras:
        raise WorkflowError(f"复核结果包含批次外 item_id：{extras}")
    errors = {}
    verdicts = {}
    for item_id in expected:
        matches = grouped.get(item_id, [])
        if not matches:
            errors[item_id] = "复核结果缺少此项目"
        elif len(matches) > 1:
            errors[item_id] = "复核结果重复提交此项目"
        elif matches[0].get("decision") not in {"expose", "skip"}:
            errors[item_id] = "语义过滤结果缺少合法 decision"
        else:
            verdicts[item_id] = matches[0]
    return verdicts, errors


def batch_prompt_values(batch, attempt_dir, config):
    app_config = config["apps"][batch["app"]]
    return {
        "BATCH_PATH": attempt_dir / "batch.json",
        "AXIS_ROOT": AXIS_ROOT,
        "CENSUS_PATH": AXIS_ROOT / app_config["existing_census"],
        "LEDGER_PATH": AXIS_ROOT / app_config["existing_ledger"],
        "GOOD_PRACTICE_PATH": GOOD_PRACTICE,
        "RUNTIME_FAMILY": batch["runtime_family"],
        "MAX_ATTEMPTS": config["max_attempts_per_item"],
    }


def item_result(report, item_id):
    return next(
        (item for item in report.get("items", [])
         if item.get("item_id") == item_id), None)


def terminal_batch_status(item_states):
    statuses = {item["status"] for item in item_states.values()}
    if statuses <= {"accepted", "filtered"}:
        return "accepted"
    if statuses <= TERMINAL_ITEM_STATES:
        return "completed_with_unfinished"
    return "needs_retry"


def migrate_legacy_batch(run_dir, batch_state):
    """Turn old all-or-nothing batch history into locked per-item history.

    Every independently accepted item is preserved, even when another item in
    the same legacy batch made the old controller reject the batch. Items
    never accepted after at least two completed reviews become unfinished.
    Incomplete/mechanical attempts without a review are retained on disk but
    do not erase a previously accepted result.
    """
    if batch_state.get("items"):
        return False
    source_dir = run_dir / batch_state["path"]
    batch = load_json(source_dir / "batch.json")
    states = {
        item["item_id"]: {
            "status": "pending", "attempts": 0,
            "accepted_attempt": "", "error": "",
        }
        for item in batch["items"]
    }
    reviewed = {item_id: 0 for item_id in states}
    last_error = {item_id: "" for item_id in states}
    for attempt_dir in sorted(source_dir.glob("attempt-*")):
        result_path = attempt_dir / "discovery_result.json"
        review_path = attempt_dir / "review_result.json"
        if not (result_path.is_file() and review_path.is_file()):
            continue
        try:
            report = load_json(result_path)
            review = load_json(review_path)
        except (json.JSONDecodeError, OSError):
            continue
        report_by_id = {
            item.get("item_id"): item for item in report.get("items", [])}
        review_by_id = {
            item.get("item_id"): item for item in review.get("items", [])}
        for item_id, state in states.items():
            discovered = report_by_id.get(item_id)
            verdict = review_by_id.get(item_id)
            if discovered is None or verdict is None:
                continue
            reviewed[item_id] += 1
            if (state["status"] != "accepted"
                    and verdict.get("verdict") == "accept"
                    and discovered.get("disposition") != "blocked"):
                state.update({
                    "status": "accepted",
                    "attempts": min(reviewed[item_id], ITEM_ATTEMPT_LIMIT),
                    "accepted_attempt": str(
                        attempt_dir.relative_to(run_dir)),
                    "error": "",
                })
            elif verdict.get("verdict") != "accept":
                last_error[item_id] = verdict.get("reason", "复核未通过")
            elif discovered.get("disposition") == "blocked":
                last_error[item_id] = discovered.get(
                    "reason", "当前环境无法完成")
    for item_id, state in states.items():
        if state["status"] == "accepted":
            continue
        state["attempts"] = min(reviewed[item_id], ITEM_ATTEMPT_LIMIT)
        state["error"] = last_error[item_id] or batch_state.get(
            "error", "尚未取得独立复核结果")
        if state["attempts"] >= ITEM_ATTEMPT_LIMIT:
            state["status"] = "unfinished"
    batch_state["items"] = states
    batch_state["status"] = terminal_batch_status(states)
    batch_state["error"] = ""
    return True


def migrate_legacy_state(run_dir, state):
    changed = False
    for batch_state in state["batches"]:
        changed = migrate_legacy_batch(run_dir, batch_state) or changed
    return changed


def investigate_batch(run_dir, batch_state, args, config, global_config,
                      review_semaphore):
    """Apply one semantic expose/skip decision to every assigned record.

    This stage deliberately performs no runtime call, parameter analysis,
    evidence collection, or implementation planning. Those belong to the
    isolated command implementation stage.
    """
    source_dir = run_dir / batch_state["path"]
    full_batch = load_json(source_dir / "batch.json")
    item_states = batch_state["items"]
    active = [item for item in full_batch["items"]
              if item_states[item["item_id"]]["status"] == "pending"]
    attempt_number = batch_state["attempts"] + (1 if active else 0)
    if active:
        batch = {**full_batch, "items": active}
        attempt_dir = source_dir / f"attempt-{attempt_number:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(attempt_dir / "batch.json", batch)
        shutil.copy2(STAGE_DIR / "AGENTS.md", attempt_dir / "AGENTS.md")
        try:
            with review_semaphore:
                review = run_assigned_review(
                    batch, None, attempt_dir, args.model, global_config,
                    batch_path=attempt_dir / "batch.json")
            review_by_id, review_errors = review_item_errors(batch, review)
        except (WorkflowError, json.JSONDecodeError, KeyError) as exc:
            review_by_id = {}
            review_errors = {
                item["item_id"]: f"语义过滤任务失败：{exc}"
                for item in active
            }
            atomic_json(attempt_dir / "controller_error.json", {
                "error": str(exc), "attempt": attempt_number})
        for assigned in active:
            item_id = assigned["item_id"]
            state = item_states[item_id]
            state["attempts"] = 1
            if item_id in review_errors:
                state.update({
                    "status": "unfinished",
                    "error": review_errors[item_id],
                })
                continue
            verdict = review_by_id[item_id]
            attempt_path = str(attempt_dir.relative_to(run_dir))
            if verdict["decision"] == "expose":
                state.update({
                    "status": "accepted",
                    "accepted_attempt": attempt_path,
                    "review_reason": verdict["reason"],
                    "error": "",
                })
            else:
                state.update({
                    "status": "filtered",
                    "filtered_attempt": attempt_path,
                    "review_reason": verdict["reason"],
                    "error": "",
                })
    return {
        "status": terminal_batch_status(item_states),
        "attempts": attempt_number,
        "items": item_states,
        "error": "",
    }


def save_state(run_dir, state):
    state["updated_at"] = time.time()
    if state.get("status") in {
            "complete", "complete_with_unfinished", "gate_failed"}:
        atomic_json(run_dir / "state.json", state)
        return
    statuses = {batch["status"] for batch in state["batches"]}
    if statuses <= {"accepted", "completed_with_unfinished"}:
        state["status"] = "investigation_complete"
    elif "needs_retry" in statuses:
        state["status"] = "needs_retry"
    elif "running" in statuses:
        state["status"] = "running"
    atomic_json(run_dir / "state.json", state)


def run_pending(run_dir, args):
    run_dir, inputs, state = load_run(run_dir)
    migrated = migrate_legacy_state(run_dir, state)
    if migrated:
        adopt_current_controller(
            run_dir, inputs,
            "从整批验收迁移为逐项锁定；单项最多尝试两次")
        save_state(run_dir, state)
    verify_frozen_inputs(inputs)
    config = load_config()
    global_config = system_config.load(args.config or inputs.get("global_config"))
    # Ctrl-C、机器重启或外部终止可能把当前批次留在 running。恢复时不能覆盖
    # 已有 attempt 目录，也不能丢掉独立复核给出的修复意见。
    recovered = False
    for batch in state["batches"]:
        if batch.get("status") != "running":
            continue
        source_dir = run_dir / batch["path"]
        attempts = sorted(source_dir.glob("attempt-*"))
        if attempts:
            numbers = []
            latest_error = ""
            for attempt_dir in attempts:
                try:
                    numbers.append(int(attempt_dir.name.rsplit("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
                error_path = attempt_dir / "controller_error.json"
                if error_path.is_file():
                    latest_error = load_json(error_path).get("error", latest_error)
            batch["attempts"] = max(numbers or [batch.get("attempts", 0)])
            if latest_error:
                batch["error"] = latest_error
        batch["status"] = "needs_retry"
        recovered = True
    if recovered:
        save_state(run_dir, state)
    pending = [
        batch for batch in state["batches"]
        if batch["status"] not in {"accepted", "completed_with_unfinished"}
    ]
    if args.max_batches is not None:
        pending = pending[:args.max_batches]
    max_workers = min(
        global_config["workflow"]["capability_discovery"][
            "max_parallel_batches"],
        global_config["coding_agent"]["max_parallel_processes"],
        len(pending) or 1)
    review_config = global_config["workflow"]["capability_review"]
    review_semaphore = threading.Semaphore(
        min(review_config["max_parallel_batches"],
            global_config["coding_agent"]["max_parallel_processes"]))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending_iterator = iter(pending)
        futures = {}

        def submit_next():
            try:
                batch = next(pending_iterator)
            except StopIteration:
                return False
            batch["status"] = "running"
            future = executor.submit(
                investigate_batch, run_dir, batch, args, config,
                global_config, review_semaphore)
            futures[future] = batch
            return True

        for _ in range(max_workers):
            submit_next()
        save_state(run_dir, state)
        while futures:
            future = next(as_completed(tuple(futures)))
            batch = futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                batch["status"] = "needs_retry"
                batch["error"] = str(exc)
            else:
                batch.update(result)
            submit_next()
            # Only the controller thread writes the shared state file.
            verify_frozen_inputs(inputs)
            save_state(run_dir, state)
            print(
                f"[stage1] {batch['batch_id']}: {batch['status']} "
                f"(attempts={batch['attempts']})", flush=True)
    return run_dir


def accepted_results(run_dir, state):
    """Collect completed semantic expose/skip decisions."""
    results = {}
    for batch in state["batches"]:
        if batch.get("items"):
            for item_id, item_state in batch["items"].items():
                if item_state["status"] not in {"accepted", "filtered"}:
                    continue
                attempt_key = (
                    "accepted_attempt" if item_state["status"] == "accepted"
                    else "filtered_attempt")
                attempt = run_dir / item_state[attempt_key]
                report = load_json(attempt / "review_result.json")
                item = item_result(report, item_id)
                if item is None:
                    raise WorkflowError(f"语义过滤结果缺少项目：{item_id}")
                results[item_id] = {
                    **item,
                    "batch_id": batch["batch_id"],
                    "result_root": str(attempt.relative_to(run_dir)),
                }
            continue
    return results


def verify_run(run_dir):
    run_dir, inputs, state = load_run(run_dir)
    verify_frozen_inputs(inputs)
    accepted = accepted_results(run_dir, state)
    item_states = {
        item_id: item_state
        for batch in state["batches"]
        for item_id, item_state in batch.get("items", {}).items()
    }
    app_reports = {}
    all_entries = []
    gate_errors = []
    unfinished_commands = []
    for app in inputs["apps"]:
        inventory = read_jsonl(run_dir / app / "inventory.jsonl")
        summary = {
            "total": len(inventory), "reused": 0, "expose": 0,
            "skip": 0, "unfinished": 0, "missing": 0,
        }
        for item in inventory:
            if item["stage1_action"] == "reuse":
                record = {
                    **item, "stage1_status": "reused",
                    "stage1_result": None,
                }
                summary["reused"] += 1
            else:
                result = accepted.get(item["item_id"])
                if result is None:
                    state_record = item_states.get(item["item_id"], {})
                    if state_record.get("status") == "unfinished":
                        record = {
                            **item, "stage1_status": "unfinished",
                            "stage1_result": None,
                            "unfinished": state_record,
                        }
                        summary["unfinished"] += 1
                        unfinished_commands.append({
                            **item,
                            "attempts": state_record.get("attempts", 0),
                            "reason": state_record.get("error", ""),
                        })
                    else:
                        record = {
                            **item, "stage1_status": "missing",
                            "stage1_result": None,
                        }
                        summary["missing"] += 1
                        gate_errors.append(
                            f"{item['item_id']} 尚未达到接受或未完成终态")
                else:
                    decision = result["decision"]
                    record = {
                        **item, "stage1_status": decision,
                        "stage1_result": result,
                    }
                    summary[decision] += 1
            all_entries.append(record)
        app_reports[app] = summary
    output = {
        "schema_version": 1,
        "run_id": inputs["run_id"],
        "source_inputs": inputs["files"],
        "apps": app_reports,
        "gate": {
            "passed": not gate_errors,
            "errors": gate_errors,
        },
        "entries": all_entries,
    }
    atomic_json(run_dir / "stage1_discovery.json", output)
    atomic_json(run_dir / "unfinished_commands.json", {
        "schema_version": 1,
        "run_id": inputs["run_id"],
        "count": len(unfinished_commands),
        "items": unfinished_commands,
    })
    lines = [
        "# AXIS 第一阶段能力发现报告", "",
        f"- 运行：`{inputs['run_id']}`",
        f"- 完成门：{'通过' if not gate_errors else '未通过'}", "",
        "| 软件 | 总项目 | 复用已有结果 | 批准公开 | 过滤跳过 | 未完成 | 缺失 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for app, summary in app_reports.items():
        lines.append(
            f"| {app} | {summary['total']} | {summary['reused']} | "
            f"{summary['expose']} | {summary['skip']} | "
            f"{summary['unfinished']} | "
            f"{summary['missing']} |")
    if unfinished_commands:
        lines.extend([
            "", "## 两次尝试后仍未完成的命令", "",
            "这些项目已经终结，不会阻塞其余命令，也不会自动重试。",
            "详细机器可读记录见 `unfinished_commands.json`。", "",
        ])
        lines.extend(
            f"- {item['item_id']} `{item['symbol']}`：{item['reason']}"
            for item in unfinished_commands[:200])
    if gate_errors:
        lines.extend([
            "",
            f"## 尚未完成（共 {len(gate_errors)} 项，下面只列前 200 项）",
            "",
        ])
        lines.extend(f"- {error}" for error in gate_errors[:200])
    (run_dir / "REPORT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    state["status"] = (
        "complete_with_unfinished" if unfinished_commands and not gate_errors
        else "complete" if not gate_errors else "gate_failed")
    save_state(run_dir, state)
    return output


def print_status(run_dir):
    run_dir, inputs, state = load_run(run_dir)
    counts = {}
    for batch in state["batches"]:
        counts[batch["status"]] = counts.get(batch["status"], 0) + 1
    item_counts = {}
    for batch in state["batches"]:
        for item in batch.get("items", {}).values():
            status = item["status"]
            item_counts[status] = item_counts.get(status, 0) + 1
    print(json.dumps({
        "run_id": inputs["run_id"], "status": state["status"],
        "batch_statuses": counts, "item_statuses": item_counts,
        "run_dir": str(run_dir),
    }, indent=2, ensure_ascii=False))


def parser():
    root = argparse.ArgumentParser(
        description="AXIS 第一阶段：确定性能力发现工作流")
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="冻结输入并生成完整任务队列")
    prepare.add_argument("--apps", default="all")
    prepare.add_argument("--run-id")
    prepare.add_argument("--batch-size", type=int)
    prepare.add_argument("--config")

    run = sub.add_parser("run", help="创建或恢复运行，并调用本地 Codex")
    run.add_argument("--apps", default="all")
    run.add_argument("--run-id")
    run.add_argument("--run-dir")
    run.add_argument("--batch-size", type=int)
    run.add_argument("--model")
    run.add_argument("--timeout", type=int)
    run.add_argument("--retries", type=int, default=1)
    run.add_argument("--max-batches", type=int)
    run.add_argument("--fail-fast", action="store_true")
    run.add_argument("--config")

    verify = sub.add_parser("verify", help="聚合结果并执行第一阶段完成门")
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--config")
    status = sub.add_parser("status", help="查看可恢复运行状态")
    status.add_argument("--run-dir", required=True)
    status.add_argument("--config")
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "prepare":
            run_dir = prepare_run(args)
            print(run_dir)
        elif args.command == "run":
            if args.run_dir:
                run_dir = Path(args.run_dir)
            else:
                run_dir = prepare_run(args)
            run_pending(run_dir, args)
            print_status(run_dir)
        elif args.command == "verify":
            output = verify_run(Path(args.run_dir))
            gate = output["gate"]
            print(json.dumps({
                "passed": gate["passed"],
                "error_count": len(gate["errors"]),
                "examples": gate["errors"][:20],
            }, indent=2, ensure_ascii=False))
            if not output["gate"]["passed"]:
                return 2
        elif args.command == "status":
            print_status(Path(args.run_dir))
    except WorkflowError as exc:
        print(f"[stage1] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
