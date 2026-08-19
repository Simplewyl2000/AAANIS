#!/usr/bin/env python3
"""Turn Stage-1 semantic approvals into implementation work orders."""

import argparse
import json
import sys
from pathlib import Path

from routing import command_name, route_for


AXIS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AXIS))
from axis import system_config  # noqa: E402
# 第一阶段的运行结果有两个落点：新跑的在它自己的 runs 目录下，历史那批在
# 实验目录下。两处都要找，否则刚跑完的调查在这一步会被当成不存在。
RESULT_ROOTS = (
    AXIS / "stages" / "01_capability_discovery" / "runs",
    AXIS / "experiments" / "task_minimum_subset" / "results",
)
RESULTS = RESULT_ROOTS[1]
SELECTION = RESULTS / "minimum-20260728" / "empirical_minimum_subset.json"
RUNS = (
    "minimum-discovery-20260728",
    "minimum-discovery-retry-20260728",
    "minimum-discovery-single-20260729",
    "minimum-discovery-final-20260729",
)
SUPPLEMENTAL_RESULTS = (
    AXIS / "experiments" / "framework_validation"
    / "results" / "validation-20260728"
)
PIPELINE_REQUIREMENTS = Path(__file__).with_name(
    "pipeline_requirements.json")
APP_DIR = {}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def selection_from_accepted(accepted):
    """不给挑选清单时，就把第一阶段所有已接受的发现全部纳入。

    原来这一步必须喂一份挑选清单，清单是照着评测任务反推出来的，等于让产出
    的命令跟着评测走。全量模式下不做这层挑选：第一阶段认可了多少能力，就
    实现多少条命令。
    """
    by_app = {}
    for app, key in accepted:
        by_app.setdefault(app, []).append(key)
    return {"apps": [{"app": app, "remaining_keys": sorted(keys)}
                     for app, keys in sorted(by_app.items())]}


def implementation_discovery(assigned, reviewed):
    """Build an implementation work order without claiming runtime proof."""
    record = assigned.get("record")
    record = record if isinstance(record, dict) else {}
    symbol = str(assigned.get("symbol", "capability"))
    member = record.get("member")
    runtime_object = record.get("object")
    if member and runtime_object:
        object_name = str(runtime_object).rsplit(".", 1)[-1]
        suggested_name = f"{object_name}-{member}"
    else:
        suggested_name = symbol
    raw_parameters = record.get("params", [])
    parameters = [
        {"name": str(value.get("name", "parameter")),
         "type": str(value.get("type", "unknown"))}
        for value in raw_parameters if isinstance(value, dict)
    ]
    return {
        "disposition": "pending_implementation",
        "reason": reviewed["reason"],
        "runtime_object": str(runtime_object or ""),
        "operation": symbol,
        "parameters": parameters,
        "command_candidates": [{
            "name": suggested_name,
            "capability_class": "to_be_determined_during_implementation",
            "selector": "",
            "arguments": [],
            "implementation_route": "single_session_operation",
            "persistence_check": (
                "由实现 Agent 根据实际命令效果确定；随后由 Python 验证"),
        }],
        "requires_runtime_investigation": True,
    }


def accepted_items(run_names):
    accepted = {}
    run_dirs = []
    for run_id in run_names:
        for root in RESULT_ROOTS:
            if (root / run_id).is_dir():
                run_dirs.append(root / run_id)
    for run_dir in run_dirs:
        modern_state = run_dir / "state.json"
        if modern_state.is_file():
            state = load(modern_state)
            for batch_state in state.get("batches", []):
                batch_dir = run_dir / batch_state["path"]
                original_batch = load(batch_dir / "batch.json")
                assigned_by_id = {
                    item["item_id"]: item
                    for item in original_batch["items"]}
                for item_id, item_state in batch_state.get(
                        "items", {}).items():
                    if item_state.get("status") != "accepted":
                        continue
                    attempt = run_dir / item_state["accepted_attempt"]
                    review = load(attempt / "review_result.json")
                    reviewed = next(
                        item for item in review["items"]
                        if item["item_id"] == item_id)
                    if reviewed.get("decision") != "expose":
                        continue
                    assigned = assigned_by_id[item_id]
                    # item_id is the census identity.  Symbol names are only
                    # labels and can legitimately repeat for two registry
                    # entries (for example Group and PathElement in Inkex).
                    key = item_id
                    accepted[(original_batch["app"], key)] = {
                        "key": key,
                        "command_symbol": assigned["symbol"],
                        "assigned": assigned,
                        "review": reviewed,
                        "discovery": implementation_discovery(
                            assigned, reviewed),
                        "source": str(attempt.relative_to(AXIS)),
                    }
        for state_path in sorted(run_dir.glob("*/state.json")):
            app = state_path.parent.name
            state = load(state_path)
            for batch_id, status in state.get("batches", {}).items():
                if status.get("status") != "accepted":
                    continue
                attempt = state_path.parent / status["accepted_attempt"]
                batch = load(attempt / "batch.json")
                result = load(attempt / "discovery_result.json")
                by_id = {item["item_id"]: item for item in result["items"]}
                for assigned in batch["items"]:
                    item_id = assigned["item_id"]
                    discovered = by_id[item_id]
                    key = assigned["symbol"]
                    if discovered.get("disposition") != "executable":
                        raise RuntimeError(
                            f"{app}/{key} accepted but is not executable")
                    accepted[(app, key)] = {
                        "key": key,
                        "assigned": assigned,
                        "discovery": discovered,
                        "source": str(attempt.relative_to(AXIS)),
                    }
    return accepted


def supplemental_items(paths):
    items = {}
    for path in paths:
        result = load(path)
        app = result["app"]
        for discovered in result.get("items", []):
            if discovered.get("disposition") != "executable":
                continue
            key = f"supplemental:{discovered['item_id']}"
            try:
                source = str(path.relative_to(AXIS))
            except ValueError:
                source = str(path)
            items[(app, key)] = {
                "key": key,
                "assigned": {
                    "item_id": discovered["item_id"],
                    "symbol": discovered.get(
                        "runtime_object", discovered["item_id"]),
                },
                "discovery": discovered,
                "source": source,
            }
    return items


def command_needed(app_dir, command):
    atlas = AXIS / "apps" / app_dir / "atlas" / f"{command}.json"
    report_path = AXIS / "apps" / app_dir / "build" / "report.json"
    if not atlas.is_file() or not report_path.is_file():
        return True
    report = load(report_path)
    return not report.get(command, {}).get("pass", False)


def expand_item(item):
    """Expand every discovered operation; never collapse an object to one command."""
    discovery = item["discovery"]
    assigned_record = item.get("assigned", {}).get("record") or {}
    if not isinstance(assigned_record, dict):
        assigned_record = {}
    candidates = discovery.get("command_candidates") or [{}]
    expanded = []
    seen = set()
    for candidate in candidates:
        command = command_name(
            item.get("command_symbol", item["key"]), candidate)
        if command in seen:
            continue
        seen.add(command)
        row = dict(item)
        row["command"] = command
        row["candidate"] = candidate
        row["generation_route"] = route_for(discovery, candidate)
        row["object_family"] = str(
            discovery.get("runtime_object")
            or assigned_record.get("object")
            or f"command:{command}")
        row["persistence_check"] = candidate.get(
            "persistence_check",
            "执行后保存文件，关闭并重新打开，再用独立读取命令确认目标状态")
        expanded.append(row)
    return expanded


def required_operations():
    """读取清单：经真实任务验证为必需、重新生成时不得遗漏的命令。"""
    payload = load(PIPELINE_REQUIREMENTS)
    defaults = payload["generation_defaults"]
    result = {}
    for app, row in payload["apps"].items():
        app_dir = row["app_dir"]
        operations = []
        for command in row["commands"]:
            atlas = AXIS / "apps" / app_dir / "atlas" / f"{command}.json"
            cell = load(atlas) if atlas.is_file() else {}
            binding = cell.get("binding", {})
            operations.append({
                "key": f"required:{command}",
                "command": command,
                "assigned": {
                    "item_id": f"required:{app}:{command}",
                    "symbol": (cell.get("census_ref") or {}).get(
                        "symbol", command),
                },
                "discovery": {
                    "disposition": "executable",
                    "runtime_object": binding.get("runtime_type", ""),
                    "operation": binding.get(
                        "action", binding.get(
                            "capability", command)),
                    "parameters": [
                        {"name": name, "type": spec.get("type", "scalar")}
                        for name, spec in binding.get(
                            "cli_args", {}).items()
                    ],
                    "command_candidates": [{
                        "name": command,
                        "capability_class": cell.get(
                            "capability_class", "action"),
                        "implementation_route": defaults[
                            "generation_route"],
                        "persistence_check": defaults[
                            "persistence_check"],
                    }],
                },
                "candidate": {
                    "name": command,
                    "capability_class": cell.get(
                        "capability_class", "action"),
                },
                "generation_route": defaults["generation_route"],
                "persistence_check": defaults["persistence_check"],
                "source": str(
                    atlas.relative_to(AXIS)) if atlas.is_file()
                    else str(PIPELINE_REQUIREMENTS.relative_to(AXIS)),
                "source_items": [f"required:{command}"],
                "required_release_command": True,
            })
        result[app] = operations
    return result


def main():
    global APP_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="commands-20260729")
    parser.add_argument(
        "--selection", type=Path, default=SELECTION,
        help="挑选清单；传 --all-accepted 时忽略")
    parser.add_argument(
        "--all-accepted", action="store_true",
        help="不按挑选清单，把第一阶段所有已接受的发现全部实现成命令")
    parser.add_argument(
        "--apps", default="",
        help="逗号分隔，只处理这几款软件；留空表示全部")
    parser.add_argument(
        "--config", help="AXIS 全局配置；默认读取根目录 axis-config.json")
    parser.add_argument(
        "--discovery-run", action="append", dest="discovery_runs",
        help="可重复；默认读取既有四轮发现结果")
    parser.add_argument(
        "--discovery-result", action="append", type=Path, default=[],
        help="补充的 discovery_result.json；可重复")
    parser.add_argument(
        "--include-framework-results",
        action=argparse.BooleanOptionalAction, default=True,
        help="若存在框架验证结果，则纳入其中已接受的发现")
    parser.add_argument(
        "--only-needed", action=argparse.BooleanOptionalAction, default=True,
        help="只生成 atlas 不存在或验证未通过的命令")
    parser.add_argument(
        "--include-pipeline-requirements",
        action=argparse.BooleanOptionalAction, default=True,
        help="纳入经过真实任务证明、以后每次发布都不得遗漏的命令")
    args = parser.parse_args()
    APP_DIR = system_config.load(args.config).get("app_aliases", {})

    if args.discovery_runs:
        discovery_runs = args.discovery_runs
    elif args.all_accepted:
        # 全量模式下不挑运行：所有跑过的第一阶段结果一律纳入。
        discovery_runs = sorted({
            path.name for root in RESULT_ROOTS if root.is_dir()
            for path in root.iterdir() if path.is_dir()})
    else:
        discovery_runs = RUNS
    accepted = accepted_items(discovery_runs)
    supplemental_paths = list(args.discovery_result)
    if args.include_framework_results and SUPPLEMENTAL_RESULTS.is_dir():
        supplemental_paths.extend(sorted(
            SUPPLEMENTAL_RESULTS.glob("*/attempt-*/discovery_result.json")))
    supplements = supplemental_items(dict.fromkeys(supplemental_paths))
    accepted.update(supplements)

    if args.all_accepted:
        selection = selection_from_accepted(accepted)
        if not selection["apps"]:
            raise SystemExit(
                "第一阶段没有任何已接受的发现，没有东西可以实现。\n"
                "先跑：python3 stages/01_capability_discovery/workflow.py run")
    else:
        selection = load(args.selection)
        expected = {
            (app["app"], key)
            for app in selection["apps"]
            for key in app["remaining_keys"]
        }
        actual = set(accepted)
        if not expected <= actual:
            raise SystemExit(
                f"accepted discoveries do not match selection; "
                f"missing={sorted(expected-actual)}")

    if args.apps:
        wanted = {name.strip() for name in args.apps.split(",") if name.strip()}
        selection = {"apps": [row for row in selection["apps"]
                              if row["app"] in wanted
                              or APP_DIR.get(row["app"], row["app"]) in wanted]}

    out = Path(__file__).parent / "runs" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    totals = {}
    required = required_operations() if args.include_pipeline_requirements else {}
    for app_row in selection["apps"]:
        app = app_row["app"]
        app_dir = APP_DIR.get(app, app)
        operations = []
        selected_keys = list(app_row["remaining_keys"])
        selected_keys.extend(
            key for supplement_app, key in supplements
            if supplement_app == app)
        for key in selected_keys:
            operations.extend(expand_item(accepted[(app, key)]))
        # Several runtime registry entries can legitimately lead to the same
        # public command.  Merge their provenance, but never emit/implement the
        # command twice.
        by_command = {}
        for item in operations:
            name = item["command"]
            if name not in by_command:
                item["source_items"] = [item["key"]]
                by_command[name] = item
            else:
                by_command[name]["source_items"].append(item["key"])
                if by_command[name]["generation_route"] != item["generation_route"]:
                    route_priority = {
                        "direct_property": 0, "validated_enum": 1,
                        "validated_file": 2, "structured_region": 3,
                        "construct_runtime_struct": 4,
                        "construct_typed_sequence": 5,
                        "resolve_runtime_object": 6,
                        "create_from_runtime_factory": 7,
                        "single_session_operation": 8,
                    }
                    # A command shared by several registry entries must use the
                    # strongest required route, not the easiest source.
                    if (route_priority[item["generation_route"]]
                            > route_priority[
                                by_command[name]["generation_route"]]):
                        by_command[name]["generation_route"] = item[
                            "generation_route"]
        operations = list(by_command.values())
        for item in required.get(app, []):
            if item["command"] not in {
                    operation["command"] for operation in operations}:
                operations.append(item)
        if args.only_needed:
            operations = [
                item for item in operations
                if command_needed(app_dir, item["command"])
            ]
        manifest = {
            "schema_version": 1,
            "run_id": args.run_id,
            "app": app,
            "app_dir": app_dir,
            "operation_count": len(operations),
            "operations": operations,
        }
        app_out = out / app
        app_out.mkdir(exist_ok=True)
        (app_out / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        totals[app] = {
            "discovered_items": len(selected_keys),
            "commands": len(operations),
            "required_release_commands": len(required.get(app, [])),
        }

    summary = {
        "run_id": args.run_id,
        "total_discovered_items": sum(
            item["discovered_items"] for item in totals.values()),
        "total_commands": sum(item["commands"] for item in totals.values()),
        "apps": totals,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
