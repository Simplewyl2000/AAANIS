"""普查适配层的机械验收。

存在的理由：`prompts/census_adapter.md` 的验收标准过去是让模型自己判断
"我跑通了吗"，没有独立的门。结果一个软件里 492 个对象方法被枚举出来、
只报了个数量、从没变成命令，而普查却算"验收通过"。

这个脚本检查的东西模型骗不过去：类型名是不是真填了、递归有没有展开、
边界账等式成不成立。哪项不过就打印具体是哪条记录、缺哪个字段，
这段输出可以原样贴回生成提示词让模型改。

用法：
    python3 axis/conformance.py --app <软件>
    python3 axis/conformance.py --app <软件> --deep    # 连重跑确定性一起查
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 类型名写成这些等于没写
EMPTY_TYPES = {"", "unknown", "none", "null", "?", "any", "object", "-"}

MEMBER_FIELDS = ("object", "member", "kind", "params", "returns", "writes",
                 "universal", "reached_from")
MIN_DEPTH = 2


class Check:
    """一项检查的结果。"""

    def __init__(self, name):
        self.name = name
        self.passed = True
        self.notes = []
        self.failures = []

    def note(self, text):
        self.notes.append(text)

    def fail(self, text):
        self.passed = False
        self.failures.append(text)


def census_path(app):
    return os.path.join(ROOT, "apps", app, "census", "raw_ops.json")


def load_census(app):
    path = census_path(app)
    if not os.path.isfile(path):
        raise SystemExit(f"找不到 {path}。先跑 python3 axis/census.py --app {app}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def c1_file_readable(census):
    check = Check("C1  普查产物可读、顶层字段齐全")
    for key in ("app", "channels"):
        if key not in census:
            check.fail(f"顶层缺 {key!r} 字段")
    if "unavailable" not in census:
        check.note("没有 unavailable 字段。找不到等价物的通道应该在这里"
                   "列出并附原因，空着也要给个空数组")
    channels = census.get("channels", {})
    check.note(f"通道：{', '.join(sorted(channels))}")
    return check


def count_legacy_members(node, found=None, depth=0):
    """在旧格式的普查产物里递归找"被数了个数的成员"。

    每个软件的通道结构都不一样，所以不按固定路径找，而是扫描整棵树里
    所有叫 methods / properties / attributes 的列表，把长度加起来。
    这个数字的用途是让报错具体：告诉模型"你手上明明有这么多东西"。
    """
    if found is None:
        found = {}
    if depth > 8:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("methods", "properties", "attributes", "members") \
                    and isinstance(value, list):
                found[key] = found.get(key, 0) + len(value)
            else:
                count_legacy_members(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:5000]:
            count_legacy_members(item, found, depth + 1)
    return found


def c2_members_channel_present(census):
    """最关键的一项：有没有逐条落盘的成员记录。"""
    check = Check("C2  成员普查通道存在且逐条落盘")
    channels = census.get("channels", {})
    members = channels.get("members")

    if members is None:
        found = count_legacy_members(channels)
        detail = "、".join(f"{key} {value} 个"
                          for key, value in sorted(found.items())) or "没数到"
        check.fail(
            "没有 channels.members 通道。这是旧格式的普查产物。\n"
            f"      实测：旧通道里能数出 {detail}。\n"
            "      它们只是被数了个数或只留了名字，没有逐条落盘成带签名的\n"
            "      记录，所以下游拿不到参数类型、拿不到返回类型，"
            "无法生成命令。\n"
            "      按 prompts/census_adapter.md 的 §E-1 补 channels.members。")
        return check, []

    if not isinstance(members, dict):
        check.fail("channels.members 不是一个对象")
        return check, []

    records = members.get("records")
    if not isinstance(records, list):
        check.fail("channels.members.records 不存在或不是列表")
        return check, []
    if not records:
        check.fail("channels.members.records 是空的")
        return check, []

    check.note(f"成员记录 {len(records)} 条")
    return check, records


def c3_member_fields(records):
    """字段完整性 + 类型名真实性。这一项挡住"丢掉类型信息"。"""
    check = Check("C3  每条成员记录字段完整、类型名真实")
    missing_field = []
    empty_type = []
    bad_returns = []
    bad_kind = []

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            missing_field.append((index, "整条不是对象"))
            continue

        absent = [key for key in MEMBER_FIELDS if key not in record]
        if absent:
            missing_field.append((index, f"缺 {absent}"))
            continue

        if record["kind"] not in ("property", "method"):
            bad_kind.append((index, record["kind"]))

        params = record["params"]
        if isinstance(params, list):
            for param in params:
                if not isinstance(param, dict):
                    empty_type.append((index, "参数不是对象"))
                elif str(param.get("type", "")).strip().lower() in EMPTY_TYPES:
                    empty_type.append(
                        (index, f"{record['object']}::{record['member']} 的参数 "
                                f"{param.get('name')!r} 类型是 "
                                f"{param.get('type')!r}"))
        else:
            missing_field.append((index, "params 不是列表"))

        returns = record["returns"]
        if not isinstance(returns, dict) or "type" not in returns \
                or "is_object" not in returns:
            bad_returns.append(
                (index, f"{record['object']}::{record['member']} 的 returns "
                        "缺 type 或 is_object"))
        elif str(returns["type"]).strip().lower() in EMPTY_TYPES:
            empty_type.append(
                (index, f"{record['object']}::{record['member']} 的返回类型是 "
                        f"{returns['type']!r}"))

    def report(label, items, why):
        if not items:
            return
        check.fail(f"{len(items)} 条{label}。{why}")
        for index, detail in items[:8]:
            check.failures.append(f"        第 {index} 条：{detail}")
        if len(items) > 8:
            check.failures.append(f"        …还有 {len(items) - 8} 条")

    report("记录字段不全", missing_field,
           f"必需字段是 {MEMBER_FIELDS}")
    report("记录的 kind 不合法", bad_kind, "只能是 property 或 method")
    report("记录的 returns 结构不对", bad_returns,
           "returns 必须同时有 type 和 is_object，is_object 决定要不要递归展开")
    report("记录的类型名等于没填", empty_type,
           "类型名必须是软件自报的真实类型。丢了类型，下游只能靠成员名猜"
           "它能不能调用，这正是旧版本漏掉几百个方法的原因")

    if check.passed:
        check.note("全部记录字段完整，类型名非空")
    return check


def c4_recursion_depth(members, records):
    """递归有没有真的展开。挡住"只列根对象一层"。"""
    check = Check(f"C4  递归展开深度 ≥ {MIN_DEPTH} 层")

    depths = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        path = str(record.get("reached_from", ""))
        depths[path.count(".")] = depths.get(path.count("."), 0) + 1
    observed = max(depths) if depths else 0
    declared_depth = members.get("max_depth")

    check.note(f"各层记录数：{dict(sorted(depths.items()))}")
    if declared_depth is not None and declared_depth != observed:
        check.fail(
            f"自报 max_depth={declared_depth}，但从 reached_from 实算出来是 "
            f"{observed}。自报的数字要和记录对得上")

    if observed < MIN_DEPTH:
        check.fail(
            f"实际只展开了 {observed} 层。表格单元格这类东西通常在"
            "文档→表格集合→表格→行集合→单元格的第四层，只展开一层"
            "就永远看不到 setString 这种成员。\n"
            "      按 prompts/census_adapter.md 的 §E-2：returns.is_object "
            "为真的成员，要真的拿到对象实例再继续枚举。")

    object_returning = [
        record for record in records
        if isinstance(record, dict)
        and isinstance(record.get("returns"), dict)
        and record["returns"].get("is_object") is True]
    if not object_returning:
        check.fail(
            "没有任何一条记录的 returns.is_object 为真。要么这个软件真的"
            "没有返回对象的成员（极少见），要么 is_object 没如实填——"
            "后者会让递归展开完全失效")
    else:
        check.note(f"声称返回对象的成员 {len(object_returning)} 条")
    return check


def c5_boundary_conserved(members, records):
    """边界账等式。挡住"展不开的悄悄消失"。"""
    check = Check("C5  边界账等式成立（展不开的没有静默消失）")

    boundary = members.get("boundary")
    if boundary is None:
        check.fail(
            "没有 channels.members.boundary。声称返回对象但实际拿不到实例的"
            "成员必须记在这里并附原因，否则无法证明没有东西悄悄丢掉")
        return check
    if not isinstance(boundary, list):
        check.fail("channels.members.boundary 不是列表")
        return check

    for index, item in enumerate(boundary[:200]):
        if not isinstance(item, dict) or not str(item.get("reason", "")).strip():
            check.fail(f"边界账第 {index} 条没有写 reason。每条都要说清"
                       "为什么拿不到这个对象")
            break

    expanded_types = {record["object"] for record in records
                      if isinstance(record, dict) and "object" in record}
    declared_types = {record["returns"]["type"] for record in records
                      if isinstance(record, dict)
                      and isinstance(record.get("returns"), dict)
                      and record["returns"].get("is_object") is True}
    unreached = declared_types - expanded_types
    boundary_types = {str(item.get("declared_type", "")) for item in boundary
                      if isinstance(item, dict)}

    check.note(f"展开到的对象类型 {len(expanded_types)} 种，"
               f"声称会返回的对象类型 {len(declared_types)} 种，"
               f"其中没展开的 {len(unreached)} 种，边界账 {len(boundary)} 条")

    unaccounted = unreached - boundary_types
    if unaccounted:
        check.fail(
            f"有 {len(unaccounted)} 种对象类型既没被展开、也没进边界账，"
            f"例如 {sorted(unaccounted)[:5]}。\n"
            "      等式必须成立：展开成功的类型数 + 边界账条数 = 声称会返回"
            "对象的类型总数。\n"
            "      不成立说明有东西丢了，而丢掉的正是下游拿不到的那些零件。")

    for key in ("expanded_types", "declared_object_types", "count"):
        if key in members and isinstance(members[key], int):
            actual = {"expanded_types": len(expanded_types),
                      "declared_object_types": len(declared_types),
                      "count": len(records)}[key]
            if members[key] != actual:
                check.fail(f"自报 {key}={members[key]}，实算是 {actual}，"
                           "对不上")
    return check


def c6_determinism(app, census):
    """重跑一次普查，产物必须逐字节一致。贵，只在 --deep 时跑。"""
    check = Check("C6  重跑确定（同一台机器两次普查产物一致）")
    path = census_path(app)
    with open(path, "rb") as handle:
        before = hashlib.sha256(handle.read()).hexdigest()

    backup = path + ".conformance_backup"
    os.replace(path, backup)
    try:
        completed = subprocess.run(
            [sys.executable, os.path.join(HERE, "census.py"), "--app", app],
            cwd=ROOT, capture_output=True, text=True, timeout=3600)
        if completed.returncode != 0:
            check.fail(f"重跑普查失败，退出码 {completed.returncode}\n"
                       f"      {completed.stderr[-800:]}")
            return check
        with open(path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        if before != after:
            check.fail(
                "两次普查产物不一致。普查必须是确定的——输出前把所有集合"
                "排序，不要带时间戳、临时路径、内存地址、迭代顺序不定的字典")
        else:
            check.note(f"两次一致，指纹 {before[:16]}")
    finally:
        if os.path.isfile(backup) and not os.path.isfile(path):
            os.replace(backup, path)
        elif os.path.isfile(backup):
            os.remove(backup)
    return check


def run(app, deep=False):
    census = load_census(app)
    checks = [c1_file_readable(census)]

    c2, records = c2_members_channel_present(census)
    checks.append(c2)
    if records:
        members = census["channels"]["members"]
        checks.append(c3_member_fields(records))
        checks.append(c4_recursion_depth(members, records))
        checks.append(c5_boundary_conserved(members, records))
    if deep:
        checks.append(c6_determinism(app, census))

    print(f"\n普查适配层验收：{app}")
    print("=" * 72)
    for check in checks:
        mark = "通过" if check.passed else "不通过"
        print(f"\n[{mark}] {check.name}")
        for note in check.notes:
            print(f"      {note}")
        for failure in check.failures:
            print(f"      {failure}" if failure.startswith(" ")
                  else f"      × {failure}")

    failed = [check for check in checks if not check.passed]
    print("\n" + "=" * 72)
    if failed:
        print(f"{len(failed)}/{len(checks)} 项不通过：")
        for check in failed:
            print(f"  · {check.name}")
        print("\n上面的输出可以原样贴回 prompts/census_adapter.md 的生成流程，"
              "让模型照着改。不要放宽标准——这些检查每一条都对应一个"
              "真实发生过的漏项。")
        return 1
    print(f"全部 {len(checks)} 项通过。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="普查适配层机械验收")
    parser.add_argument("--app", required=True)
    parser.add_argument("--deep", action="store_true",
                        help="连重跑确定性一起查（会重新跑一次普查，很慢）")
    options = parser.parse_args()
    raise SystemExit(run(options.app, deep=options.deep))


if __name__ == "__main__":
    main()
