# Stage 1 应用适配编写 Prompt（软件无关模板）

> 用法：替换 {软件名} 等占位符，整段交给 coding agent 执行。
> 前置：该软件的 S1 普查已完成（`apps/{软件名}/census/raw_ops.json` 存在）。
> 本 prompt 只产出应用目录内的适配文件；过滤、命令实现、文档和发布由后续阶段控制。

---

## 任务

为 {软件名} 写三个应用自有产物：

1. 引擎适配层 `apps/{软件名}/engine.py`；
2. 静态探针 `apps/{软件名}/atlas_gen.py`，运行后把命令候选写到
   `apps/{软件名}/atlas/*.json`；
3. 应用契约 `apps/{软件名}/axis-app.json`，声明构建参数、验证并行上限和
   该应用发布时确实需要携带的文件。

不要运行后续过滤、批量命令实现、文档生成或冻结发布；Python 主流程会在
验收这三个产物后继续调度。

## 开工前先读（不许跳过）

1. `AXIS/METHOD.md` §S0、§S2、§S4、§S7——设计动机。
2. `AXIS/axis/proptemplate.py`——属性写命令与 getter 的模板绑定契约。
3. `AXIS/axis/build.py` 的 docstring——格子 JSON 契约。
4. `AXIS/axis/app_contract.py`——应用契约的结构和路径边界。
5. `AXIS/apps/{软件名}/census/raw_ops.json`——命令只能绑定这里面的符号。

## 架构（不要改 axis/ 下的任何文件）

```
atlas/*.json  --build.py-->  commands/<cmd>/{spec.json,impl.py}  --verify.py-->  build/report.json
              格子是数据        impl.py 是模板薄壳（只含 BINDING）      六门全过才进 freeze
```

属性写命令的 impl.py 调 `proptemplate.run(args, BINDING, engine)`，getter 调
`proptemplate.get(args, BINDING, engine)`。proptemplate 回调你的 engine.py
三个属性访问函数。应用专用代码只能写在 `apps/{软件名}/` 下；若需要额外的
运行时代码，可以新增文件，并在 `axis-app.json` 的
`release.runtime_files` 中逐个声明。

## axis-app.json 契约

```json
{
  "schema_version": 1,
  "adapter": null,
  "build": {
    "file_arg_help": "Artifact file path",
    "selector_args": {}
  },
  "verification": {"max_parallel_jobs": 1},
  "documentation": {"example_file": "demo.out"},
  "release": {
    "runtime_files": ["engine.py"],
    "axis_modules": [
      "__init__.py", "kernel.py", "cli.py", "proptemplate.py",
      "actiontemplate.py", "transformtemplate.py"
    ]
  }
}
```

- `selector_args` 按实际 binding kind 声明统一的对象定位参数；没有属性绑定时
  可以为空。命令自己的其他参数保存在 atlas 的 `binding.cli_args` 中。
- `runtime_files` 只列这个应用运行命令真正需要的应用文件。
- `axis_modules` 只列发布 CLI 所需的通用模块，不得加入其他应用的代码。
- 确有通用脚本无法表达的验证或发布检查时，才设置 `adapter`；该文件也必须
  位于 `apps/{软件名}/` 下。

## engine.py 契约

```python
def get_raw(args, binding)      # 返回当前原始值（标量/列表）；选择器未命中
                                #   抛 AxisError("TARGET_NOT_FOUND", ...)
def set_raw(args, binding, raw) # 写原始值并保存产物文件
def describe_target(args, binding)  # 目标的人类可读描述（str）
def make_demo_doc(path, text=None, table_cells=None)  # cli 内置命令要调它
def observe(path)               # 返回产物结构化状态 JSON（cli 内置命令要调它）
```

- `AxisError` 从 `axis.kernel` import；code 必须 ∈ spec.errors（build.py 已把
  `TARGET_NOT_FOUND/INPUT_NOT_FOUND/ENGINE_UNAVAILABLE/OPEN_FAILED` 写进每条
  命令的 errors，你抛错只用这几个码，除非格子里声明了 errors_extra）。
- 值的归一化不是你的活：get_raw 返回引擎原始值，proptemplate 负责换算成
  命令层可比值（bool 阈值 / enum 名字 / color 大写 hex / vec3 列表）。
- demo.expect 必须等于 proptemplate 归一化后的形式（bool→true/false；
  color→"#FF8800" 大写；enum→names 表里的名字或省略 names 时的原始字符串；
  vec3→浮点列表）。浮点 demo 值选可精确表示的（0.5、80.0），避免 == 抖动。
- 绑定种类（binding.kind）自带选择器参数：span_prop(match,all) /
  para_prop(para,match) / named_prop(name) / global_prop(无)。你的 engine
  按 kind 解释 args。kind 语义由你定义并实现——它是你 engine 内部的约定，
  但同一 kind 的所有格子必须行为一致。
- 结构属性（如 LO 的 LineSpacing 这类 struct）可以走"自定义 path 约定"：
  get_raw/set_raw 里特判 binding.path，返回标量——把复杂度关在 engine 里，
  格子保持简单。

## atlas 格子契约（逐格一个 JSON，字段见 build.py docstring）

- 布尔参数只有一种外部写法：`--参数名 true` 或 `--参数名 false`。不得生成
  单独出现的布尔开关（例如裸 `--force`）。只有 `--help`、`--schema`、
  `--recipe` 这三个界面控制开关不带值。覆盖已有文件统一使用布尔参数
  `--force true`；软件领域里若另有同名的数值概念，必须改用不会混淆的名称，
  例如 `--force-value 20` 或 `--plotter-force 20`。

- `binding.path` 必须能在 census 里追溯到：`census_ref.channel` 指进
  raw_ops.json 的 channels 树、`census_ref.symbol` 是其中的符号名。
  verify 的幻觉门会真的去核——**选格子前先查 census，只挑真实存在的符号**。
- 每格配 demo：make-demo 能造出的文件 + 一组参数 + 归一化后的期望值。
  demo 就是 recipe 门的测试用例，别造你 make_demo_doc 造不出来的场景。
- 改名等动作能力使用 `verb` 格子和 `actiontemplate`，不得硬塞进属性模板。
- 只读/幻影符号不要做（S1 已证明它们存在；你选的每格先手动探针验一次：
  能读、能写、读回一致）。
- 格子数量 3–12 个，证明流水线即可，不追求覆盖率（全矩阵是后续里程碑）。

## 本阶段验收（全部跑通才算完）

1. `axis-app.json` 能被 `axis/app_contract.py` 读取；
2. `engine.py` 和 `atlas_gen.py` 都存在，且应用声明的解释器能启动探针；
3. 探针至少产生一份结构合格、能追溯到普查结果的 atlas JSON。

## 铁律

- 不改 `axis/` 或其他 apps 的任何文件；缺能力（比如
  新的 value type）就回报阻塞，不许绕；
- 每格必须真实过门，FAIL 不许藏——修不好就如实汇报哪门卡了、证据是什么；
- engine.py 里不许硬编码 demo 特判（探测 args 内容走捷径 = 造假）；
- 不联网；只写 `apps/{软件名}/` 下的文件。
