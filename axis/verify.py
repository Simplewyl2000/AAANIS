"""AXIS 命令外部验证栈。

每条写命令只执行动作并报告，不再自行读取前态、计算计划、读取后态。本脚本在
命令外部重造演示文件、运行命令，再通过 getter、observe 或引擎的只读测量接口
检查真实文件。结构、防幻觉、坏参数三道检查继续保留；旧 dry-run 检查已删除。
"""
import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import hashlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from axis import app_contract, kernel, runtime  # noqa: E402

RUN_TIMEOUT = 300


def cli(app_dir, argv, timeout=RUN_TIMEOUT):
    """跑一次与冻结产物等价的命令行调用，返回退出码和 JSON。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + app_dir
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "axis", "cli.py"),
                        "--app", app_dir] + argv,
                       capture_output=True, text=True, env=env, timeout=timeout)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {"status": "error", "code": "BAD_ENVELOPE",
               "message": r.stdout[-500:] + r.stderr[-500:]}
    return r.returncode, out


def load_engine(app, app_dir):
    """以独立模块名加载当前软件引擎，避免多软件模块名相撞。"""
    path = os.path.join(app_dir, "engine.py")
    spec = importlib.util.spec_from_file_location(f"axis_verify_engine_{app}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def census_symbols(app):
    """把 census 的嵌套通道摊平成 {通道路径: 符号集合}。

    人工确认的能力放在 census/attested.json，单独摊进 "attested" 通道。
    两个来源严格分开：raw_ops.json 只许普查脚本写、重跑整体覆盖；
    attested.json 由人维护、普查重跑不受影响。过去把人工符号混写进
    raw_ops.json 的自造通道里，普查一重跑就把 28 条命令的来源冲掉了。
    """
    path = os.path.join(_ROOT, "apps", app, "census", "raw_ops.json")
    d = json.load(open(path, encoding="utf-8"))
    out = {}

    def flatten(prefix, node):
        if isinstance(node, list):
            names = set()
            for it in node:
                if isinstance(it, str):
                    names.add(it)
                elif isinstance(it, dict):
                    names.add(
                        it.get("name") or it.get("cmd")
                        or it.get("service") or "")
            out[prefix] = {n for n in names if n}
        elif isinstance(node, dict):
            for k, v in node.items():
                flatten(f"{prefix}.{k}" if prefix else k, v)

    flatten("", d.get("channels", {}))

    attested = attested_symbols(app)
    if attested:
        out["attested"] = set(attested)
        smuggled = sorted(
            symbol for symbol in attested
            for pool in out.values() if symbol in pool and pool is not out["attested"])
        if smuggled:
            raise SystemExit(
                f"[verify] {app}: 以下人工确认的符号同时出现在自动普查产物里："
                f"{smuggled[:5]}\n"
                "        普查产物只许普查脚本写入。人工确认的能力放 attested.json，"
                "不许混进 raw_ops.json——混进去就分不清哪些能力是软件自报的。")
    return out


def attested_symbols(app):
    """读人工确认的能力清单。返回 {符号: 那条记录}。"""
    path = os.path.join(_ROOT, "apps", app, "census", "attested.json")
    if not os.path.isfile(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for entry in data.get("capabilities", []):
        symbol = entry.get("symbol")
        if not symbol:
            continue
        if not str(entry.get("reason", "")).strip():
            raise SystemExit(
                f"[verify] {app}: attested.json 里 {symbol!r} 没写 reason。"
                "每条人工确认的能力都必须说明为什么自动普查覆盖不到它。")
        out[symbol] = entry
    return out


def gate_structure(cmd_dir, spec, app_dir):
    missing = [k for k in ("command", "summary", "args", "errors", "demo")
               if k not in spec]
    if missing:
        return False, f"spec 缺字段 {missing}"
    for p in (_ROOT, app_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    from axis import kernel
    try:
        kernel.load_impl(cmd_dir)
    except Exception as e:
        return False, f"impl 加载失败：{e}"
    return True, "spec 齐全，单一 run() 可加载"


def gate_hallucination(spec, symbols):
    """命令引用的能力符号必须真实存在。

    两个来源都接受，但必须分得清：自动普查的产物，或者 attested.json 里
    人工确认并写明原因的。后者在报告里明确标出来，发布时分开报数。
    """
    ref = spec.get("census_ref")
    if not ref:
        return False, "缺 census_ref（命令必须可追溯到普查条目）"

    pool = symbols.get(ref["channel"], set())
    if ref["symbol"] in pool:
        if ref["channel"] == "attested":
            return True, (f"{ref['symbol']} 来自人工确认清单 attested.json，"
                          "不是软件自动普查的产物")
        return True, f"{ref['symbol']} ∈ {ref['channel']}"

    # 通道名对不上，但符号本身在人工确认清单里——按人工确认放行并标注。
    if ref["symbol"] in symbols.get("attested", set()):
        return True, (f"{ref['symbol']} 来自人工确认清单 attested.json"
                      f"（档案里记的通道 {ref['channel']!r} 已不存在，"
                      "应把 census_ref.channel 改成 \"attested\"）")

    return False, (f"{ref['symbol']} 不在 census 通道 {ref['channel']}"
                   f"（{len(pool)} 条）内，也不在人工确认清单里")


def values_match(cur, expect):
    """递归比较状态；浮点数允许千分之一相对误差。"""
    if isinstance(expect, bool) or isinstance(cur, bool):
        return cur == expect
    if isinstance(expect, (int, float)) and isinstance(cur, (int, float)):
        return abs(cur - expect) <= 1e-3 * max(1.0, abs(expect))
    if isinstance(expect, dict) and isinstance(cur, dict):
        return set(cur) == set(expect) and all(
            values_match(cur[k], expect[k]) for k in expect)
    if isinstance(expect, list) and isinstance(cur, list):
        return len(cur) == len(expect) and all(
            values_match(c, e) for c, e in zip(cur, expect))
    return cur == expect


def make_demo(app_dir, spec, demo):
    argv = ["make-demo", "--file", demo, "--force", "true",
            "--text", spec["demo"].get("text", "Demo text")]
    # Demo creation is a test precondition, so retry it once before failing the
    # command under test.
    for attempt in range(2):
        code, out = cli(app_dir, argv)
        if code == 0:
            break
    if code != 0:
        return False, f"make-demo 连续两次失败：{out.get('message')}"
    return True, "演示文件已创建"


def command_argv(spec, demo):
    return ([spec["command"], "--file", demo]
            + kernel.argument_tokens(spec["demo"]["args"]))


def getter_argv(getter, setter_spec, demo, contract):
    selectors = contract["build"].get("selector_args", {}).get(
        setter_spec["binding"]["kind"], {})
    return ([getter, "--file", demo]
            + kernel.argument_tokens(setter_spec["demo"]["args"], selectors))


def _content_digest(path, adapter=None):
    if adapter is not None and hasattr(adapter, "content_digest"):
        return adapter.content_digest(path)
    digest = hashlib.sha256()
    digest.update(open(path, "rb").read())
    return digest.hexdigest()


def check_verb(app, app_dir, spec, demo, run_report, before_digest=None,
               adapter=None):
    """用 observe 或动作报告检查动作命令的实际文件结果。"""
    code, observed = cli(app_dir, ["observe", "--file", demo])
    if code != 0:
        return False, f"observe 失败：{observed.get('message')}"
    state = observed["state"]
    expect = spec["demo"]["expect"]
    if expect.get("content_changed"):
        if before_digest is None or _content_digest(demo, adapter) == before_digest:
            return False, "保存重开后文件内容与执行前相同"
    if "state" in expect:
        actual = json.loads(json.dumps(state))
        actual.pop("file", None)
        if not values_match(actual, expect["state"]):
            return False, "重开文件后的结构化状态与动作探针冻结状态不一致"
    if "content_fingerprint" in expect:
        if _content_digest(demo, adapter) != expect["content_fingerprint"]:
            return False, "重开文件后的持久内容指纹与动作探针证据不一致"
    if "action_state" in expect:
        engine = load_engine(app, app_dir)
        actual = engine.action_state({"file": demo}, spec["binding"])["current"]
        wanted = json.loads(json.dumps(expect["action_state"]))
        actual = json.loads(json.dumps(actual))
        if adapter is not None and hasattr(adapter, "normalize_action_state"):
            actual, wanted = adapter.normalize_action_state(
                spec, actual, wanted)
        if not values_match(actual, wanted):
            return False, "重开文件后的算子状态与冻结探针状态不一致"
    if "output_exists" in expect:
        output = run_report.get("output")
        if not output or not os.path.isfile(output):
            return False, f"动作没有生成输出文件：{output!r}"
        return True, f"确认输出文件已生成：{output}"
    if adapter is not None and hasattr(adapter, "check_verb_expectation"):
        return adapter.check_verb_expectation(
            spec, state, run_report, expect, demo)
    return True, "observe confirmed the persisted command effect"


def external_state(app, app_dir, spec, demo, engine, contract):
    """在写命令之外读取当前状态，返回可与冻结期望比较的值。"""
    kind = spec["binding"]["kind"]
    getter = (spec.get("related") or {}).get("read_current")
    if getter:
        code, out = cli(
            app_dir, getter_argv(getter, spec, demo, contract))
        if code != 0:
            raise RuntimeError(f"getter {getter} 失败：{out.get('message')}")
        return out.get("current")
    args = {"file": demo}
    args.update(spec["demo"]["args"])
    if kind == "transform":
        return engine.measure(args)
    if kind == "processing":
        return engine.action_state(args, spec["binding"]).get("current")
    if kind in contract["build"].get("selector_args", {}):
        from axis import proptemplate
        return proptemplate.get(args, spec["binding"], engine)["current"]
    raise RuntimeError(f"没有为 {app}/{kind} 定义外部观测")


def gate_recipe(app, app_dir, spec, tmp, engine, contract, adapter):
    demo = os.path.join(tmp, "demo" + runtime.document_extension(app))
    ok, msg = make_demo(app_dir, spec, demo)
    if not ok:
        return False, msg, demo
    for setup in spec["demo"].get("setup_commands", []):
        argv = [part.replace("{file}", demo) for part in shlex.split(setup)]
        code, out = cli(app_dir, argv)
        if code != 0:
            return False, (f"动作前置命令失败：{setup}："
                           f"{out.get('message')}"), demo

    # getter 自己的配方：先用成对 setter 写值，再单独读取。
    if spec.get("paired_setter"):
        setter_dir = os.path.join(app_dir, "commands", spec["paired_setter"])
        setter_spec = json.load(open(os.path.join(setter_dir, "spec.json"),
                                     encoding="utf-8"))
        code, out = cli(app_dir, command_argv(setter_spec, demo))
        if code != 0:
            return False, f"配对 setter 失败：{out.get('message')}", demo
        code, got = cli(app_dir, getter_argv(
            spec["command"], setter_spec, demo, contract))
        if code != 0 or not values_match(got.get("current"), spec["demo"]["expect"]):
            return False, (f"getter 读到 {got.get('current')!r}，"
                           f"期望 {spec['demo']['expect']!r}"), demo
        return True, "先写后读，getter 返回当前真实值", demo

    demo_expect = spec["demo"].get("expect")
    before_digest = (
        _content_digest(demo, adapter)
        if isinstance(demo_expect, dict) and demo_expect.get("content_changed")
        else None)
    argv = command_argv(spec, demo)
    observation_digest = (
        _content_digest(demo, adapter)
        if spec["binding"]["kind"] == "observation" else None)
    code, run1 = cli(app_dir, argv)
    if code != 0 or run1.get("status") != "ok":
        return False, f"命令执行失败：{run1.get('code')} {run1.get('message')}", demo

    kind = spec["binding"]["kind"]
    if kind == "observation":
        expected = spec["demo"]["expect"]
        if not values_match(run1.get("current"), expected):
            return False, (f"只读观测值 {run1.get('current')!r}，"
                           f"期望 {expected!r}"), demo
        code, run2 = cli(app_dir, argv)
        if code != 0 or not values_match(run2.get("current"), expected):
            return False, "第二次只读观测不稳定", demo
        if _content_digest(demo, adapter) != observation_digest:
            return False, "只读观测改变了输入文件", demo
        return True, "只读观测与冻结期望一致，连续读取稳定", demo
    if kind == "verb":
        ok, detail = check_verb(
            app, app_dir, spec, demo, run1, before_digest=before_digest,
            adapter=adapter)
        if not ok:
            return False, detail, demo
        if spec["demo"].get("repeat_mode") == "fresh":
            os.remove(demo)
            ok, msg = make_demo(app_dir, spec, demo)
            if not ok:
                return False, f"第二份演示文件创建失败：{msg}", demo
            for setup in spec["demo"].get("setup_commands", []):
                setup_argv = [
                    part.replace("{file}", demo) for part in shlex.split(setup)]
                code, out = cli(app_dir, setup_argv)
                if code != 0:
                    return False, (f"第二份演示文件的前置命令失败："
                                   f"{out.get('message')}"), demo
        code, run2 = cli(app_dir, argv)
        if code != 0:
            return False, f"连续第二次执行失败：{run2.get('message')}", demo
        repeat = ("在两份等价输入上执行均未报错"
                  if spec["demo"].get("repeat_mode") == "fresh"
                  else "连续执行两次均未报错")
        return True, detail + f"；{repeat}", demo

    try:
        cur1 = external_state(app, app_dir, spec, demo, engine, contract)
    except Exception as e:
        return False, f"外部观测失败：{e}", demo
    expect = spec["demo"]["expect"]
    expected1 = expect["after1"] if kind == "transform" else expect
    if not values_match(cur1, expected1):
        return False, f"外部观测值 {cur1!r}，期望 {expected1!r}", demo

    code, run2 = cli(app_dir, argv)
    if code != 0:
        return False, f"连续第二次执行失败：{run2.get('message')}", demo
    cur2 = external_state(app, app_dir, spec, demo, engine, contract)
    expected2 = expect["after2"] if kind == "transform" else expected1
    if not values_match(cur2, expected2):
        return False, f"第二次外部观测值 {cur2!r}，期望 {expected2!r}", demo
    return True, "命令外部观测符合冻结期望，连续执行两次均未报错", demo


def gate_badargs(app_dir, spec, demo):
    code, _ = cli(app_dir, [spec["command"], "--file", demo, "--bogus", "1"])
    if code != 2:
        return False, f"未知参数退出码 {code}（应为 2）"
    required = [k for k, a in spec["args"].items()
                if a.get("required") and k != "file"]
    if required:
        argv = [spec["command"], "--file", demo]
        for k, v in spec["demo"]["args"].items():
            if k != required[0] and k in spec["args"]:
                argv += [f"--{k}", str(v)]
        code, _ = cli(app_dir, argv)
        if code != 2:
            return False, f"缺必填 --{required[0]} 退出码 {code}（应为 2）"
    return True, "未知参数和缺少必填参数均以退出码 2 拒绝"


def verify_one(app, app_dir, cmd_root, symbols, contract, adapter, name):
    cmd_dir = os.path.join(cmd_root, name)
    spec_path = os.path.join(cmd_dir, "spec.json")
    if not os.path.isfile(spec_path):
        return name, None
    spec = json.load(open(spec_path, encoding="utf-8"))
    gates = {}
    ok, msg = gate_structure(cmd_dir, spec, app_dir)
    gates["structure"] = {"pass": ok, "detail": msg}
    if ok:
        ok, msg = gate_hallucination(spec, symbols)
        gates["hallucination"] = {"pass": ok, "detail": msg}
        if ok:
            engine = load_engine(app, app_dir)
            with tempfile.TemporaryDirectory() as tmp:
                ok, msg, demo = gate_recipe(
                    app, app_dir, spec, tmp, engine, contract, adapter)
                gates["external_observation"] = {"pass": ok, "detail": msg}
                ok2, msg2 = gate_badargs(app_dir, spec, demo)
                gates["badargs"] = {"pass": ok2, "detail": msg2}
    return name, {"pass": all(g["pass"] for g in gates.values()), "gates": gates}


def main():
    ap = argparse.ArgumentParser(description="AXIS 命令外部验证栈")
    ap.add_argument("--app", required=True)
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行度：各命令在独立临时目录运行")
    args = ap.parse_args()
    app_dir = os.path.join(_ROOT, "apps", args.app)
    contract = app_contract.load(args.app)
    adapter = app_contract.load_adapter(args.app, contract)
    maximum = contract["verification"].get("max_parallel_jobs")
    if isinstance(maximum, int) and maximum > 0:
        args.jobs = min(args.jobs, maximum)
    census_path = os.path.join(app_dir, "census", "raw_ops.json")
    census = json.load(open(census_path, encoding="utf-8"))
    if adapter and hasattr(adapter, "check_verification"):
        adapter.check_verification(census)
    cmd_root = os.path.join(app_dir, "commands")
    symbols = census_symbols(args.app)
    names = [args.only] if args.only else sorted(os.listdir(cmd_root))

    report = {}
    if args.jobs <= 1:
        for name in names:
            name, result = verify_one(
                args.app, app_dir, cmd_root, symbols, contract, adapter, name)
            if result is not None:
                report[name] = result
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(
                verify_one, args.app, app_dir, cmd_root, symbols,
                contract, adapter, name): name for name in names}
            for future in as_completed(futures):
                name, result = future.result()
                if result is not None:
                    report[name] = result

    for name in sorted(report):
        result = report[name]
        mark = "PASS" if result["pass"] else "FAIL"
        print(f"[{mark}] {name}")
        for gate, detail in result["gates"].items():
            if not detail["pass"]:
                print(f"   ✗ {gate}: {detail['detail']}")

    selected_report = dict(report)
    out_dir = os.path.join(app_dir, "build")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "report.json")
    if args.only and os.path.isfile(out_path):
        old = json.load(open(out_path, encoding="utf-8"))
        old.update(report)
        report = old
    json.dump(report, open(out_path, "w"), indent=1, ensure_ascii=False)
    n_pass = sum(1 for result in report.values() if result["pass"])
    print(f"\n[verify] {args.app}: {n_pass}/{len(report)} 全门通过 -> {out_path}")
    # --only is a targeted repair gate.  Historical failures remain in the
    # cumulative report for visibility, but must not make an unrelated repaired
    # command fail its own gate.
    selected_pass = sum(
        1 for result in selected_report.values() if result["pass"])
    if selected_pass != len(selected_report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
