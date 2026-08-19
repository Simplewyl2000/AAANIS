"""<app>-axis 命令行入口（内核的一部分，人工维护）。

用法：
  <app>-axis dir                             只列命令类别和每类数量
  <app>-axis dir <category>                  列出一个类别内的命令
  <app>-axis dir --search "关键词"           按名称和作用检索命令
  <app>-axis dir <command>                   查看该命令完整说明书（作用/参数表/调用示例）
  <app>-axis guide                           一次读取完整精简命令文档
  <app>-axis <command> --help                同上
  <app>-axis <command> --schema              查看参数契约（含调用示例）
  <app>-axis <command> --recipe              查看验证过的端到端示例
  <app>-axis <command> --k v ...             执行
  <app>-axis observe --file x                查看文档完整状态（内置）
  <app>-axis make-demo --file x --text "..." 生成演示文档（内置，供 recipe/探针自包含；
                                             目标已存在时拒绝覆盖，--force true 除外）
"""
import inspect
import json
import os
import shlex
import sys

from axis import kernel

# Only interface controls are valueless. All command parameters, including
# booleans such as force/overwrite, use the single form ``--name true|false``.
_FLAG_NAMES = ("schema", "recipe", "help")

# 内置命令说明书（--schema/--help/dir <name> 时只展示，不执行）
_BUILTIN_SPECS = {
    "observe": {
        "summary": "查看文档完整状态（内置）：标题、图层/对象列表及各项状态",
        "args": {"file": {"type": "path", "required": True,
                          "help": "产物文件路径"}},
    },
    "make-demo": {
        "summary": "生成演示文档（内置）：供 recipe/探针自包含；目标或同名数据文件"
                   "已存在时拒绝覆盖，--force true 除外",
        "args": {"file": {"type": "path", "required": True,
                          "help": "演示文档路径"},
                 "text": {"type": "string",
                          "help": "演示内容，字面 \\n 或换行分隔"},
                 "table": {"type": "string",
                           "help": "行用 | 分隔、单元格用 , 分隔，如 'A,B|C,D'"},
                 "force": {"type": "bool", "default": False,
                           "help": "允许覆盖已存在的同名文件（默认拒绝，防误覆盖）"}},
    },
    "dir": {
        "summary": "命令发现（内置）：dir 列类别；dir --search 关键词检索；dir <命令> 查看完整说明书",
        "args": {
            "command": {"type": "string",
                        "help": "要查看说明书的命令名（可选，位置参数）"},
            "search": {"type": "string",
                       "help": "按命令名和作用说明检索；多个词都必须匹配"},
        },
    },
    "guide": {
        "summary": "读取实现完成后生成的完整精简命令文档",
        "args": {},
    },
}


def _tool_name(app_dir):
    """Derive the launcher name from an application package directory."""
    base = os.path.basename(os.path.normpath(app_dir))
    return base if base.endswith("-axis") else base + "-axis"


def _file_example(app_dir):
    """Read the display-only example filename from the app-owned contract."""
    path = os.path.join(app_dir, "axis-app.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                contract = json.load(handle)
            example = (contract.get("documentation") or {}).get(
                "example_file")
            if isinstance(example, str) and example.strip():
                return example.strip()
        except (OSError, ValueError):
            pass
    return "demo.out"


def _example_for(app_dir, spec):
    """Render one copyable example from the command's verified demo values."""
    tool = _tool_name(app_dir)
    parts = [tool, spec["command"], "--file", _file_example(app_dir)]
    demo_args = (spec.get("demo") or {}).get("args", {})
    for k, v in demo_args.items():
        if k not in spec.get("args", {}):
            continue
        # 与 spec recipe 同款的 shell 形态：字符串加引号，其余走 JSON 字面量
        cli_key = k.replace("_", "-")
        parts += ([f"--{cli_key}", shlex.quote(str(v))] if isinstance(v, str)
                  else [f"--{cli_key}", json.dumps(v)])
    return " ".join(parts)


def _builtin_example(app_dir, name):
    tool, f = _tool_name(app_dir), _file_example(app_dir)
    if name == "observe":
        return f"{tool} observe --file {f}"
    if name == "make-demo":
        return f"{tool} make-demo --file {f} --text 'Alpha\\nBeta'"
    if name == "guide":
        return f"{tool} guide"
    return (f"{tool} dir ；{tool} dir --search '关键词'；"
            f"{tool} dir <命令名>")


def _parse(argv):
    """拆分元控制开关与参数；所有业务参数都必须写成 ``--k v``。"""
    if not argv:
        return None, set(), {}
    command, flags, kv = argv[0], set(), {}
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("--"):
            kernel.emit({"status": "error", "code": "INVALID_ARGS",
                         "message": f"意外的参数 {tok!r}", "fix": "参数写法是 --名字 值"},
                        kernel.EXIT_BAD_ARGS)
        name = tok[2:]
        if name in _FLAG_NAMES:
            flags.add(name)
            i += 1
        else:
            if i + 1 >= len(argv):
                kernel.emit({"status": "error", "code": "INVALID_ARGS",
                             "message": f"--{name} 缺少值", "fix": "参数写法是 --名字 值"},
                            kernel.EXIT_BAD_ARGS)
            kv[name] = argv[i + 1]
            i += 2
    return command, flags, kv


def _normalize_cli_keys(kv, spec):
    """Accept conventional --kebab-case for internal snake_case arguments."""
    known = spec.get("args", {})
    normalized = {}
    for key, value in kv.items():
        target = key
        snake = key.replace("-", "_")
        if key not in known and snake in known:
            target = snake
        if target in normalized:
            kernel.emit(
                {"status": "error", "code": "INVALID_ARGS",
                 "message": f"参数 --{key} 与另一写法重复",
                 "fix": f"只保留 --{target.replace('_', '-')} 一种写法"},
                kernel.EXIT_BAD_ARGS)
        normalized[target] = value
    return normalized


def _iter_specs(app_dir):
    """按完整命令文档冻结的组织顺序遍历命令说明。"""
    cmd_root = os.path.join(app_dir, "commands")
    if os.path.isdir(cmd_root):
        available = os.listdir(cmd_root)
        ordered = []
        guide_path = os.path.join(app_dir, "guide", "commands.json")
        if os.path.isfile(guide_path):
            with open(guide_path, encoding="utf-8") as handle:
                guide = json.load(handle)
            ordered.extend(
                row.get("command") for row in guide.get("commands", [])
                if isinstance(row, dict) and row.get("command") in available)
        seen = set(ordered)
        ordered.extend(name for name in available if name not in seen)
        for name in ordered:
            spec_path = os.path.join(cmd_root, name, "spec.json")
            if os.path.isfile(spec_path):
                with open(spec_path, encoding="utf-8") as f:
                    yield name, json.load(f)


def _catalog(app_dir):
    """构造分层目录；顶层调用者只拿类别，不展开全部命令。"""
    builtin_rows = [{"command": n, "summary": _BUILTIN_SPECS[n]["summary"]}
                    for n in ("dir", "guide", "observe", "make-demo")]
    by_obj = {"builtin": builtin_rows}
    for name, spec in _iter_specs(app_dir):
        obj = spec.get("object") or name.split("-")[0]
        by_obj.setdefault(obj, []).append(
            {"command": name, "summary": spec.get("summary", "")})
    return by_obj


def _dir_list(app_dir):
    """dir / --list：只列类别和数量，避免把全部命令一次塞给 agent。"""
    tool = _tool_name(app_dir)
    by_obj = _catalog(app_dir)
    categories = [
        {"category": obj, "command_count": len(by_obj[obj]),
         "summary": ("AXIS 内置命令" if obj == "builtin"
                     else f"{obj} 对象相关命令")}
        for obj in by_obj
    ]
    kernel.emit({
        "status": "ok",
        "category_count": len(categories),
        "command_count": sum(x["command_count"] for x in categories),
        "categories": categories,
        "usage": (f"{tool} dir <类别> 查看该类命令；"
                  f"{tool} dir --search '关键词' 检索；"
                  f"{tool} dir <命令> 查看完整说明书"),
    }, kernel.EXIT_OK)


def _dir_category(app_dir, category):
    """dir <类别>：只展开一个类别内的命令。"""
    tool = _tool_name(app_dir)
    commands = _catalog(app_dir).get(category)
    if commands is None:
        kernel.emit({
            "status": "error", "code": "UNKNOWN_CATEGORY",
            "message": f"没有类别 {category!r}",
            "fix": f"用 {tool} dir 查看全部类别",
        }, kernel.EXIT_BAD_ARGS)
    kernel.emit({
        "status": "ok", "category": category,
        "command_count": len(commands), "commands": commands,
        "usage": f"{tool} dir <命令> 查看完整说明书",
    }, kernel.EXIT_OK)


def _dir_search(app_dir, query):
    """按命令名、类别和作用说明检索；多个空格分隔词必须同时匹配。"""
    tool = _tool_name(app_dir)
    terms = [term.casefold() for term in str(query).split() if term.strip()]
    if not terms:
        kernel.emit({
            "status": "error", "code": "INVALID_ARGS",
            "message": "--search 需要至少一个关键词",
            "fix": f"{tool} dir --search '要完成的动作或对象'",
        }, kernel.EXIT_BAD_ARGS)
    matches = []
    for category, rows in _catalog(app_dir).items():
        for row in rows:
            haystack = " ".join((
                category, row.get("command", ""), row.get("summary", "")
            )).casefold()
            if all(term in haystack for term in terms):
                matches.append({
                    "command": row["command"],
                    "category": category,
                    "summary": row.get("summary", ""),
                })
    kernel.emit({
        "status": "ok",
        "query": query,
        "match_count": len(matches),
        "commands": matches,
        "usage": f"{tool} dir <命令> 查看完整说明书",
    }, kernel.EXIT_OK)


def _dir_show(app_dir, name):
    """dir <name> / <name> --help / 内置命令 --schema：完整说明书，不执行。"""
    tool = _tool_name(app_dir)
    if name in _BUILTIN_SPECS:
        b = _BUILTIN_SPECS[name]
        kernel.emit({"status": "ok", "command": name, "summary": b["summary"],
                     "args": b["args"], "example": _builtin_example(app_dir, name)},
                    kernel.EXIT_OK)
    cmd_dir = os.path.join(app_dir, "commands", name)
    if not os.path.isfile(os.path.join(cmd_dir, "spec.json")):
        kernel.emit({"status": "error", "code": "UNKNOWN_COMMAND",
                     "message": f"没有命令 {name!r}",
                     "fix": f"用 {tool} dir 查看全部命令"}, kernel.EXIT_BAD_ARGS)
    spec = kernel.load_spec(cmd_dir)
    kernel.emit({"status": "ok", "command": spec.get("command", name),
                 "summary": spec.get("summary", ""), "args": spec.get("args", {}),
                 "example": _example_for(app_dir, spec),
                 "errors": spec.get("errors", {}),
                 "related": spec.get("related", {})}, kernel.EXIT_OK)


def _builtin(app_dir, command, kv, flags):
    import engine  # app 目录下的固定引擎助手
    try:
        if command == "guide":
            path = os.path.join(app_dir, "guide", "commands.json")
            if not os.path.isfile(path):
                raise kernel.AxisError(
                    "GUIDE_UNAVAILABLE", "当前发布包没有完整命令文档",
                    "完成命令实现后的文档阶段并重新冻结发布包")
            with open(path, encoding="utf-8") as handle:
                guide = json.load(handle)
            kernel.emit({"status": "ok", "guide": guide}, kernel.EXIT_OK)
        if command == "observe":
            if "file" not in kv:
                raise kernel.AxisError("INVALID_ARGS", "缺少 --file", "observe --file <文档路径>")
            kernel.emit({"status": "ok", "state": engine.observe(kv["file"])}, kernel.EXIT_OK)
        if command == "make-demo":
            args = kernel.validate(
                _BUILTIN_SPECS["make-demo"],
                _normalize_cli_keys(kv, _BUILTIN_SPECS["make-demo"]))
            path = args.get("file", "demo.odt")
            force = args["force"]
            if os.path.exists(path) and not force:
                raise kernel.AxisError(
                    "OUTPUT_EXISTS",
                    f"make-demo 拒绝覆盖已存在的文件：{path}",
                    "换 --file 目标路径；确认文件无用后加 --force true 重试")
            table_cells = None
            if "table" in kv:  # 行用 | 分隔、单元格用 , 分隔，如 'A,B|C,D'
                table_cells = [row.split(",") for row in kv["table"].split("|")]
            # force 是后加的能力：引擎支持才透传（旧引擎由上面的存在性检查兜底）
            if "force" in inspect.signature(engine.make_demo_doc).parameters:
                engine.make_demo_doc(path, args.get("text"), table_cells, force=force)
            else:
                engine.make_demo_doc(path, args.get("text"), table_cells)
            kernel.emit({"status": "ok", "command": "make-demo", "file": path},
                        kernel.EXIT_OK)
    except kernel.AxisError as e:
        kernel.emit({"status": "error", "code": e.code, "message": str(e), "fix": e.fix},
                    kernel.EXIT_CMD_ERROR)


def main():
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "--app":
        app_dir, argv = os.path.abspath(argv[1]), argv[2:]
    else:
        kernel.emit({"status": "error", "code": "INVALID_ARGS",
                     "message": "缺少 --app <应用目录>", "fix": "请通过 bin/ 下的包装脚本调用"},
                    kernel.EXIT_BAD_ARGS)
    sys.path.insert(0, app_dir)  # 让 impl.py 和内置命令能 import engine

    # dir 带位置参数（dir <类别或命令>），须在 _parse 之前拦截
    if argv and argv[0] == "dir":
        rest = argv[1:]
        if not rest:
            _dir_list(app_dir)
        if len(rest) == 2 and rest[0] == "--search":
            _dir_search(app_dir, rest[1])
        if len(rest) == 1 and rest[0] == "--help":
            _dir_show(app_dir, "dir")
        if len(rest) == 1 and not rest[0].startswith("--"):
            target = rest[0]
            if target in _BUILTIN_SPECS or os.path.isfile(os.path.join(
                    app_dir, "commands", target, "spec.json")):
                _dir_show(app_dir, target)
            _dir_category(app_dir, target)
        kernel.emit({"status": "error", "code": "INVALID_ARGS",
                     "message": "dir 用法：dir [类别或命令名] 或 dir --search <关键词>",
                     "fix": (f"{_tool_name(app_dir)} dir 或 "
                             f"{_tool_name(app_dir)} dir --search '关键词' 或 "
                             f"{_tool_name(app_dir)} dir <类别或命令名>")},
                    kernel.EXIT_BAD_ARGS)

    command, flags, kv = _parse(argv)
    if command in (None, "--list", "--help"):
        _dir_list(app_dir)
    if command in ("guide", "observe", "make-demo"):
        if "schema" in flags or "help" in flags:
            _dir_show(app_dir, command)  # 内置命令：只展示说明书，不执行
        _builtin(app_dir, command, kv, flags)

    cmd_dir = os.path.join(app_dir, "commands", command)
    if not os.path.isfile(os.path.join(cmd_dir, "spec.json")):
        kernel.emit({"status": "error", "code": "UNKNOWN_COMMAND",
                     "message": f"没有命令 {command!r}",
                     "fix": f"用 {_tool_name(app_dir)} dir 查看全部命令"},
                    kernel.EXIT_BAD_ARGS)
    spec = kernel.load_spec(cmd_dir)
    if "schema" in flags or "help" in flags:
        kernel.emit({"status": "ok", "command": spec.get("command", command),
                     "summary": spec.get("summary", ""), "args": spec.get("args", {}),
                     "example": _example_for(app_dir, spec),
                     "errors": spec.get("errors", {}),
                     "related": spec.get("related", {})}, kernel.EXIT_OK)
    if "recipe" in flags:
        kernel.emit({"status": "ok", "recipe": spec.get("recipe", {})}, kernel.EXIT_OK)
    kv = _normalize_cli_keys(kv, spec)
    kernel.run(cmd_dir, kv)


if __name__ == "__main__":
    main()
