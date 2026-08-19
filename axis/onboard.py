"""一条命令：放进一个软件，产出一条能跑的 CLI。

    python3 axis/onboard.py --app krita --launch "krita"

这个模块不实现任何工序，它只是把已经跑通的那些脚本和提示词按顺序串起来，
并且在每一步之后卡一道机械验收。过去这些步骤要人一条条敲命令、人肉判断
每步过没过（见 REPRODUCE.md 的第 34 行：「大模型只出现在流水线之外」），
现在它们全在流水线之内。

职责划分只有两类，一目了然：

  归给模型的：写这个软件的适配代码（探测启动方式、写普查器、写引擎、写探针）。
              每一样都有一份固定的提示词规范，规范在 prompts/ 下，不随软件变。
  归给脚本的：跑、验、判、重试、发布。核心脚本不认识任何软件名。

模型每交一样东西，脚本立刻验一遍；不过就把验收输出原文贴回提示词让它重做，
到上限还不过就停下并说清卡在哪一道。模型说通过不算通过。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import runtime  # noqa: E402
import system_config  # noqa: E402

PROMPTS = os.path.join(ROOT, "prompts")
RETRY_PROMPT = os.path.join(PROMPTS, "retry_feedback.md")
DEFAULT_LLM_TIMEOUT = 7200
DEFAULT_SCRIPT_TIMEOUT = 10800
DEFAULT_MAX_ATTEMPTS = 3
ACTIVE_CONFIG = None

STAGE1 = os.path.join(ROOT, "stages", "01_capability_discovery", "workflow.py")
STAGE2_DIR = os.path.join(ROOT, "stages", "02_05_command_release")
# 第一阶段的运行结果落在两个地方：历史那批在实验目录下，新跑的在自己的
# runs 目录下。对账必须两边都看，否则新跑的调查会被当成没跑过。
STAGE1_RESULT_ROOTS = (
    os.path.join(ROOT, "stages", "01_capability_discovery", "runs"),
    os.path.join(ROOT, "experiments", "task_minimum_subset", "results"),
)

# 待办的定义：运行时登记了这条能力，但还没拿到可重复的效果证据。
# 其余状态都是有据可查的结论（只作证据、有理由排除、已合并、已生成）。
BACKLOG_STATUS = ("candidate", "not_generated_with_reason",
                  "partially_generated_with_reason")


class StageFailed(Exception):
    """某一道工序在允许的重试次数内没能通过验收。"""

    def __init__(self, stage, detail):
        super().__init__(f"[{stage}] {detail}")
        self.stage = stage
        self.detail = detail


# ── 基础设施 ────────────────────────────────────────────────────────────

def app_dir(app):
    return os.path.join(ROOT, "apps", app)


def work_dir(app):
    path = os.path.join(app_dir(app), "onboard")
    os.makedirs(path, exist_ok=True)
    return path


def state_path(app):
    return os.path.join(work_dir(app), "state.json")


def load_state(app):
    path = state_path(app)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {"app": app, "done": [], "history": []}


def new_workflow_run_id(app):
    """Return a unique run ID for an explicitly fresh onboarding run."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    suffix = time.time_ns() % 1_000_000_000
    return f"onboard-{app}-{stamp}-{suffix:09d}"


def start_fresh_state(app):
    """Archive the current progress record and create independent progress."""
    path = state_path(app)
    if os.path.isfile(path):
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        archive = os.path.join(
            work_dir(app), f"state.before-{stamp}-{time.time_ns()}.json")
        os.replace(path, archive)
        log(f"已归档旧进度记录：{archive}")
    state = {
        "app": app,
        "done": [],
        "history": [],
        "workflow_run_id": new_workflow_run_id(app),
    }
    save_state(app, state)
    return state


def save_state(app, state):
    tmp = state_path(app) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(app))


def log(message):
    print(f"[onboard] {message}", flush=True)


def run_script(command, env=None, timeout=None, cwd=ROOT):
    """跑一条确定性脚本，返回 (成功, 合并输出)。"""
    config = ACTIVE_CONFIG or system_config.load()
    timeout = timeout or config["workflow"]["onboarding"][
        "script_timeout_seconds"]
    log("跑 " + " ".join(str(part) for part in command))
    try:
        completed = subprocess.run(
            [str(part) for part in command], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时（{timeout} 秒）"
    except FileNotFoundError as error:
        return False, f"找不到可执行文件：{error}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output


def invoke_model(prompt, app, tag, config, timeout=None):
    """把一段提示词交给本地编码模型执行。

    调用方式沿用 stages/ 下已经在用的那套，不另造一套。
    """
    logs = os.path.join(work_dir(app), "logs")
    os.makedirs(logs, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    last_message = os.path.join(logs, f"{tag}-{stamp}.txt")
    events = os.path.join(logs, f"{tag}-{stamp}.jsonl")

    agent = config["coding_agent"]
    timeout = timeout or config["workflow"]["onboarding"][
        "agent_timeout_seconds"]
    command = [
        shutil.which(agent["command"]) or agent["command"], "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "danger-full-access", "--json",
        "--output-last-message", last_message,
        "-C", ROOT, "-",
    ]
    if agent.get("model"):
        command[2:2] = ["--model", agent["model"]]
    log(f"交给模型：{tag}（提示词 {len(prompt)} 字）")
    with open(events, "w", encoding="utf-8") as event_handle:
        try:
            completed = subprocess.run(
                command, input=prompt, text=True, stdout=event_handle,
                stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"模型执行超时（{timeout} 秒），日志 {events}"
    if completed.returncode != 0:
        return False, (f"模型执行失败，退出码 {completed.returncode}\n"
                       f"{(completed.stderr or '')[-2000:]}\n日志 {events}")
    summary = ""
    if os.path.isfile(last_message):
        with open(last_message, encoding="utf-8") as handle:
            summary = handle.read()
    return True, summary


def read_prompt(name, app, launch=None):
    """读一份提示词模板，把软件名和启动方式填进去。

    模板本身是软件无关的，这是"归因给 Prompt"的落点：换软件只换占位符，
    不改模板内容。
    """
    path = os.path.join(PROMPTS, name)
    if not os.path.isfile(path):
        raise StageFailed("prompt", f"缺提示词模板 {path}")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("{软件名}", app).replace("{app}", app)
    if launch:
        text = text.replace("{运行时启动方式}", launch)
    return text


# ── 各道工序的验收（全部是脚本判定）────────────────────────────────────

def check_runtime_declaration(app):
    try:
        command = runtime.build_command(app, os.path.join(
            app_dir(app), "census.py"))
    except runtime.RuntimeError_ as error:
        return False, str(error)
    binary = command[0]
    if not (os.path.isabs(binary) and os.path.isfile(binary)) \
            and not shutil.which(binary):
        return False, (f"runtime.json 声明的解释器 {binary!r} 在这台机器上"
                       "找不到。写进声明之前必须先实测它能跑起来。")
    declaration = runtime.load(app)
    kind = "通用 Collector 命令" if declaration.get("runner") else "旧版解释器声明"
    return True, f"{kind}可用：{' '.join(command)}"


def check_file_exists(app, relative, what):
    path = os.path.join(app_dir(app), relative)
    if not os.path.isfile(path):
        return False, f"缺 {path}（{what}）"
    return True, f"{relative} 已就位"


def check_app_adapter(app):
    """Require every application-owned input needed by later generic stages."""
    required = (
        ("engine.py", "引擎适配层"),
        ("axis-app.json", "应用契约"),
    )
    for relative, what in required:
        passed, detail = check_file_exists(app, relative, what)
        if not passed:
            return passed, detail
    try:
        from axis import app_contract
        app_contract.load(app)
    except Exception as error:
        return False, f"axis-app.json 未通过结构验收：{error}"
    probe = os.path.join(app_dir(app), "atlas_gen.py")
    atlas_ok, _ = check_atlas_not_empty(app)
    if not os.path.isfile(probe) and not atlas_ok:
        return False, ("既没有 atlas_gen.py，也没有既有命令档案；"
                       "应用适配必须提供一种能力候选来源")
    source = "atlas_gen.py" if os.path.isfile(probe) else "既有 atlas 命令档案"
    return True, f"engine.py、axis-app.json 和{source}已就位且契约有效"


def run_atlas_probe(app):
    """Run an app-owned probe, while preserving already-materialized adapters."""
    script = os.path.join(app_dir(app), "atlas_gen.py")
    if not os.path.isfile(script):
        passed, detail = check_atlas_not_empty(app)
        return (passed, "没有探针脚本；使用已验收的既有命令档案：" + detail
                if passed else detail)
    return run_script(
        runtime.build_command(app, script), env=runtime.build_env(app))


def check_conformance(app):
    ok, output = run_script(
        [sys.executable, os.path.join(HERE, "conformance.py"), "--app", app])
    return ok, output


def check_atlas_not_empty(app):
    directory = os.path.join(app_dir(app), "atlas")
    if not os.path.isdir(directory):
        return False, f"没有 {directory} 目录，探针一条命令档案都没产出"
    count = len([n for n in os.listdir(directory) if n.endswith(".json")])
    if count == 0:
        return False, "atlas 目录是空的，探针一条命令档案都没产出"
    return True, f"命令档案 {count} 份"


def check_commands_built(app):
    directory = os.path.join(app_dir(app), "commands")
    if not os.path.isdir(directory):
        return False, f"没有 {directory} 目录"
    count = len([n for n in os.listdir(directory)
                 if os.path.isfile(os.path.join(directory, n, "spec.json"))])
    if count == 0:
        return False, "commands 目录里没有任何带 spec.json 的命令"
    return True, f"命令 {count} 条"


def check_verify_report(app, min_pass_rate):
    path = os.path.join(app_dir(app), "build", "report.json")
    if not os.path.isfile(path):
        return False, f"没有验证报告 {path}"
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)

    # 报告的结构是顶层直接一层 {命令名: {"pass": bool, "gates": {...}}}。
    results = {name: item for name, item in report.items()
               if isinstance(item, dict) and "pass" in item}
    total = len(results)
    if total == 0:
        return False, f"验证报告 {path} 里没有任何命令记录"

    passed = sum(1 for item in results.values() if item.get("pass"))
    rate = passed / total
    detail = f"验证 {total} 条，通过 {passed} 条（{rate:.1%}）"
    if rate >= min_pass_rate:
        return True, detail

    # 按失败的门和报错原文聚类，把病根摆出来，而不是罗列一堆单条失败。
    clusters = {}
    for name, item in results.items():
        if item.get("pass"):
            continue
        for gate, info in (item.get("gates") or {}).items():
            if info.get("pass"):
                continue
            text = str(info.get("detail", ""))
            key = (gate, text[:110])
            clusters.setdefault(key, []).append(name)

    lines = []
    for (gate, text), names in sorted(clusters.items(),
                                      key=lambda kv: -len(kv[1]))[:12]:
        lines.append(f"  [{gate}] {len(names)} 条：{text}")
        lines.append(f"      例如 {', '.join(sorted(names)[:4])}")
    return True, (detail + f"，低于参考值 {min_pass_rate:.0%}；"
                  "未通过命令会记录并排除，流程继续发布通过项。\n"
                  "失败按检查项和报错原文聚类如下：\n"
                  + "\n".join(lines))


def stage_name(app):
    """Resolve historical workflow aliases from data, not core conditionals."""
    aliases = (ACTIVE_CONFIG or system_config.load()).get("app_aliases", {})
    reverse = {value: key for key, value in aliases.items()}
    return reverse.get(app, app)


def backlog_symbols(app):
    """待办：账本登记了、但还没做出可重复效果证据的能力。"""
    path = os.path.join(app_dir(app), "capabilities", "ledger.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        ledger = json.load(handle)
    return {entry["symbol"] for entry in ledger.get("entries", [])
            if entry.get("status") in BACKLOG_STATUS}


def investigated_symbols(app):
    """Return symbols that received a semantic expose/skip decision."""
    wanted = stage_name(app)
    done = set()
    app_runs = []
    for results_root in STAGE1_RESULT_ROOTS:
        if not os.path.isdir(results_root):
            continue
        for run in sorted(os.listdir(results_root)):
            candidate = os.path.join(results_root, run, wanted)
            if os.path.isdir(candidate):
                app_runs.append(candidate)
    for app_run in app_runs:
        for root, _, files in os.walk(app_run):
            if "batch.json" not in files or "review_result.json" not in files:
                continue
            try:
                with open(os.path.join(root, "batch.json"),
                          encoding="utf-8") as handle:
                    batch = json.load(handle)
                with open(os.path.join(root, "review_result.json"),
                          encoding="utf-8") as handle:
                    review = json.load(handle)
            except (OSError, ValueError):
                continue
            approved_ids = {
                item["item_id"] for item in review.get("items", [])
                if (item.get("decision") in {"expose", "skip"}
                    or item.get("verdict") == "accept")}
            for item in batch.get("items", []):
                if item.get("item_id") in approved_ids and item.get("symbol"):
                    done.add(item["symbol"])
    return done


def release_approved_symbols(app):
    """Return symbols whose semantic decision is expose."""
    wanted = stage_name(app)
    approved = set()
    app_runs = []
    for results_root in STAGE1_RESULT_ROOTS:
        if not os.path.isdir(results_root):
            continue
        for run in sorted(os.listdir(results_root)):
            candidate = os.path.join(results_root, run, wanted)
            if os.path.isdir(candidate):
                app_runs.append(candidate)
    for app_run in app_runs:
        for root, _, files in os.walk(app_run):
            if "batch.json" not in files or "review_result.json" not in files:
                continue
            try:
                with open(os.path.join(root, "batch.json"),
                          encoding="utf-8") as handle:
                    batch = json.load(handle)
                with open(os.path.join(root, "review_result.json"),
                          encoding="utf-8") as handle:
                    review = json.load(handle)
            except (OSError, ValueError):
                continue
            reviewed_by_id = {
                item.get("item_id"): item for item in review.get("items", [])}
            for assigned in batch.get("items", []):
                item_id = assigned.get("item_id")
                reviewed = reviewed_by_id.get(item_id, {})
                decision = (reviewed.get("decision")
                            or reviewed.get("release_decision"))
                if decision == "expose":
                    if assigned.get("symbol"):
                        approved.add(assigned["symbol"])
    return approved


def atlas_symbols(app):
    """已经落成命令档案的能力。"""
    directory = os.path.join(app_dir(app), "atlas")
    found = set()
    if not os.path.isdir(directory):
        return found
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                cell = json.load(handle)
        except (OSError, ValueError):
            continue
        symbol = (cell.get("census_ref") or {}).get("symbol")
        if symbol:
            found.add(symbol)
    return found


def check_backlog_listed(app):
    backlog = backlog_symbols(app)
    if backlog is None:
        return False, "还没有待办账本，先跑账本脚本从普查结果里列出待办"
    path = os.path.join(app_dir(app), "capabilities", "ledger.json")
    with open(path, encoding="utf-8") as handle:
        ledger = json.load(handle)
    total = sum(entry.get("status") in BACKLOG_STATUS
                for entry in ledger.get("entries", []))
    return True, f"候选 {total} 条已列出，下一步只做语义过滤"


def check_backlog_investigated(app):
    """Every candidate label must receive an expose/skip decision."""
    backlog = backlog_symbols(app)
    if backlog is None:
        return False, "还没有待办账本"
    remaining = backlog - investigated_symbols(app)
    if not remaining:
        return True, f"候选名称 {len(backlog)} 个全部有语义过滤结论"
    sample = ", ".join(sorted(remaining)[:6])
    return False, (f"待办 {len(backlog)} 条里还有 {len(remaining)} 条没有"
                   f"第一阶段结论。例如：{sample}")


def check_accepted_implemented(app):
    """Report implementation coverage without blocking partial release."""
    accepted = release_approved_symbols(app) & (backlog_symbols(app) or set())
    if not accepted:
        return True, "没有待实现的已调查能力"
    remaining = accepted - atlas_symbols(app)
    if not remaining:
        return True, f"已调查的 {len(accepted)} 条全部落成命令档案"
    sample = ", ".join(sorted(remaining)[:6])
    return True, (f"已调查 {len(accepted)} 条，其中 {len(remaining)} 条"
                  f"还没有命令档案，已记录并排除；继续发布其余命令。"
                  f"例如：{sample}")


def run_discovery(app, config, run_id):
    """Run batched semantic filtering without launching the target app."""
    run_dir = os.path.join(STAGE1_RESULT_ROOTS[0], run_id)
    command = [sys.executable, STAGE1, "run"]
    if os.path.isdir(run_dir):
        log(f"发现第一阶段已有运行，恢复：{run_dir}")
        command.extend(["--run-dir", run_dir])
    else:
        command.extend([
            "--apps", stage_name(app), "--run-id", run_id])
    command.extend(["--config", config["_path"]])
    return run_script(command)


def run_release(app, config, run_id):
    """把第一阶段认可的能力全部交给模型实现成命令。

    这里用全量模式，不喂挑选清单——清单是照着评测任务反推的，会让产出的
    命令跟着评测走。
    """
    ok, output = run_script(
        [sys.executable, os.path.join(STAGE2_DIR, "prepare.py"),
         "--all-accepted", "--apps", stage_name(app),
         "--run-id", run_id, "--config", config["_path"]],
        cwd=STAGE2_DIR)
    if not ok:
        return False, output
    ok2, output2 = run_script(
        [sys.executable, os.path.join(STAGE2_DIR, "run_app.py"),
         "--app", stage_name(app), "--run-id", run_id,
         "--config", config["_path"], "--skip-documentation"],
        cwd=STAGE2_DIR,
        timeout=config["workflow"]["command_implementation"].get(
            "total_timeout_seconds", 172800))
    return ok2, output + "\n" + output2


def check_dist(app):
    directory = os.path.join(ROOT, "dist", f"{app}-axis", "commands")
    if not os.path.isdir(directory):
        return False, f"没有 {directory}"
    count = len(os.listdir(directory))
    launcher = os.path.join(ROOT, "bin", f"{app}-axis")
    if not os.path.isfile(launcher):
        return False, f"缺启动器 {launcher}"
    return True, f"冻结 {count} 条命令，启动器 {launcher}"


# ── 工序表 ──────────────────────────────────────────────────────────────

def stages(app, launch, min_pass_rate, config, run_id=None):
    """整条流水线。每一项要么归模型、要么归脚本，验收一律归脚本。"""
    run_id = run_id or f"onboard-{app}"

    return [
        {
            "name": "1-探测程序化入口",
            "owner": "模型",
            "prompt": "probe_runtime.md",
            "check": lambda: check_runtime_declaration(app),
            "what": "找到至少一个真实可查询的程序化入口，并声明怎样运行实现模型编写的 Collector",
        },
        {
            "name": "2-写普查器",
            "owner": "模型",
            "prompt": "census_adapter.md",
            "check": lambda: check_file_exists(app, "census.py", "能力普查器"),
            "what": "枚举这个软件运行时自报的能力全集",
        },
        {
            "name": "3-跑普查",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "census.py"),
                 "--app", app]),
            "check": lambda: check_file_exists(
                app, os.path.join("census", "raw_ops.json"), "普查产物"),
        },
        {
            "name": "4-验收普查",
            "owner": "脚本",
            "check": lambda: check_conformance(app),
            "on_fail_redo": "2-写普查器",
            "legacy_ok": True,
        },
        {
            "name": "5-写引擎和探针",
            "owner": "模型",
            "prompt": "build_app.md",
            "check": lambda: check_app_adapter(app),
            "what": "应用自有的 engine.py、atlas_gen.py 和 axis-app.json",
        },
        {
            "name": "6-跑探针",
            "owner": "脚本",
            "run": lambda: run_atlas_probe(app),
            "check": lambda: check_atlas_not_empty(app),
            "on_fail_redo": "5-写引擎和探针",
        },
        {
            "name": "7-列出待办",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "ledger.py"),
                 "--app", app]),
            "check": lambda: check_backlog_listed(app),
            "what": "把普查账本里还没做成命令的能力挑出来，作为模型的工单",
        },
        {
            "name": "8-模型批量过滤待办",
            "owner": "模型",
            "run": lambda: run_discovery(app, config, run_id),
            "basis": "stages/01 的 prompts + 自带切批重试",
            "check": lambda: check_backlog_investigated(app),
            "legacy_ok": True,
            "what": "只按用户价值把候选分为 expose 或 skip，不运行软件",
        },
        {
            "name": "9-模型实现成命令",
            "owner": "模型",
            "run": lambda: run_release(app, config, run_id),
            "basis": "stages/02_05/implement_batch.md",
            "check": lambda: check_accepted_implemented(app),
            "legacy_ok": True,
            "what": "把调查认可的能力写成命令档案和引擎实现",
        },
        {
            "name": "10-渲染命令",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "build.py"),
                 "--app", app]),
            "check": lambda: check_commands_built(app),
        },
        {
            "name": "11-账实对齐",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "ledger.py"),
                 "--app", app]),
            "check": lambda: check_file_exists(
                app, os.path.join("capabilities", "ledger.json"), "能力账本"),
        },
        {
            "name": "12-过验证门",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "verify.py"),
                 "--app", app, "--jobs",
                 str(config["workflow"]["verification"]["jobs"])]),
            "check": lambda: check_verify_report(app, min_pass_rate),
            "on_fail_redo": "5-写引擎和探针",
        },
        {
            "name": "13-组织命令文档和 Skill",
            "owner": "模型",
            "run": lambda: run_script(
                [sys.executable, os.path.join(
                    STAGE2_DIR, "agent_guide.py"),
                 "--app", app, "--run-id", run_id,
                 "--config", config["_path"]],
                cwd=STAGE2_DIR),
            "basis": "stages/02_05/document_commands.md",
            "check": lambda: check_file_exists(
                app, os.path.join("guide", "commands.json"),
                "完整命令文档"),
        },
        {
            "name": "14-冻结成 CLI",
            "owner": "脚本",
            "run": lambda: run_script(
                [sys.executable, os.path.join(HERE, "freeze.py"),
                 "--app", app]),
            "check": lambda: check_dist(app),
        },
    ]


def stage1_discovery(steps):
    """Adapter onboarding, runtime census, probing, and deterministic ledger."""
    return steps[:7]


def stage2_filter(steps):
    """Semantic exposure filtering only."""
    return steps[7:8]


def stage3_implementation(steps):
    """Isolated implementation, rendering, ledger reconciliation, verification."""
    return steps[8:12]


def stage4_organization_and_release(steps):
    """Organized documentation, Agent skill, and deterministic release."""
    return steps[12:]


def workflow_stages(app, launch, min_pass_rate, config, run_id=None):
    """Expose the complete workflow as four auditable software-neutral stages."""
    steps = stages(app, launch, min_pass_rate, config, run_id=run_id)
    return [
        {"name": "Stage 1 · 能力发现", "steps": stage1_discovery(steps)},
        {"name": "Stage 2 · 语义过滤", "steps": stage2_filter(steps)},
        {"name": "Stage 3 · 命令实现", "steps": stage3_implementation(steps)},
        {"name": "Stage 4 · 组织与发布",
         "steps": stage4_organization_and_release(steps)},
    ]


# ── 编排 ────────────────────────────────────────────────────────────────

def run_stage(stage, app, launch, max_attempts, config,
              force=False, allow_legacy=False):
    """跑一道工序，失败就把验收输出贴回去重试。

    先验收再干活：产物已经就位就直接跳过。整条流水线因此是幂等的——
    重跑一遍不会重新调模型、不会覆盖已经过关的东西。这跟 REPRODUCE.md 里
    探针「已有格子的跳过」是同一个原则。
    """
    name = stage["name"]
    feedback = stage.pop("_feedback", "")

    if stage.get("legacy_ok") and allow_legacy:
        passed, detail = stage["check"]()
        if not passed:
            log(f"── {name} 不通过，但按 --allow-legacy 放行。"
                "这个软件的适配层是早期版本，缺口如下，不自动重写已发布的东西：")
            for line in detail.splitlines():
                if line.strip():
                    log("     " + line)
            return True, "按遗留格式放行（缺口已记录，见上）"
        return True, detail

    if not force and not feedback:
        passed, detail = stage["check"]()
        if passed:
            log(f"── {name} 产物已就位，跳过：{detail}")
            return True, detail

    for attempt in range(1, max_attempts + 1):
        log(f"── {name}（{stage['owner']}）第 {attempt}/{max_attempts} 次")

        # 有的模型工序不是直接喂提示词，而是交给一条自带切批和重试的链去驱动
        # （第一阶段的调查、第二阶段的实现都是这样）。有 run 就走 run。
        if stage.get("prompt"):
            prompt = read_prompt(stage["prompt"], app, launch)
            if feedback:
                with open(RETRY_PROMPT, encoding="utf-8") as handle:
                    retry_prompt = handle.read()
                prompt += retry_prompt.replace(
                    "{{FEEDBACK}}", feedback[-8000:])
            ok, output = invoke_model(prompt, app, tag=name, config=config)
            if not ok:
                feedback = output
                log(f"   模型执行本身失败：{output[:400]}")
                continue
        elif stage.get("run"):
            ok, output = stage["run"]()
            if not ok:
                feedback = output
                log(f"   脚本失败：{output[-1500:]}")
                if not stage.get("on_fail_redo"):
                    continue

        passed, detail = stage["check"]()
        if passed:
            log(f"   通过：{detail}")
            return True, detail
        feedback = detail
        log(f"   验收不通过：{detail[:1500]}")

        if stage.get("on_fail_redo") and not stage.get("prompt"):
            return False, detail

    return False, feedback


def onboard(app, launch, only=None, max_attempts=DEFAULT_MAX_ATTEMPTS,
            min_pass_rate=1.0, redo_limit=2, force=False,
            allow_legacy=False, config=None, state=None):
    global ACTIVE_CONFIG
    config = config or system_config.load()
    ACTIVE_CONFIG = config
    state = state or load_state(app)
    run_id = state.setdefault("workflow_run_id", f"onboard-{app}")
    phases = workflow_stages(
        app, launch, min_pass_rate, config, run_id=run_id)
    plan = [step for phase in phases for step in phase["steps"]]
    phase_by_step = {
        step["name"]: phase["name"]
        for phase in phases for step in phase["steps"]
    }
    by_name = {stage["name"]: stage for stage in plan}
    redo_used = {}

    log(f"软件 {app}，共 {len(plan)} 道工序"
        + (f"，只跑 {only}" if only else "")
        + (f"，已完成 {len(state['done'])} 道" if state["done"] else ""))

    index = 0
    while index < len(plan):
        stage = plan[index]
        name = stage["name"]

        if only and only not in name:
            index += 1
            continue
        if name in state["done"] and not only:
            log(f"── {name} 已完成，跳过（要从头运行加 --no-resume）")
            index += 1
            continue

        passed, detail = run_stage(stage, app, launch, max_attempts, config,
                                   force=force, allow_legacy=allow_legacy)
        state["history"].append({
            "phase": phase_by_step[name], "stage": name,
            "owner": stage["owner"], "passed": passed,
            "detail": detail[:4000], "at": time.strftime("%Y-%m-%d %H:%M:%S")})

        if passed:
            if name not in state["done"]:
                state["done"].append(name)
            save_state(app, state)
            index += 1
            continue

        save_state(app, state)
        redo_target = stage.get("on_fail_redo")
        if redo_target and redo_used.get(redo_target, 0) < redo_limit:
            redo_used[redo_target] = redo_used.get(redo_target, 0) + 1
            log(f"   回到「{redo_target}」重做"
                f"（第 {redo_used[redo_target]}/{redo_limit} 次），"
                "把验收输出带过去")
            if redo_target in state["done"]:
                state["done"].remove(redo_target)
            target_stage = by_name[redo_target]
            target_stage["_feedback"] = detail
            index = plan.index(target_stage)
            continue

        raise StageFailed(name, detail)

    if only:
        log(f"指定工序完成：{only}。这不代表后续工序或 CLI 已经完成。")
    else:
        log(f"完成。CLI 在 dist/{app}-axis/，启动器 bin/{app}-axis")
    return state


def main():
    parser = argparse.ArgumentParser(
        description="放进一个软件，产出一条能跑的 CLI")
    parser.add_argument("--app", required=True, help="软件名，也是 apps/ 下的目录名")
    parser.add_argument("--launch", default="",
                        help="这个软件的启动命令，交给模型做探测的起点")
    parser.add_argument("--only", default=None,
                        help="只跑名字里含这个字样的那道工序")
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument(
        "--resume", dest="resume", action="store_true", default=True,
        help="恢复已有总进度和各阶段运行（默认）")
    resume.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="归档旧总进度，使用新运行编号从第一道工序重新开始")
    resume.add_argument(
        "--redo", dest="resume", action="store_false",
        help="--no-resume 的兼容旧名称")
    parser.add_argument("--max-attempts", type=int,
                        help="每道工序内部的重试上限")
    parser.add_argument("--min-pass-rate", type=float,
                        help="验证门要求的通过率，默认必须 100%%")
    parser.add_argument("--plan", action="store_true",
                        help="只打印工序表和职责划分，不执行")
    parser.add_argument("--allow-legacy", action="store_true",
                        help="普查验收不通过时只打印缺口、不让模型重写。"
                             "给早期接入、已经发布过命令的软件用；"
                             "新软件不要加这个开关。")
    parser.add_argument(
        "--config", help="AXIS 全局配置文件；默认使用根目录 axis-config.json")
    parser.add_argument("--redo-limit", type=int,
                        help="阶段失败后回到前置模型阶段的次数")
    options = parser.parse_args()
    config = system_config.load(options.config)
    onboarding_config = config["workflow"]["onboarding"]
    max_attempts = options.max_attempts or onboarding_config[
        "stage_attempt_limit"]
    min_pass_rate = (onboarding_config["minimum_pass_rate"]
                     if options.min_pass_rate is None
                     else options.min_pass_rate)
    redo_limit = (onboarding_config["redo_limit"]
                  if options.redo_limit is None else options.redo_limit)

    if options.plan:
        print(f"\n软件 {options.app} 的流水线\n" + "=" * 68)
        print(f"{'工序':<22}{'归谁':<8}{'依据'}")
        print("-" * 68)
        for phase in workflow_stages(
                options.app, options.launch, min_pass_rate, config):
            print(f"\n{phase['name']}")
            for stage in phase["steps"]:
                basis = (stage.get("prompt") or stage.get("basis")
                         or "确定性脚本")
                print(f"{stage['name']:<22}{stage['owner']:<8}{basis}")
        print("\n验收一律归脚本。模型每交一样东西，脚本立刻验；不过就把验收"
              "输出原文贴回提示词重做。")
        return 0

    if options.resume:
        state = load_state(options.app)
        log("运行方式：恢复已有进度")
    else:
        state = start_fresh_state(options.app)
        log(f"运行方式：从头开始；新运行编号 {state['workflow_run_id']}")

    try:
        onboard(options.app, options.launch, only=options.only,
                max_attempts=max_attempts,
                min_pass_rate=min_pass_rate, redo_limit=redo_limit,
                force=not options.resume,
                allow_legacy=options.allow_legacy,
                config=config, state=state)
    except StageFailed as error:
        print(f"\n流水线停在 {error.stage}\n\n{error.detail}\n", file=sys.stderr)
        if options.resume:
            print("进度已存档，修好之后重跑同一条命令会从这一道继续。",
                  file=sys.stderr)
        else:
            print("进度已存档；修好之后改用 --resume，才会从这一道继续。",
                  file=sys.stderr)
        return 1
    except runtime.RuntimeError_ as error:
        print(f"\n{error}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
