"""AXIS 构建器（S4 模板绑定）：atlas 格子 -> 命令目录。

输入：apps/<app>/atlas/*.json（每格一条：binding + demo + census_ref 证据）。
输出：apps/<app>/commands/<command>/{spec.json, impl.py}。

impl.py 是模板渲染的薄壳：全部执行逻辑在 axis/proptemplate.py（TCB），
impl 只携带 BINDING 数据——"绑定而非发明"的落地（METHOD.md 转向一）。

格子 JSON 契约（S2 工序产出，逐格带运行时核验证据）：
{
  "command": "char-set-bold",          // 命名 = 格坐标 <object>-<action>
  "summary": "...",                     // 一句话说明（agent 在 --list 里看到）
  "binding": {
    "kind": "span_prop|para_prop|named_prop|global_prop",
    "path": "CharWeight",               // 引擎属性路径（必须落在 census 内）
    "value": {"type": "bool|int|float|string|enum|color|vec3",
              "threshold"?, "true"?, "false"?,          // bool
              "values"?, "names"?,                       // enum
              "min"?, "max"?}                            // int/float
  },
  "census_ref": {"channel": "properties.char", "symbol": "CharWeight"},
  "demo": {"text": "...", "args": {"match": "...", "value": true},
           "expect": true},
  "errors_extra": {"CODE": "说明"}      // 可选，格特有错误码
}

processing 格（带输入、参数和输出的动作命令）：
{
  "command": "native-buffer",
  "summary": "...",
  "binding": {
    "kind": "processing",
    "algorithm": "native:buffer",        // 必须落在 census dispatch.providers.* 内
    "output_param": "OUTPUT", "output_kind": "vector|raster",
    "inputs": [{"name": "INPUT", "arg": "input", "type": "source",
                "optional": false, "desc": "Input layer"}],
    "params": [{"name": "DISTANCE", "arg": "distance", "type": "distance",
                "spec_type": "float", "optional": false, "default": 10,
                "desc": "Distance", "options"? : [...]}]
  },
  "census_ref": {"channel": "dispatch.providers.native", "symbol": "native:buffer"},
  "demo": {"text": "...", "args": {"input": "cities", "output": "out",
           "distance": 10}, "expect": true}
}
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from axis import app_contract, runtime  # noqa: E402

IMPL_TEMPLATE = '''"""{command}：AXIS 模板生成，勿手改。绑定数据见 BINDING。"""
import engine
from axis import proptemplate
from axis.kernel import AxisError  # noqa: F401  (供 engine 抛错语义对齐)

BINDING = {binding}


def run(args):
    report = proptemplate.run(args, BINDING, engine)
    report["command"] = {command!r}
    return report
'''

ACTION_IMPL_TEMPLATE = '''"""{command}：AXIS 模板生成，勿手改。动作绑定见 BINDING。"""
import engine
from axis import actiontemplate
from axis.kernel import AxisError  # noqa: F401  (供 engine 抛错语义对齐)

BINDING = {binding}


def run(args):
    report = actiontemplate.run(args, BINDING, engine)
    report["command"] = {command!r}
    return report
'''

TRANSFORM_IMPL_TEMPLATE = '''"""{command}：AXIS 模板生成，勿手改。变换绑定见 BINDING。"""
import engine
from axis import transformtemplate
from axis.kernel import AxisError  # noqa: F401  (供 engine 抛错语义对齐)

BINDING = {binding}


def run(args):
    report = transformtemplate.run(args, BINDING, engine)
    report["command"] = {command!r}
    return report
'''

OBSERVATION_IMPL_TEMPLATE = '''"""{command}：AXIS 模板生成的独立只读观测命令。"""
import engine
from axis.kernel import AxisError  # noqa: F401

BINDING = {binding}


def run(args):
    report = engine.observation_run(args, BINDING)
    report["command"] = {command!r}
    return report
'''

GETTER_IMPL_TEMPLATE = '''"""{command}：AXIS 自动生成的独立属性读取命令。"""
import engine
from axis import proptemplate
from axis.kernel import AxisError  # noqa: F401

BINDING = {binding}


def run(args):
    report = proptemplate.get(args, BINDING, engine)
    report["command"] = {command!r}
    return report
'''

BASE_ERRORS = {
    "INPUT_NOT_FOUND": "输入文件不存在",
    "ENGINE_UNAVAILABLE": "引擎不可用",
    "TARGET_NOT_FOUND": "按选择器找不到目标",
    "OPEN_FAILED": "引擎打不开产物文件",
}


def value_args(vspec):
    """值参数随类型走：vec3 拆 x/y/z，其余统一 --value。"""
    vt = vspec["type"]
    if vt == "vec3":
        return {k: {"type": "float", "required": True, "help": f"{k} 分量"}
                for k in ("x", "y", "z")}
    if vt == "vec":
        return {"value": {"type": "vec", "size": vspec.get("size"),
                          "elem": vspec.get("elem", "float"), "required": True,
                          "help": f"{vspec.get('size', 'N')} 个分量，逗号分隔"}}
    arg = {"type": {"bool": "bool", "int": "int", "float": "float"}.get(vt, "string"),
           "required": vt != "bool",
           "help": "目标值"}
    if vt == "bool":
        arg["default"] = True
        arg["help"] = "true 设置 / false 取消"
    if vt == "enum":
        arg["type"] = "enum"
        arg["values"] = vspec["values"]
    if vt in ("int", "float"):
        for k in ("min", "max"):
            if k in vspec:
                arg[k] = vspec[k]
    if vt == "color":
        arg["help"] = "颜色，形如 #FF8800"
    return {"value": arg}


def _default_for_spec(p):
    """processing 参数默认值 -> spec arg 默认值；转不动就返回 None（不给默认）。"""
    d = p.get("default")
    if d is None:
        return None
    try:
        if p["spec_type"] == "enum":
            opts = p.get("options") or []
            return opts[int(d)] if isinstance(d, (int, float)) and 0 <= int(d) < len(opts) else d
        if p["spec_type"] == "bool":
            return bool(d)
        if p["spec_type"] == "float":
            return float(d)
        if p["spec_type"] == "int":
            return int(d)
        return str(d)
    except (TypeError, ValueError, IndexError):
        return None


def render_spec(cell, app=None, contract=None):
    contract = contract or app_contract.load(app)
    build_contract = contract["build"]
    selector_args = build_contract.get("selector_args", {})
    kind = cell["binding"]["kind"]
    args = {"file": {"type": "path", "required": True,
                     "help": build_contract.get(
                         "file_arg_help", "Artifact file path")}}
    if kind in ("transform", "verb", "observation", "processing"):
        args.update(cell["binding"].get("cli_args", {}))
    elif kind in selector_args:
        args.update(selector_args[kind])
        args.update(value_args(cell["binding"]["value"]))
    else:
        raise ValueError(
            f"binding kind {kind!r} has no cli_args or declared selector contract")
    cmd = cell["command"]
    demo = cell["demo"]
    demo_file = "demo" + runtime.document_extension(app)
    demo_args = " ".join(
        f"--{k} '{v}'" if isinstance(v, str) else f"--{k} {json.dumps(v)}"
        for k, v in demo["args"].items())
    recipe = {
        "setup": [f"make-demo --file {demo_file} --text '{demo.get('text', 'Demo text')}'"],
        "run": [f"{cmd} --file {demo_file} {demo_args}"],
        "expect": {"current": demo["expect"]},
    }
    errors = dict(BASE_ERRORS)
    errors.update(cell.get("errors_extra", {}))
    layer = "observation" if kind == "observation" else "primitive"
    capability_class = cell.get("capability_class") or {
        "observation": "observation",
        "transform": "action",
        "processing": "action",
        "verb": "action",
    }.get(kind, "property_write")
    spec = {"command": cmd, "summary": cell["summary"], "layer": layer,
            "capability_class": capability_class,
            "object": cmd.split("-")[0], "action": "-".join(cmd.split("-")[1:]),
            "args": args, "errors": errors, "recipe": recipe, "demo": demo,
            "binding": cell["binding"], "census_ref": cell.get("census_ref")}
    if kind in selector_args and "-set-" in cmd:
        getter = cmd.replace("-set-", "-get-", 1)
        spec["related"] = {
            "read_current": getter,
            "hint": f"读取当前值请用 {getter}",
        }
    return spec


def render_impl(cell):
    kind = cell["binding"]["kind"]
    tpl = (OBSERVATION_IMPL_TEMPLATE if kind == "observation"
           else ACTION_IMPL_TEMPLATE if kind in ("processing", "verb")
           else TRANSFORM_IMPL_TEMPLATE if kind == "transform"
           else IMPL_TEMPLATE)
    return tpl.format(command=cell["command"], binding=repr(cell["binding"]))


def render_getter(cell, setter_spec, selector_args):
    """从属性 setter 生成参数更少的 getter：保留 file 和选择器，删除值参数。"""
    setter = cell["command"]
    command = setter.replace("-set-", "-get-", 1)
    selector_names = {"file"} | set(selector_args[cell["binding"]["kind"]])
    args = {k: v for k, v in setter_spec["args"].items() if k in selector_names}
    spec = {
        "command": command,
        "summary": f"读取 {setter_spec['summary']} 对应属性的当前值",
        "layer": "observation",
        "capability_class": "observation",
        "object": setter_spec["object"],
        "action": "-".join(command.split("-")[1:]),
        "args": args,
        "errors": setter_spec["errors"],
        "demo": setter_spec["demo"],
        "binding": setter_spec["binding"],
        "census_ref": setter_spec["census_ref"],
        "paired_setter": setter,
        "related": {
            "write_value": setter,
            "hint": f"修改这个值请用 {setter}",
        },
    }
    return spec, GETTER_IMPL_TEMPLATE.format(
        command=command, binding=repr(cell["binding"]))


def main():
    ap = argparse.ArgumentParser(description="atlas 格子 -> 命令目录")
    ap.add_argument("--app", required=True)
    ap.add_argument(
        "--only",
        help="只构建这一条 atlas 命令；用于逐命令验收，不清理其他命令")
    args = ap.parse_args()

    census_path = os.path.join(
        _ROOT, "apps", args.app, "census", "raw_ops.json")
    census = (json.load(open(census_path, encoding="utf-8"))
              if os.path.isfile(census_path) else {})
    contract = app_contract.load(args.app)
    adapter = app_contract.load_adapter(args.app, contract)
    if adapter and hasattr(adapter, "check_build"):
        adapter.check_build(census)

    atlas_dir = os.path.join(_ROOT, "apps", args.app, "atlas")
    out_root = os.path.join(_ROOT, "apps", args.app, "commands")
    if args.only:
        filename = f"{args.only}.json"
        if not os.path.isfile(os.path.join(atlas_dir, filename)):
            raise SystemExit(f"[build] 找不到命令 atlas：{filename}")
        cells = [filename]
    else:
        cells = sorted(f for f in os.listdir(atlas_dir) if f.endswith(".json"))
    if not cells:
        raise SystemExit(f"{atlas_dir} 里没有格子 JSON")
    command_sources = {}
    for filename in cells:
        cell = json.load(open(os.path.join(atlas_dir, filename),
                              encoding="utf-8"))
        command_sources.setdefault(cell["command"], []).append(filename)
    duplicates = {
        command: sources for command, sources in command_sources.items()
        if len(sources) > 1
    }
    if duplicates:
        detail = "; ".join(
            f"{command}: {', '.join(sources)}"
            for command, sources in sorted(duplicates.items()))
        raise SystemExit(
            "[build] 多个 atlas 格子生成同一命令，拒绝静默覆盖：" + detail)
    equivalents_path = os.path.join(_ROOT, "apps", args.app, "equivalences.json")
    equivalents = (json.load(open(equivalents_path, encoding="utf-8"))
                   if os.path.isfile(equivalents_path) else {})
    merged_refs = set(equivalents)
    generated = []
    for f in cells:
        cell = json.load(open(os.path.join(atlas_dir, f), encoding="utf-8"))
        ref = cell.get("census_ref") or {}
        ref_key = f"{ref.get('channel')}:{ref.get('symbol')}"
        if ref_key in merged_refs:
            canonical = equivalents[ref_key]["canonical_command"]
            if cell["command"] != canonical:
                print(f"[build] skip {cell['command']}：等价能力已合并到 {canonical}")
                continue
        spec = render_spec(cell, app=args.app, contract=contract)
        cmd_dir = os.path.join(out_root, cell["command"])
        os.makedirs(cmd_dir, exist_ok=True)
        json.dump(spec, open(os.path.join(cmd_dir, "spec.json"), "w"),
                  indent=1, ensure_ascii=False)
        open(os.path.join(cmd_dir, "impl.py"), "w").write(render_impl(cell))
        print(f"[build] {cell['command']}  <-  {f}")
        generated.append(cell["command"])
        selector_args = contract["build"].get("selector_args", {})
        if (cell["binding"]["kind"] in selector_args
                and "-set-" in cell["command"]):
            getter_spec, getter_impl = render_getter(
                cell, spec, selector_args)
            getter = getter_spec["command"]
            getter_dir = os.path.join(out_root, getter)
            os.makedirs(getter_dir, exist_ok=True)
            json.dump(getter_spec, open(os.path.join(getter_dir, "spec.json"), "w"),
                      indent=1, ensure_ascii=False)
            open(os.path.join(getter_dir, "impl.py"), "w").write(getter_impl)
            print(f"[build] {getter}  <-  getter for {cell['command']}")
            generated.append(getter)
    # 构建目录必须精确反映本次 atlas，不能让已经删除或合并的旧命令继续存活。
    if not args.only:
        for name in sorted(set(os.listdir(out_root)) - set(generated)):
            stale = os.path.join(out_root, name)
            if os.path.isdir(stale):
                import shutil
                shutil.rmtree(stale)
                print(f"[build] remove stale {name}")
    print(f"[build] {args.app}: {len(generated)} 条命令已生成到 {out_root}")


if __name__ == "__main__":
    main()
