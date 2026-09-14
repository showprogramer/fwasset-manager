# TASK-20260914-prototype-undo-scope：原型撤销范围收窄

状态：进行中

## 目标与规则

### 目标

按父规格 [TASK-20260903-crud-write-semantics](TASK-20260903-crud-write-semantics.md) 的 D10 与 D10.3，让原型 `specs/design/prototypes/firmware-crud-prototype.html` 只对删除类操作提供提示条撤销，其余写操作提交后立即生效、靠反向操作纠正；同步其任务文件与测试脚本，使原型、文档、测试三者不再互相矛盾。

本子 TASK 只动原型、原型测试与原型任务文件文档，不改 `src/fwasset/`。正式撤销契约（隔离清单、CAS 冲突、窗口清理）由实现子 TASK 按 D10.1a–D10.1c 落地。

### 现状（编写本规格时的只读审计）

- 父规格定稿时原型已改为 `apply(mutate, message, detail = "", undoable = false)`：五个删除类调用点传 `true`，其余十个不传；归档任务文件的交互通则与验收标准也已同步。
- 15 个 `apply(...)` 调用点与 D10.3 逐点一致；仓库内无「所有写操作可撤销」的残留表述。
- 测试现状：`node specs\design\prototypes\firmware-crud-prototype.test.js` 实测输出 `prototype data interactions: 122 assertions passed`。该脚本只截取脚本的 dataLayer（`const ROOT` 到 `//   3. 工具` 分节之前）求值，`apply`（第 4 节）与 `toast`（第 6 节）都不在其中，因此这 122 项断言完全不覆盖撤销矩阵，`undoable` 收窄目前没有回归保护。
- 原型尚未实现 D7.5 待补齐项（D10.1 第六项标注为「新增」）。

### 非目标

- 不改 `src/fwasset/` 任何代码，不接入正式 CRUD service。
- 不重开 D10 的范围：不增删可撤销操作，不给新增、重命名、更新、设为默认、登记借用、恢复副本加撤销。
- 不在原型里实现隔离区、回收站或 preimage/postimage 的持久化，原型以内存快照等价表达。
- 不改 R8 删除零改写语义与 `unsupported_semantic_change` 阻断，不改 UI 术语映射（差异 #6），不重开原型已定稿的其他产品规则。

### 可撤销：删除类六项

「提示文案锚点」是该调用点第 2 实参（提示文案）的可辨识片段，在主脚本内唯一，用作静态断言的键。

| # | 操作 | 逆操作 | 提示文案锚点 | 原型 |
| --- | --- | --- | --- | --- |
| 1 | 删除程序 | 隔离区目录移回原路径 | `「${a.name}」已放进回收站` | 已有 |
| 2 | 删除型号 | 同上 | `型号「${model.name}」已删除` | 已有 |
| 3 | 删除方案 | 同上 | `方案「${scheme.name}」已放进回收站` | 已有 |
| 4 | 删除备用副本 | 同上 | `已删除备用副本` | 已有 |
| 5 | 解除借用 | 恢复 `型号配置.toml` 条目（不涉及任何文件移动） | `${b.type}不再用 ${modelName(b.fromModel)} 的程序` | 已有 |
| 6 | 删除待补齐项（D7.5） | 移回原路径，撤销后重新进入 `scan_incomplete_imports` | —（原型无此调用点） | 未实现，落地时必须带撤销 |

提示条带「撤销」按钮。原型的可撤销提示条展示 5 秒，与 D10.1a 的撤销窗口一致；非删除类提示条 3 秒且无按钮。

### 不可撤销：其余十个 `apply` 调用

| 操作 | 提示文案锚点 | 用户的替代路径 |
| --- | --- | --- |
| 重命名程序 | `已改名为「${next}」` | 再改回原名 |
| 设为默认 | `「${a.name}」已设为本型号默认` | 重新设为原来那个 |
| 新建型号 | `已新建型号「${modelNameInput.trim()}」` | 删除刚建的对象（本身可撤销） |
| 重命名型号 | `型号已重命名为「${value}」` | 再改回原名 |
| 新建方案 | `已新建方案「${schemeNameInput.trim()}」` | 删除刚建的对象（本身可撤销） |
| 重命名方案 | `方案已重命名为「${value}」` | 再改回原名 |
| 新增程序 | `「${name}」已加进程序库` | 删除刚建的对象（本身可撤销） |
| 登记借用 | `${type}现在用 ${modelName(fromModel)} 的程序` | 解除借用（本身可撤销） |
| 更新程序（含仅改厂商） | `「${changes.name}」已更新` | 恢复备用副本，或再更新一次 |
| 恢复备用副本 | `已恢复备用副本` | 反向再交换一次 |

静态断言按「第 2 实参源码文本 → 第 4 实参」建 15 条映射：上表十处缺第 4 实参（即缺省 `false`），六项删除类中已在原型落地的五处为 `true`。文案锚点为主脚本内唯一，可直接作键，不依赖行号。

**同为不可撤销，但不是 `apply` 调用点**：厂商名单增删（D6.1，保存失败保留原 `config.toml`，用户可再次增删候选项）、legacy 模块叶子归一（D0.3）与工作区布局迁移（D9.3，一次性操作，操作前需确认）。

### `apply` API 形态

```js
apply(mutate, message, detail = "", undoable = false)
```

- `undoable` 默认 `false`，只有删除类操作传 `true`。
- `undoable === true`：在 `mutate()` 前取快照，提示条带「撤销」按钮，撤销即恢复快照；否则不取快照，提示条只报告结果、不带按钮。
- `applyPicked(...)` 是导入来源解析的独立函数，其第 4 个参数与撤销无关，不计入 15 个 `apply` 调用点。
- 正式实现不得复用原型的整库快照：删除类撤销走 D10.1a–D10.1c 的隔离清单与 preimage/postimage 契约。

### 父规格依赖与不变量

依赖父规格 D10（D10.1、D10.1a、D10.1b、D10.1c、D10.2、D10.3）。本子 TASK 只同步原型与文档，不定稿正式契约。以下不变量父规格已冻结，原型与文档不得与之矛盾：

1. 只有删除类六项可撤销；其余写操作提交后即生效，提示条不带「撤销」按钮。
2. 解除借用的撤销必须能判冲突：当前文件不等于 postimage → `undo_conflict`；严格读取失败时不执行 clear；5 秒窗口届满丢弃 undo 依据。原型以整库快照等价表达，正式实现必须用 preimage/postimage。
3. 删除最后一个变体后撤销：隔离清单须记录本次自动删除的空模块容器，撤销时先重建容器再移回 asset；父路径已被其他身份占用 → `undo_conflict`，隔离内容保留。
4. 移回前核验隔离内容 manifest 与清单状态，不一致 → `undo_conflict`。
5. 待补齐项不是 `FirmwareAsset`：不做 R8 asset 反查，但仍过路径守卫与状态 CAS。
6. 父规格验收要求不留「文档说只删除可撤销、原型仍全部可撤销」的矛盾，原型、原型任务文件与测试必须一致。

第 3、4 条属正式契约，原型无对应结构，不得以原型代替其验证。

## 验收清单

- [ ] 静态断言：主脚本内 `apply(` 调用点（排除 `function apply` 定义本身）恰为 15 个；按第 2 实参源码文本建映射后，第 4 实参为 `true` 的恰是「可撤销」表中已落地的五处，其余十处缺省。
- [ ] 静态断言：存在 `function apply(mutate, message, detail = "", undoable = false)`、`const snap = undoable ? snapshot() : null;`、`toast("ok", message, detail, snap ? () => restore(snap) : null);`、`undo ? 5000 : 3000` 四处分支源码。
- [ ] 行为断言（最小 stub，不需要真实 DOM）：缺省调用下 `structuredClone` 计数为 0、定时器 3000ms、toast 节点 `children` 为空；`undoable = true` 下计数为 5（`snapshot()` 对五个集合各克隆一次）、定时器 5000ms、toast 节点与其按钮不是同一对象且该节点 `children` 中恰有一个 `textContent === "撤销"` 的按钮；触发该按钮后数据回到调用前、`undoLast` 清空、追加「已撤销上一步操作」提示。
- [ ] 归档任务文件 `specs/archive/TASK-20260806-independent-crud-web-prototype.md` 的交互通则与验收标准与本规格一致。
- [ ] 仓库内除父规格 D10 的历史说明外，没有「所有写操作可撤销」类表述。
- [ ] 厂商名单增删（D6.1）不带撤销。
- [ ] 原型若落地 D7.5 待补齐项，其删除带撤销并在撤销后重新进入 `scan_incomplete_imports` 语义。

## 验证

### 自动化方案（Windows，仅 `node:fs` + `node:assert/strict`，无新增依赖）

沿用现有测试的三项手法：读 HTML、取 `<script>` 文本、以分节注释作切片锚点、`new Function` 求值。

**1. 静态断言（对 `<script>` 文本）**

- 计数：`script.match(/\bapply\(/g)` 的命中数减一（定义本身）等于 15。`\bapply\(` 不匹配 `applyPicked(`，无需额外排除。
- 逐点映射：扫描每个 `apply(` 调用点，从 `(` 起做**跳过字符串字面量与注释的括号配平**，定位匹配的 `)`，再按**顶层逗号**切分实参（不能用「是否含 `, true)`」判断，因为 mutate 体里也可能出现 `true`）。断言实参个数为 2、3 或 4（重命名型号与恢复备用副本两处省略了 `detail`，直接落到默认 `undoable`），并以第 2 实参源码文本为键、第 4 实参为值，与上文两张表的 15 个锚点逐条比对。
- 分支源码：断言上述四条分支语句存在。

**2. 行为断言（最小 stub 与等效执行边界）**

现有 dataLayer 切片止于第 3 节之前，`apply`（第 4 节）与 `toast`（第 6 节）都不可达，故另取两段并注入宿主对象：

- 切片 A（第 4 节）：`script.slice(script.indexOf("const snapshot = () => ({"), script.indexOf("/* ============================================================\n   5. 渲染"))`。
- 切片 B（第 6 节）：`script.slice(script.indexOf("let undoLast = null;"), script.indexOf("/* ============================================================\n   7. 模态"))`。
- 求值边界：`new Function("render","esc","$","document","structuredClone","setTimeout","state","assets","borrows","backups","MODELS","SCHEMES", A + B + "\nreturn { apply, getAssets: () => assets, getUndoLast: () => undoLast };")`。两段切片只引用这些自由名字，第 5 节 `render` 以参数顶替，因此不需要真实 DOM 或 jsdom。
- 最小 stub：元素工厂 `el()` **每次调用返回独立对象**，各自持有 `listeners` 与 `children`，含 `className` / `innerHTML` / `type` / `textContent` 属性与 `append(...children)`、`remove()`、`addEventListener(type, fn)`（记录进自身 `listeners`）；`document = { createElement: () => el() }`；`$ = sel => (sel === "#toasts" ? { append: node => appended.push(node) } : el())`；`esc = value => String(value)`；`setTimeout = (fn, ms) => (timers.push({ fn, ms }), timers.length)`（只记录不延时，由测试手动触发）；`structuredClone` 用计数包装以观测「是否取快照」；`render` 用计数器空函数。
  必须用工厂而非共用单个对象：`toast()` 会先用 `createElement` 建提示条节点、再用它建「撤销」按钮，共用同一对象会让按钮被 `append` 进自身，`children` / `textContent` / `listeners` 互相污染，断言全部失真。
- 断言：缺省调用 → `structuredClone` 计数 0、`timers[0].ms === 3000`、`appended` 里的 toast 节点 `children.length === 0`；`apply(fn, msg, "", true)` → 计数 5、`timers[0].ms === 5000`、该 toast 节点与其按钮**不是同一对象**、`children.length === 1` 且这个唯一子节点 `textContent === "撤销"`；手动调用该按钮的 `listeners.click` → `getAssets()` 深等于调用前、`getUndoLast() === null`、又追加一条「已撤销上一步操作」提示。
- 必须经返回值里的 getter 读状态：`restore` 会把 `assets` 重新赋值（`MODELS` / `SCHEMES` 是原地 `splice`），读测试外层的数组引用会漏掉 `assets` 的回滚。

该边界与上述断言已用一次性探针脚本在 Node 23.10.0 上实测通过（探针不入库）：15 个调用点、5/10 划分、两段切片求值，以及缺省与可撤销两个分支的克隆计数与定时器时长均按预期返回。

**3. 其余检查**

- 提取 `<script>` 后 `new Function(...)` 做语法检查。
- `git diff --check`。

### 人工（待执行）

浏览器打开 `specs/design/prototypes/firmware-crud-prototype.html`，按原型现有入口走完下表，确认提示条是否符合预期。

| 组 | 操作 | 入口 | 预期 |
| --- | --- | --- | --- |
| 不可撤销 | 重命名程序 | 详情铅笔或 `F2` | 提示条无「撤销」 |
| 不可撤销 | 设为默认 | 详情区 | 同上 |
| 不可撤销 | 新建型号 | 型号下拉「＋ 新建型号」 | 同上 |
| 不可撤销 | 重命名型号 | 右键型号 → 重命名 | 同上 |
| 不可撤销 | 新建方案 | 导航「＋ 新建定制方案」 | 同上 |
| 不可撤销 | 重命名方案 | 右键方案 → 重命名 | 同上 |
| 不可撤销 | 新增程序 | 主表「新增程序」→ 添加文件 | 同上 |
| 不可撤销 | 登记借用 | 同一新增对话框 → 选择别的型号已有程序 | 同上 |
| 不可撤销 | 更新程序 | 详情「更新程序」 | 同上 |
| 不可撤销 | 恢复备用副本 | 详情备用副本区「恢复」 | 同上 |
| 可撤销 | 删除程序 | 右键、`Del` 或详情底部弱化链接 | 提示条有「撤销」，点击后回到操作前 |
| 可撤销 | 删除型号 | 右键型号 → 删除 | 同上 |
| 可撤销 | 删除方案 | 右键方案 → 删除 | 同上 |
| 可撤销 | 删除备用副本 | 详情备用副本区「删除」 | 同上 |
| 可撤销 | 解除借用 | 详情「不再用别的型号的程序」 | 同上（撤销后借用登记恢复） |

### 文档

Review 不适用（不改产品运行代码）；CHANGELOG 不适用（原型不是用户可感知功能）；迁移说明不适用（无数据格式或路径变化）。

## 待决事项

- 本次按指示只新建本规格，未改动任何其他文件。父规格要求同步 `TASK-20260806-independent-crud-web-prototype.md`，该文件已含收窄后的交互通则与验收标准；是否需要复核性改动待确认。
- 原型测试脚本的静态与行为断言、以及原型落地 D7.5 待补齐项时的撤销，均属本子 TASK 的实现工作，需授权后执行。
