# 新建程序：范围选择、方案新建、使用其他型号的程序

- 状态：已完成
- 日期：2026-09-23
- 来源：用户决策——「新增程序应该多一栏 通用/定制的选择，定制的选择中可以新增定制方案，新增程序可以选择别的型号的已有程序」
- 关联：specs/archive/TASK-20260922-ui-orchestration.md（子任务 8，本文扩它的第 8 节）、specs/archive/TASK-20260922-create-asset-source-auto-detect.md（来源自动识别）
- 影响面：`ui_qt/entry_flows.py`、测试。方案列举复用既有 `direct_scheme_dir_names`，不新增 ViewModel API
- **不改核心层**：`create_asset`、`create_scheme`、`register_shared_module` 的签名、错误码、payload 一律不动

## 1. 背景

子任务 8 的新建程序对话框把范围写死「通用」，`scheme_name=""`。但服务层早就支持定制：

- `create_asset`（`asset_service.py:150-153`）接受 `scope` ∈ {通用, 定制}，`scope=定制` 时 `scheme_name` 必填，且方案目录必须已存在，否则 `invalid_target`。
- `create_scheme`（`model_scheme_service.py:193`）已实现，写 `方案配置.toml` 并 staging 原子提升。
- `register_shared_module`（`write_edit_service.py:435`）已实现，但 UI 只有右键入口 `_register_shared_source`（`workbench_window.py:1007`），**参数是 `ModuleVariant`，必须先有一行程序才能点**。空型号没有任何行，借用因此不可达。

三项都是接既有服务，零核心改动。

## 2. 本轮做

| # | 能力 | 服务入口 |
| --- | --- | --- |
| 1 | 新建程序多一栏「范围」：通用 / 定制；定制时选方案 | `create_asset(scope=..., scheme_name=...)` |
| 2 | 方案下拉旁「新建方案」，建完自动选中 | `create_scheme` |
| 3 | 来源多一个「使用其他型号的程序」，走借用而非导入 | `register_shared_module` |

## 3. 本轮不做

- **方案级借用**。`SharedModuleRef` 只有 `module_key`，没有方案维度；加它要动配置 schema、resolver、借用三入口和回源逻辑，属核心改动，单开一轮。本轮选了借用就固定通用，定制选项置灰。
- 方案的重命名、删除（`rename_scheme` / `delete_scheme` 已存在，本轮不接界面）。
- 「从已有程序复制一份独立副本」。用户明确选了引用语义，不是拷贝。
- 借用的覆盖确认之外的新交互。`confirmation_required` 沿用子任务 8 第 11 节。

## 4. 范围与方案（第 1、2 项）

对话框加一栏「范围」，两个互斥选项，默认**通用**：

```text
范围：(•)通用  ( )定制
方案：[西班牙          ▼] [新建方案]      ← 仅 scope=定制 时可用
```

- `scope=通用`：方案下拉与「新建方案」禁用，提交 `scope="通用"`、`scheme_name=""`。与现状一致。
- `scope=定制`：方案下拉可用。列表来自**磁盘真源**（见下），不是索引。
  - 列表非空：默认选第一项。
  - 列表为空：下拉显示「还没有方案」且不可提交，但「新建方案」**仍可点**——否则空型号建不出第一个方案。
- 提交时 `scope=定制` 而方案名为空：对话框本地拦住，不调服务。

### 4.1 方案列表的真源

`build_sidebar_tree` 的 `custom` 是从**资产**聚合的（`scheme_workbench_model.py:946-956`），空方案没有资产就不出现。刚建的方案必然是空的，所以不能用它。

复用既有 `direct_scheme_dir_names(model_root)`（`ui_common/workspace_actions.py:234`）——换版本的「改到定制」已经在用它，`entry_flows.py` 也已导入。它按 `discover_schemes` 列举、只认该型号 `定制/` 的直接子目录、排除嵌套型号和 `单模块变体`，空方案同样列出。**不另起一套方案列举 API。**

测试在 `test_ui_orchestration.py::test_semantics_path_follows_existing_scheme` 补一条：只有 `方案配置.toml`、没有任何固件的新方案必须立即出现在列表里。

### 4.2 新建方案

「新建方案」弹输入框收方案名，调用：

```text
create_scheme(configured_root, workspace_root, model_root, scheme_name)
```

走 `host.run_write`，与其他写操作同一门闩。

| code | 动作 |
| --- | --- |
| `ok` | 重新 `list_scheme_names`，把新方案选中，`toast` |
| `index_pending` | 同上 + `rescan_hint` |
| `invalid_name` / `path_exists` / `domain_violation` / `path_excluded` / `workspace_excluded` / `path_identity_conflict` | `keep_dialog`，新建程序对话框保持打开 |
| `invalid_target` | `alert` |
| `staging_unavailable` / `promote_failed` | `alert`，列表不变 |

方案名空：本地拦住。新建方案失败不影响已填的模块 / 程序名 / 厂商。

## 5. 使用其他型号的程序（第 3 项）

来源行变成两个按钮：

```text
来源：[选择文件] [使用其他型号的程序]
```

点「使用其他型号的程序」把对话框切到**借用模式**；再点「选择文件」切回**导入模式**。两种模式互斥，当前模式在来源只读文案里写明。

借用模式下：

- 出现「源程序」下拉：候选 = 工作区内**其他型号**、同一模块（`canonical_module_dir` 相等）的程序。复用右键借用的筛选口径（`workbench_window.py:1013-1025`）。
- 出现「更新方式」下拉：只有「固定版本」(`static`)、「自动更新」(`follow_asset`)。不提供 `follow_default`。
- 「程序名」「厂商」禁用并清空——借用不产生新程序目录，这两个字段没有意义。
- 「范围」强制通用，定制选项置灰，旁注「借用只支持通用」。
- 候选为空：源程序下拉显示「其他型号还没有程序」，不可提交。
- 切换模块后重新计算候选；已选的源程序若不再匹配则清空。

提交调用：

```text
register_shared_module(configured_root, workspace_root, target_model_root,
                       source_asset, mode=...)
```

`target_model_root` 是当前选中型号的根。首次不传 `overwrite_token`。

| code | 动作 |
| --- | --- |
| `ok` | 关闭对话框，刷新网格，`toast` |
| `confirmation_required` | `overwrite_confirm`。同意后把 `payload["overwrite_token"]` **原样**回传，其他参数不变；取消则零写入，对话框保持打开 |
| `invalid_args` | `alert`。UI 不发送 `follow_default` |
| `self_reference` / `no_other_model` / `retired_anchor` / `target_model_missing` / `invalid_target_model` / `invalid_asset` | `alert` |
| `config_parse_error` / `canonical_conflict` | `alert`，零写入 |
| `stale_plan` | `alert`，关闭对话框，要求重新打开 |

借用成功后不出现撤销条——撤销只属于「解除借用」（子任务 8 第 11 节）。

## 6. 提交分支

保存按钮按当前模式二选一，不是同一个调用：

| 模式 | 调用 | 必填 |
| --- | --- | --- |
| 导入 | `create_asset` | 来源、模块、程序名；`scope=定制` 时加方案 |
| 借用 | `register_shared_module` | 程序类型、源程序、更新方式 |

未选来源且未选源程序：提示「请先选择来源。」（沿用现有文案）。

## 7. 测试

编排断言写在 `ui_common`，Qt 对话框走既有 Qt 测试风格（`test_create_asset_source.py` 已有驱动真实对话框的先例）。

`ui_common`：

- `direct_scheme_dir_names`：在既有用例上补断言——只有 `方案配置.toml`、无资产的新方案也在列表里。

Qt 对话框：

- 默认是通用；方案下拉与「新建方案」此时禁用。
- 切到定制、方案列表为空时不可提交，但「新建方案」可点。
- 新建方案成功后列表刷新且新方案被选中。
- `scope=定制` 提交时 `create_asset` 收到 `scope="定制"` 与所选 `scheme_name`。
- 点「使用其他型号的程序」后程序名与厂商禁用、定制置灰；提交走 `register_shared_module` 而非 `create_asset`，且 `mode` 与下拉一致。
- 借用候选为空时不可提交。
- 切回「选择文件」恢复导入模式，提交走 `create_asset`。

不降低覆盖率门槛（80%）。`ui_qt` 仍在 omit 中。

## 8. 验收清单

- [x] 新建程序有「范围」栏，默认通用，行为与改动前一致
- [x] 定制可选已有方案；`create_asset` 收到 `scope="定制"` 与方案名
- [x] 空型号能在对话框里建出第一个方案并立即选中
- [x] 借用模式提交 `register_shared_module`，不调 `create_asset`；`confirmation_required` 能原样回传 token
- [x] 借用模式下定制置灰，`follow_default` 不可选
- [x] 不改 `create_asset` / `create_scheme` / `register_shared_module` 的签名与错误码
- [x] ruff、mypy、pytest 通过，覆盖率不低于 80%

## 10. 实现记录（2026-09-23）

- `entry_flows.open_create_asset`：加「范围」单选（通用/定制）、方案下拉 + 「新建方案」、来源行加「使用其他型号的程序」。提交按模式分派 `submit_create` / `submit_borrow`。控件加 `objectName` 供测试定位。
- 借用模式：隐藏并禁用程序名/厂商，`scope_custom` 置灰并强制通用；`borrow_mode` 只有 `static` / `follow_asset`。`confirmation_required` 弹确认后原样回传 `payload["overwrite_token"]`。
- `workbench_window.borrow_candidates_for(module)`：新增，从 `_register_shared_source` 抽出同一套筛选（排除当前型号、按 `canonical_module_dir` 匹配模块）。右键路径改为调用它，两条路径口径一致。空型号没有行可右键，这是借用能从新建程序进入的前提。
- 方案列举复用 `direct_scheme_dir_names`，未新增 ViewModel API。
- 测试：新增 `test_create_asset_scope_borrow.py`（5 条，驱动真实对话框）、`test_qt_smoke.py::test_borrow_candidates_excludes_current_model_and_other_modules`、`test_ui_orchestration.py` 补空方案断言。
- 验证：ruff、mypy 通过；pytest 1237 passed、1 skipped，覆盖率 92.69%。

## 9. 人工验证

接在子任务 8 第 16 节那条链之后，同一临时空目录：

1. 新建程序，范围保持通用 → 与之前一致，网格出现程序。
2. 范围切定制 → 提示还没有方案；点「新建方案」建一个 → 自动选中 → 保存 → 程序落在该方案下。
3. 再建一个型号，在它上面新建程序时点「使用其他型号的程序」→ 选第一个型号的程序 → 固定版本 → 保存 → 网格显示借用来源，磁盘没有新的程序目录。
4. 对同一模块再借一次 → 出现覆盖确认 → 同意 → 借用被替换。

## 2026-09-24 追加：文案与模式切换

- 对话框「模块」标签改「程序类型」；「借用其他型号」改「使用其他型号的程序」；「借用方式」改「更新方式」，选项「固定版本」(`static`)/「自动更新」(`follow_asset`)。网格借用来源标签同步为「固定版本·型号」「自动更新·型号」，旧 `follow_default` 仍显示「跟随默认（旧）」。
- 在「使用其他型号的程序」模式下点「选择文件」立即切回导入模式；即使取消文件对话框也不留在借用模式。测试 `test_choose_file_leaves_borrow_mode_even_if_cancelled`。
- 「使用其他型号的程序」不再按当前程序类型筛候选：列出其他型号的全部程序，显示「型号 / 程序类型 / 程序名」，型号按所在型号目录取（资产自带 `model` 来自文件名解析，可能不准）。进入该模式后「程序类型」下拉禁用并跟随所选源程序；切回选择文件后恢复可选。右键登记借用仍按所在行的类型筛选。测试 `test_borrow_lists_all_types_and_locks_program_type`。

## 2026-09-24 追加：空型号借用后列表看不到

- 现象：`D:\testprogram` 的 L36 没有自己的程序，已登记借用 L50S 主板程序（解析 `hit`），但 L36 列表 0 行、侧栏无「主板程序」。
- 根因：`_decorate_shared_cards` 只给本型号已有的通用行加借用信息，不补行（B3 起的设计）。右键登记借用总是从已有行出发，所以以前没暴露；新建程序里的借用第一次让空型号能借用。
- 改法：`SchemeWorkbenchModel._borrowed_only_cards` 为「有借用、无本地同类型行」的模块补一行 `borrowed_only=True`：命中时 asset 为源程序，缺失时为占位（`path=""`，显示「借用  借用来源缺失」）。`get_all_modules`、`get_common_modules` 追加这些行并遵守搜索词；`build_sidebar_tree` 通用分类补上借用模块。方案树仍不含借用（B4 隔离不变）。
- 右键菜单：只借用的行不给「设默认」「删除」（asset 属于源型号）；换借用/解除借用的写入门闩改检查当前型号根（`_shared_write_target`）。
- 原测试 `test_register_does_not_break_scheme_isolation` 中「全部模块不含借用行」的断言按新语义改为出现一行 `borrowed_only`。新测试：`test_borrow_without_local_row_*`（3 个）、`test_context_menu_borrow_only_row_has_no_default_or_delete`。
