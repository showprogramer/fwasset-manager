# TASK-20260922 子任务 8：UI 编排（空工作区起步）

- 状态：已完成；最终默认值与更新程序行为以 2026-09-27 已归档子任务为准
- r2 → r3：用户在本轮之后自行退役了待补齐机制与来源类型下拉，见 specs/archive/TASK-20260922-retire-incomplete-candidate.md、specs/archive/TASK-20260922-create-asset-source-auto-detect.md（两者均已实现完成）。本规格按已落地的代码回填：删除待补齐相关的入口、错误码动作、测试项与人工验证步骤；新建程序来源改为自动识别。不再要求 `created_incomplete` 触发 `open_incomplete`。
- r1 → r2：闭合 P8-001。`load_vendor_candidates` 仍只读进程缓存，不重读磁盘；读取失败回退维持 D6.1。`save_vendor_candidates` 成功写入当前 `CONFIG_PATH` 后，在返回前同步这份缓存；原先状态为 `missing` 时改为 `ok`，避免下一次读取仍退回缺省四家。
- 日期：2026-09-22
- 父规格：specs/active/TASK-20260903-crud-write-semantics.md（第 8 项；D6.1 写入口、D7.5 UI、D8.2 启动顺序、D9 空工作区、D10 本轮实际暴露的删除撤销）
- 上游：5a / 6a / 6b / 7 已实现并审查通过。本轮只**调用**其公开入口，不改其公共契约，不改这些 Task 与既有 Review，不改父规格状态表。
- 基线：`f9fe28e`（子任务 7，未 push）
- 3a 不实施。D9.3 自动迁移不实施。

用户已清空旧工作区下的型号目录，准备从空根用软件重新录入。因此本轮按使用顺序切片，不把父规格第 8 项一次性做完。

## 1. 本轮做 / 本轮不做

### 1.1 本轮做

| 优先级 | 能力 | 服务入口 |
| --- | --- | --- |
| 高 | 空工作区可见，空白型号重启后仍在，可新建型号（名称 + 机芯类型） | `enumerate_model_roots`、`detect_workspace_layout`、`create_model` |
| 高 | 新建程序（zip / 散选文件，来源类型自动识别），程序名预填，落在**通用区** | `create_asset` |
| 高 | 设为默认、改厂商 | `set_asset_default`、`update_asset_vendor` |
| 高 | 换版本（同语义更新）与改类型/改范围 | `update_asset`、`change_asset_semantics`；确认前用 `find_references_to` 展示命中 |
| 中 | 启动恢复顺序。缺了它，崩溃后写入会一直被拒 | `recover_interrupted_workspace` → `recover_on_startup` → 逐个 staging 会话 `cleanup_staging_area`；有续跑入口时再调对应 `resume_*` |
| 中 | 借用登记 / 解除 / 解除后撤销 | `register_shared_module`、`clear_shared_module`、`undo_clear_shared_module` |
| 中 | 厂商名单增删，以及新建/改厂商对话框消费这份名单 | 新增 `save_vendor_candidates`；既有 `load_vendor_candidates` |
| 低 | 「软件修复」对空工作区显示空列表；已归一型号不占行 | `preview_platform_normalize`、`scan_legacy_excluded_dirs`（只读） |
| — | 本轮改到的文案按第 4 节术语映射 | 只改 UI 字符串 |

### 1.2 本轮不做（切片冻结，不写对应界面）

- **待补齐界面**：已整体退役，不再有「待补齐」菜单、补齐对话框和提升落点框。导入零内容校验，选了什么原样入库。规则见 specs/archive/TASK-20260922-retire-incomplete-candidate.md。`scan_incomplete_imports` / `supplement_candidate` / `promote_candidate` / `delete_candidate` 仍留在 core，供历史候选目录的诊断使用，本轮 UI 不调用。
- **8b 归一向导**：选机芯类型、逐模块取值、回传 `expectation`、迁移按钮、调用 `normalize_platform_config` / `migrate_follow_defaults_for_normalize`。流程与不可逆文案冻结在第 12 节，避免下轮重议。本轮页面不提供执行按钮。
- 型号、方案、正式程序的删除、重命名，以及它们的撤销条和删除影响对话框。
- 新建 / 重命名 / 删除方案。新建程序对话框**不提供定制落点**，也不在对话框里临时建方案。
- `restore_retired_version` 的浏览和恢复。更新对话框仍要让用户选择退位方式。
- `normalize_module_leaf` 的操作界面。`update_asset` 返回 `normalize_required` 时只展示服务消息。
- D9.3 自动迁移。`single_model` 只展示服务给出的「请手动整理目录」。
- 为没有 `resume_*` 的操作新写恢复算法，或让 UI 删状态文件、代调用 `commit`。
- UI 不调用 `build_rewrite_plan`、`apply_rewrite_plan`、`build_clear_defaults_plan`。这些由写服务在锁内使用。
- 不改 5a / 6a / 6b / 7 的函数签名、错误码和 payload 形状。

工程尺度沿用父规格 2026-09-18 裁决：不做受签发入口、不做逐行崩溃注入、不做细粒度 TOCTOU。用户可见消息用服务返回的中文 `message`。删改要能恢复的范围，限于本轮真正暴露的删除：解除借用。

## 2. 分层

业务判定留在服务层。UI 只收集锁外输入、调用入口、按 `code` 决定下一步呈现。

| 层 | 职责 |
| --- | --- |
| `ui_common` | 可单测的编排：程序名预填、启动步骤顺序与结果解释、修复页行过滤、错误码到界面动作、借用覆盖 token 原样回传、5 秒撤销条是否仍可点 |
| `ui_qt` | 对话框、按钮、右键菜单、软件修复页、设置页厂商编辑。组合 ViewModel，不继承 ViewModel。耗时调用走既有后台任务与结果队列 |
| `core` | 本轮只新增厂商名单写入口，并让设置向导改路径时保留其他键。不把冲突分类、锁判定、CAS、catalog 完整性搬进 UI |

覆盖率配置已省略 `ui_qt/*`。编排断言写在 `ui_common` 与 `settings.py` 的测试里。Qt 外观留给人工验证。

建议落点：

| 内容 | 文件 |
| --- | --- |
| 程序名预填 | 新建 `src/fwasset/ui_common/asset_name_prefill.py` |
| 启动顺序、错误动作、修复行、撤销条时钟 | 新建 `src/fwasset/ui_common/workspace_actions.py` |
| 设默认 / 借用改接新服务 | `ui_common/view_models/scheme_workbench_model.py`、`ui_common/workbench_helpers.py` |
| 对话框、空状态、右键、任务队列 | `ui_qt/workbench_window.py` |
| 软件修复空页 | 新建 `ui_qt/repair_interface.py`，挂到现有 `FluentWindow` |
| 厂商编辑 | `ui_qt/settings_interface.py` |
| 保留其他配置键 | `ui_qt/setup_wizard.py` 的 `write_config` |
| 厂商名单写入 | `core/settings.py` 新增 `save_vendor_candidates`。成功写入当前 `CONFIG_PATH` 时同步 `load_vendor_candidates` 所读的缓存 |
| 测试 | 新建 `src/fwasset/tests/test_ui_orchestration.py`；厂商写入补在现有 vendor 测试或同目录新测试。不改服务契约测试的既有断言 |

写操作进度框**不提供取消**。服务调用一旦发出，事务不能从 UI 中途杀掉。`threading.Event` 继续只用于已有扫描取消。导入对话框在调用服务之前可以关闭，关闭等于未提交。晚到的任务结果若请求 id 与当前对话框不一致，丢弃，不再改界面。

UI 不构造、不持有 `WorkspaceLock` / `WorkspaceTransaction`。

## 3. 既有入口（只调用）

| 符号 | 模块 | UI 用法 |
| --- | --- | --- |
| `create_model` | `model_scheme_service` | 新建型号 |
| `enumerate_model_roots` | `reference_lookup` | 型号芯片的存在性。不看索引里有没有资产行 |
| `detect_workspace_layout` | `managed_paths` | 空状态 / 旧布局横幅的只读展示 |
| `create_asset` | `asset_service` | 新建程序。通用或定制范围由用户选择，`source_kind` 由 UI 按所选来源自动推断 |
| `update_asset_vendor` | `write_edit_service` | 改厂商 |
| `register_shared_module` | `write_edit_service` | 新登记。`mode` 只传 `static` 或 `follow_asset` |
| `clear_shared_module` | `write_edit_service` | 解除借用 |
| `undo_clear_shared_module` | `write_edit_service` | 5 秒内撤销解除 |
| `update_asset` | `layout_update_service` | 更新程序。UI 将所选文件暂存为来源目录 |
| `change_asset_semantics` | `layout_update_service` | 改类型或范围。来源必须是目录 |
| `find_references_to` | `reference_lookup` | 确认前展示命中。失败则不允许提交 |
| `load_workspace_status` | `workspace_transaction` | 每次写返回后读取。`recovery_required` 则切横幅 |
| `recover_interrupted_workspace` | `workspace_transaction` | 启动第 1 步 |
| `recover_on_startup` | `quarantine` | 启动第 2 步 |
| `cleanup_staging_area` | `staging_io` | 启动第 3 步，只对 staging 根的直接子目录 |
| `managed_root(ws, "staging")` | `managed_paths` | 定位 staging 根。不把根本身传给 cleanup |
| `resume_normalize_module_leaf` / `resume_update_asset` / `resume_change_asset_semantics` / `resume_restore_retired_version` | `layout_update_service` | 仅当操作日志名与函数对应时，由「继续恢复」调用 |
| `preview_platform_normalize` | `platform_normalize_service` | 修复页只读 |
| `scan_legacy_excluded_dirs` | `legacy_exclusions` | 修复页只读 |
| `load_vendor_candidates` / `normalize_vendor_list` | `settings` | 读名单、写前规范化 |
| `enabled_firmware_types` 或工作台已在用的 catalog 加载 | `firmware_catalog` | 模块下拉的唯一来源，不在 UI 再写一份模块表 |
| `ChassisType` | `types` | 四值：单3D、单2D、双2D、上3D下2D |

`configured_root` 用界面当前认定的程序文件夹，`workspace_root` 与它相同。二者不一致时服务返回 `root_changed`，UI 不自行改路径。

## 4. 术语（差异 #6，仅本轮露出的字符串）

| 现文案 | 本轮 |
| --- | --- |
| 登记/更换/取消共享来源 | 登记借用 / 更换借用 / 解除借用 |
| 固定版本 | 固定版本（`static`） |
| 自动更新、`follow_default` | 新登记不再提供。读到存量时显示「跟随默认（旧）」，没有登记入口 |
| `follow_asset` | 自动更新 |
| 平台、配置块名 | 对用户说「机芯类型」 |
| `旧版本/` 路径段 | 对用户说「备用副本」。磁盘目录名仍是 `旧版本` |

内部日志可以保留原词。用户可见句子不出现「回源」「共享模块」「follow_default」。

## 5. 通用界面动作

`ServiceResult.message` 原样展示。`code` 只决定动作，不另写一套业务解释。

| 动作 | 含义 |
| --- | --- |
| `toast` | 成功或 `unchanged` 的短提示，并按第 6–10 节刷新受影响的列表 |
| `keep_dialog` | 校验失败。对话框保持打开，不刷新成成功态 |
| `alert` | 模态展示 `message`。不自动重试，不写盘 |
| `rescan_hint` | 展示 `message`（其中已要求重新读取程序列表），并保留现有「读取程序列表」按钮 |
| `undo_bar` | 5 秒撤销条。超时后按钮消失，且不再调用撤销 |
| `overwrite_confirm` | 锁外确认。同意则原样回传 `overwrite_token`，取消则零写入 |
| `recovery_banner` | 禁用全部普通写入口，只按第 6 节保留「继续恢复」 |
| `repair_readonly` | 修复页只读行，无执行按钮 |

任意写调用返回后都 `load_workspace_status`。状态为 `recovery_required` 时进入 `recovery_banner`，即使本次 `code` 不是 `recovery_required`。

下列码在所有入口上动作相同：

| code | 动作 |
| --- | --- |
| `not_configured` | `alert`，把用户带到设置页配置程序文件夹 |
| `root_changed` | `alert`。提示先重新读取程序列表。不自动改根 |
| `workspace_busy` | `alert`。不自动连点重试 |
| `recovery_required` | `recovery_banner` |
| `write_failed` | `alert`，然后按状态决定是否进入横幅 |
| 未在下文单列的失败码 | `alert` 或新建/提升对话框内的 `keep_dialog`（输入还在屏幕上时用 `keep_dialog`） |

`ok: True` 且 `code` 为 `ok` / `unchanged` / `index_pending` / `reindex_failed` 都算服务已结束，不按异常弹红框。`index_pending` 与 `reindex_failed` 额外 `rescan_hint`。导入零内容校验后，正常路径不再产生 `created_incomplete`；万一读到，按 `toast` 展示 `message`，不打开任何候选界面。

## 6. 启动恢复

工作区根第一次可用时，以及设置里更换程序文件夹之后，各跑一次。刷新网格不重跑。

顺序固定，后台任务里串行，完成前写按钮禁用：

1. `recover_interrupted_workspace(workspace_root)`
2. `recover_on_startup(workspace_root)`
3. 列出 `managed_root(workspace_root, "staging")` 的**直接子目录**，对每一个调用 `cleanup_staging_area(workspace_root, session)`。根本身、嵌套路径、不存在的路径都不传。某个会话抛错：记下该路径和异常文本，继续下一个。三步都跑完再决定横幅。第 1 步已经是 `recovery_required` 时，仍然执行 2 和 3。

不提供取消。

结果：

- `state == clean` 且 `operation is None`：按原有写入门闩放开按钮。
- `state == recovery_required`：横幅「工作区有未完成的写入（操作：{operation}），完成恢复前不能写入。」操作名来自状态，为空则写「未知」。
- 仅当 `operation` 属于下表时显示「继续恢复」。用户确认后调用对应函数（无额外业务参数），返回后**重新跑**上面三步，再决定是否放开写入。

| operation | 续跑 |
| --- | --- |
| `normalize_module_leaf` | `resume_normalize_module_leaf` |
| `update_asset` | `resume_update_asset` |
| `change_asset_semantics` | `resume_change_asset_semantics` |
| `restore_retired_version` | `resume_restore_retired_version` |

其他操作名（包括 `create_model`、`create_asset`、`set_asset_default`、`update_asset_vendor`、借用三入口、`promote_candidate`、`normalize_platform_config`）没有续跑函数。横幅说明这一点，写按钮保持禁用。UI 不删除 `.fwasset` 状态、不调用 `commit`。

崩溃后的用户可见行为就是这条横幅：能续跑的给出按钮；不能续跑的保持只读，直到工作区状态离开 `recovery_required`。UI 不假装已经修好。

## 7. 空工作区与新建型号

型号芯片的名单 = `enumerate_model_roots` 的目录名。扫描结果只填程序网格。零个型号：空状态加「新增型号」。有型号但零程序：型号仍在，网格为空。新建成功后即使还没重扫，芯片也要出现新型号。

`single_model`：横幅说明应用不会自动迁移，请手动把内容整理进型号子目录。「新增型号」仍可点；服务返回 `migration_required` 时 `alert` 该 `message`。`invalid` 同样不在 UI 里禁用入口，展示 `layout_invalid` 的 `message`。

新建对话框（锁外）：名称文本框、机芯类型四选一（必选，不预选也不可提交）。提交调用：

```text
create_model(configured_root, workspace_root, model_name, chassis_type)
```

| code | 动作 |
| --- | --- |
| `ok` | 关闭对话框，`toast`，芯片加入新型号并选中 |
| `index_pending` | 同上，再加 `rescan_hint` |
| `migration_required` / `layout_invalid` | `keep_dialog` + `alert` |
| `invalid_chassis_type` | `keep_dialog`。正常路径不会出现，因为下拉只有四值 |
| `invalid_name` / `path_exists` / `domain_violation` / `path_excluded` / `workspace_excluded` / `path_identity_conflict` | `keep_dialog` |
| `staging_unavailable` / `promote_failed` | `alert`。芯片不增加 |

名称空、未选机芯类型：对话框本地拦住，不调用服务。

## 8. 新建程序

入口在已选中的型号上。对话框字段：

- 来源：不选类型，只有一个「选择文件」按钮。类型由 `classify_create_source` 推断——单个 `.zip` 为 `archive`，其余已选文件为 `files`。程序内容都是平铺文件，整夹导入压成 zip，故不提供「选择文件夹」。选中结果用 `describe_create_source` 只读展示；取消文件对话框不改已选来源
- 模块：catalog 既有显示名，顺序与 catalog 一致
- 程序名：打开时按下来源预填，用户可改
- 厂商：`load_vendor_candidates()`。名单非空时默认第一项；名单空则提交空串。允许保留不在名单中的手输值，因为删除厂商不得改写已有资产，新建也可以显式填写
- 范围固定通用。不出现方案下拉

预填规则（只在 UI，服务层空名仍是 `invalid_name`）：

| 来源 | 预填 |
| --- | --- |
| 文件夹 | 目录名（`classify_create_source` 仍支持，新建对话框不再产生） |
| zip | 文件名去掉末尾 `.zip`（大小写不敏感，只去一次） |
| 散选 | 有 `.rom` 时用该文件名去扩展名；否则用第一个非 `程序信息.toml` 的文件名去扩展名 |

散选同时缺 `.rom` 或 `.pkg`、且文件名像手控 UI 时，对话框显示不阻断的提示（`handcontrol_gap_hint`）。这只是提醒，保存照常调用服务。导入链路零内容校验，选了什么原样入库，不再按 catalog 分流。

提交：

```text
create_asset(..., source, source_kind, model_root, scope="通用",
             scheme_name="", module_name, asset_name, vendor)
```

| code | 动作 |
| --- | --- |
| `ok` | 关闭，刷新该型号网格，`toast` |
| `index_pending` | 同上 + `rescan_hint` |
| `invalid_name` / `invalid_scope` / `invalid_target` / `invalid_args` | `keep_dialog` |
| `layout_invalid` | `alert` |
| 准入码（`path_exists`、`domain_violation`、`path_excluded`、`workspace_excluded`、`path_identity_conflict`、`out_of_workspace`） | `keep_dialog` |
| `staging_unavailable` / `promote_failed` | `alert` |

工作区里可能留有历史候选目录。它们不进资产列表，由扫描以 warning 报出路径，用户自行移走或删除；UI 不提供提升、补齐和删除入口。

## 9. 设为默认、改厂商

右键「设为默认」在确认框之后调用 `set_asset_default(configured_root, workspace_root, asset)`。确认文案继续用型号 + 模块 + 变体名，不出现配置块名。不再调用 `set_module_default_for_model`。只对通用区、且还不是当前默认的行显示该菜单（展示条件可以沿用现在的网格数据；真正能不能写由服务决定）。

| code | 动作 |
| --- | --- |
| `ok` | 刷新网格，`toast` |
| `invalid_asset` | `alert` |
| `platform_not_normalized` / `canonical_conflict` / `canonical_duplicate` | `alert`。附一句「软件修复页可以查看，本轮不在那里执行归一」。不跳转去执行 |
| `follow_default_migration_required` / `reference_incomplete` | `alert`。不自动迁移 |
| `stale_plan` | `alert`。下次从右键重来 |

改厂商对话框：下拉为名单，并额外保留该资产当前 `vendor`（即使不在名单中）。提交 `update_asset_vendor(..., asset, vendor)`。

| code | 动作 |
| --- | --- |
| `ok` | 刷新，`toast` |
| `unchanged` | `toast`，不报错 |
| `metadata_corrupt` | `alert`。说明原文件还在（`message` 已说明） |
| `stale_plan` | `alert`，关闭对话框，要求重新打开 |
| `index_update_failed` | `rescan_hint`。磁盘已写入，不提示用户再改一次来「补写」 |

## 10. 换版本与改类型/范围

两者都先在锁外做完选择，再调用写服务。来源只接受**文件夹**。zip 和散选走第 8 节新建，不在 UI 里先解压再冒充目录。

确认前调用 `find_references_to(configured_root, workspace_root, old_asset_path, "asset")`：

- `ok: False`：`alert` 其 `message`（含 `retired_anchor`），确认按钮不可用，不调用写服务。
- `ok: True`：对话框列出命中条数；每条展示已有字段能直接读到的种类与位置。列表为空则写「没有借用或默认指向这个程序」。UI 不重新分类冲突。

退位二选一，**都不预选**，没选就不能确认：

| 界面文案 | 参数 |
| --- | --- |
| 删除旧程序 | `retire_to_trash` |
| 留作备用副本 | `retire_to_backup` |

换版本调用 `update_asset(..., old_asset, source_dir, retire_mode=...)`。新目录名由服务决定，UI 不另编路径。

改类型/范围由用户选 `change_kind` 并给出 `new_path`：

| 选项 | `change_kind` | 路径 |
| --- | --- | --- |
| 换到另一模块 | `change_type` | 同一通用区下另一模块 / 程序名 |
| 改到定制、改回通用、换方案 | `general_to_custom` / `custom_to_general` / `custom_scheme_move` | 需要已存在的方案目录 |

没有方案时，后三个选项不可选，说明「还没有方案」。不调用服务去试错。有方案时，UI 只拼接路径并传入，领域对不对由服务返回。

成功后**不**显示撤销条。`retire_to_trash` 的隔离种类是 `transactional_retire`，`undo_asset_delete` 不接受它。选「删除旧程序」时，确认文案写明本轮不能撤销这次退位。选备用副本时写明副本在该程序的「备用副本」目录，本轮没有恢复按钮。

| code | 动作 |
| --- | --- |
| `ok` | 关闭，刷新，`toast` |
| `reindex_failed` | 同上 + `rescan_hint` |
| `normalize_required` | `alert`。不调用 `normalize_module_leaf` |
| `invalid_args` / `invalid_target` / `path_exists` / `path_excluded` / `out_of_workspace` / `change_kind_mismatch` / `insufficient_space` / `incomplete_replacement` | `keep_dialog` |
| `plan_build_failed` / `config_rewrite_failed` | `alert`。服务已尽量恢复原状 |
| `retire_failed` / `retire_failed_with_config_conflict` / `update_inconsistent` | `alert` + 读状态。若已是 `recovery_required`，进入横幅 |
| `retired_anchor` | `alert`，不提交 |

`change_asset_semantics` 的 `vendor` 传入对话框里的当前选择；用户没改就传资产上的现有值，空则 `""`。

## 11. 借用

右键改接 `write_edit_service`，不再调用 `shared_module_service.set_shared_module` / `clear_shared_module`。

登记对话框：

- 来源列表仍是其他型号里同一模块的程序。空列表时确认不可用，文案「其他型号还没有这个程序类型的程序」。
- 模式只有「固定版本」(`static`) 和「自动更新」(`follow_asset`，2026-09-24 起沿用此词，与历史 `follow_default` 无关)。去掉来源平台下拉。
- 首次调用不传 `overwrite_token`。

| code | 动作 |
| --- | --- |
| `ok` | 刷新，`toast` |
| `confirmation_required` | `overwrite_confirm`。正文用 `message`，并展示 payload 里已有的现有条目摘要。同意后把 `payload["overwrite_token"]` **原样**传入同一次登记参数，其他字段不变。取消则结束 |
| `stale_plan` | `alert`，关掉选择器。用户要再打开才重试 |
| `invalid_args` | `alert`。UI 不发送 `follow_default` |
| `self_reference` / `no_other_model` / `retired_anchor` / `target_model_missing` / `invalid_target_model` / `invalid_asset` | `alert` |
| `config_parse_error` / `canonical_conflict` | `alert`，零写入 |

解除：确认框说明只删除借用记录、不删除固件文件。调用 `clear_shared_module`。

| code | 动作 |
| --- | --- |
| `ok` | `toast` + `undo_bar`。token 为 `payload["undo_token"]` |
| `unchanged` | `toast`，无撤销条 |
| `config_parse_error` / `canonical_conflict` / `stale_plan` | `alert` |

撤销条 5 秒，与服务里 `expires_at = monotonic + 5.0` 对齐。点击调用 `undo_clear_shared_module(..., undo_token)`。超时、进程重启或 token 缺失都不再调用。

| code | 动作 |
| --- | --- |
| `ok` | 刷新，`toast` |
| `undo_conflict` | `alert`「已不能撤销」，条消失 |

解除借用的撤销 token 只活在当前进程。撤销条不写入磁盘，重启后不恢复。

## 12. 软件修复（本轮只读空列表）

页面挂在主窗口，标题「软件修复」。打开时对每个 `enumerate_model_roots` 结果调用 `preview_platform_normalize`（只读、不持锁），并调用一次 `scan_legacy_excluded_dirs(workspace_root)`。

行过滤：

- 没有型号，且 legacy 列表为空：页面只有空状态「没有需要处理的项目」。
- 预览 `ok` 且 `preview.already_normalized`：不占行。新建型号是单块机芯类型，因此空工作区录入之后这里仍然是空列表。
- 预览 `ok` 但未归一：一行只读文字，型号名加「需要归一」。没有确认按钮，不展示冲突编辑器。说明完整步骤在后续切片。
- 预览失败：一行只读，展示该次 `message`。没有执行按钮。
- legacy 列表为空：不额外造「无排除目录」占位行。非空时只读列出 `path`，`is_retired_versions` 为真的标注「备用副本（有意排除）」。不提供删除关键词或移动目录。

本轮不调用 `normalize_platform_config` 与 `migrate_follow_defaults_for_normalize`。

### 12.1 留给 8b 的归一向导（本轮不实现）

下轮若做执行界面，必须按 TASK-20260921 第 7 节，不得改顺序：

1. 列出未归一型号。
2. `preview_platform_normalize` 展示块、`defaults`、逐模块冲突、待选机芯类型、`discarded_content`。
3. `follow_default_blocked` 或残留命中时，只提供「先执行迁移」→ `migrate_follow_defaults_for_normalize` → 重新预览。不自动迁移。
4. 用户选定机芯类型和每个冲突模块的取值后确认，调用 `normalize_platform_config`，**回传预览里的 `expectation`**。`stale_plan` 则回到步骤 2。
5. 成功后提示重新读取程序列表。

确认框必须同时写出这些不可逆点：将被删除的块、每个模块的最终取值、落选值不保留、归一后机芯类型不能再改（要改就新建型号）、`follow_default` 迁成 `follow_asset` 不能撤销、`平台配置.toml` 会被整体重写，自定义注释和未知字段不保留（用预览的 `discarded_content` 列出）。

## 13. 厂商名单

D6.1。设置页可追加、删除候选项。删除只改名单，不扫描、不改写任何 `程序信息.toml`。

新增：

```python
def save_vendor_candidates(
    values: list[str], *, config_path: Path | None = None
) -> ServiceResult: ...
```

- 默认写当前 `settings.CONFIG_PATH`。测试写其他临时文件。
- `load_vendor_candidates` 保持 D6.1，本轮不改它的读取规则：只读进程内 `_cfg` 与 `CONFIG_LOAD_STATUS`，不重读 `CONFIG_PATH`。`CONFIG_LOAD_STATUS != "ok"`、键缺失、或 `vendors` 不是字符串列表时回退缺省四家，不写盘；显式空列表返回空。因此保存后要让下一次 load 看到新名单，必须由写入方更新这份缓存，而不是让 load 临时改成每次读盘。
- 文件不存在：创建并写入调用方要保存的内容（向导首次创建时包含 `[paths]` 与 `vendors`）。不判 `unchanged`，即使名单等于缺省四家。
- 文件存在但解析失败，或已有 `vendors` 不是字符串列表：`config_corrupt`，**零写盘**，不改缓存。不得用只含路径的新文件覆盖整个 `config.toml`。
- 规范化走 `normalize_vendor_list`（trim、去空、casefold 去重、保留首次大小写）。
- 文件已成功解析，且其规范化名单（键缺失则按缺省四家）与本次规范化结果相同：`unchanged`，不写，不改缓存。目标是当前 `CONFIG_PATH` 时，这个比较就是 `load_vendor_candidates()` 的返回值，不再读磁盘。
- 否则只替换顶层 `vendors`，其他键原样保留，原子替换。
- `ok` 的 `message` 为「厂商名单已保存」。

缓存同步（P8-001）。新建/改厂商对话框下次打开只调用 `load_vendor_candidates()`，没有第二套刷新接口。成功写入**当前** `settings.CONFIG_PATH` 时，必须在返回 `ok` 之前更新 load 所读的那份缓存，同一进程内不必重启。

- 「是不是进程配置」看解析后的写入路径是否等于当前 `settings.CONFIG_PATH`，不看 import 时的 `CONFIG_LOAD_SOURCE`。测试可以改 `CONFIG_PATH` 指向临时文件。
- 写入其他路径：不改 `_cfg`、`CONFIG_LOAD_STATUS`、`CONFIG_LOAD_ERROR`。
- 写入当前 `CONFIG_PATH` 且结果为 `ok`：把缓存顶层 `vendors` 设为刚写入的规范化名单的**新列表**。不得复用调用方传入的 list；调用方随后修改入参不得改变下一次 load。
- 写入前 `CONFIG_LOAD_STATUS` 为 `missing`（文件原先不存在，本次刚创建）：用刚写盘的文档替换缓存，并把 `CONFIG_LOAD_STATUS` 设为 `"ok"`、`CONFIG_LOAD_ERROR` 设为 `""`。只写入 `vendors` 却把状态留在 `missing` 时，下一次 load 仍回退缺省四家。
- 写入前状态已是 `"ok"`：只替换 `vendors` 键，不重读文件，不改状态和其他键。
- `unchanged`、`config_corrupt`、`write_failed` 都不改缓存。写盘失败时磁盘与缓存都保持调用前的内容。

`setup_wizard.write_config` 改为走同一套「只改路径键、保留其他键」的写入。配置损坏时向导也拒绝并展示 `config_corrupt`，不再整文件覆盖。文件尚不存在时，向导仍可创建，并写入当前名单（缺省四家或用户刚编辑的名单）。向导成功写入当前 `CONFIG_PATH` 时按上面同一规则更新缓存：新建则状态改为 `"ok"`，缓存等于刚写盘的文档（含所写的 `vendors`）；只改路径时保留缓存里的其他键，并把 `vendors` 设为文件中保留的那份名单。

| code | 动作 |
| --- | --- |
| `ok` | `toast`。已打开的下拉不热更新。下次打开新建/改厂商对话框时调用 `load_vendor_candidates()`；因上面的缓存同步，同一进程内能看到新名单 |
| `unchanged` | `toast` |
| `config_corrupt` / `write_failed` | `alert`。表单保留用户刚编辑的草稿，磁盘和进程缓存都不动 |

实现和测试不得读取用户真实的 `config.toml`，也不得访问 `D:\按摩器程序`。

## 14. 失败与崩溃（界面侧）

| 情况 | 界面 |
| --- | --- |
| 用户取消对话框 | 未调用服务。磁盘不变 |
| 服务返回失败且状态仍是 `clean` | 按上表 `alert` / `keep_dialog`。不禁用后续写入 |
| 服务返回失败且状态变为 `recovery_required` | 横幅，普通写入禁用 |
| 进程在事务中被杀掉 | 下次启动走第 6 节。能续跑的四个操作显示「继续恢复」；其余保持横幅 |
| 写操作进行中 | 进度指示，无取消按钮。重复点击写入口被现有 busy 门闩挡住 |
| 索引类 `ok: True` 但非 `ok` | 数据以磁盘为准，提示重新读取程序列表 |

## 15. 测试

`test_ui_orchestration.py` 用注入的假服务，不建 Qt：

- 预填三行规则，以及散选无 `.rom` 时跳过 `程序信息.toml`。
- 来源推断：单个 `.zip` 为 `archive`；单个非 zip 与多选都为 `files`；未选为空。只读文案与之对应。对话框无「选择文件夹」按钮（`classify_create_source` 的 `directory` 分支仍单测）。
- 启动步骤的调用顺序；staging 根不被传入 cleanup；一个会话失败不影响下一个；`recovery_required` 仍会执行后两步。
- 四个可续跑操作名映射到对应函数名；`create_model` 等不映射。
- 修复行过滤：零型号为空；`already_normalized` 不占行；未归一与预览失败各占只读行且 `executable` 为假；legacy 空列表不加占位。
- `created_incomplete` 不再触发 `open_incomplete`；`confirmation_required` 回传的 token 与入参同一对象内容；解除借用的撤销条在 5 秒后不可调用。
- `retire_to_trash` 的成功结果不产生 `undo_asset_delete` 动作。
- 设默认的 ViewModel 路径调用 `set_asset_default`，文档或断言中不再把 `set_module_default_for_model` 当作右键实现。

厂商测试用临时 toml，不读、不写用户真实的 `config.toml`：保留其他键、损坏文件零字节变化、空列表可保存、重复项按 casefold 丢掉。另有一条不重启断言：把 `CONFIG_PATH` 和进程缓存指到临时文件后调用 `save_vendor_candidates`，返回后立刻 `load_vendor_candidates()`，结果等于刚保存的规范化名单；覆盖「状态已是 `ok`」和「原先 `missing`、本次创建文件」两种。写入其他路径不得改变这次 load 的结果。保存返回后修改调用方传入的 list，下一次 load 仍是保存时的名单。

不降低覆盖率门槛（80%）。`ui_qt` 已在 omit 中，不为此改 `pyproject.toml`。

## 16. 人工验证（实现完成后，提交之前）

一条临时空目录走完，前一步的目录是后一步的起点。代理不得把程序文件夹指到 `D:\按摩器程序`，也不得代点该盘。

1. 把设置里的程序文件夹指到空目录，打开主窗口。恢复完成后可以新建；软件修复页是空列表。
2. 新建型号，选一种机芯类型。型号出现，其下没有程序。
3. 用「选择文件」多选几个固件文件新建通用程序。程序名按文件名预填，可改；厂商是名单第一项。网格出现该程序。
4. 确认第 3 步建完没有 ★默认徽章，右键也没有「设为默认」。
5. 改厂商为名单中的另一项。网格显示新厂商。
6. 更新程序：选择新文件，保留默认的「留作备用副本」。新程序在，旧目录进入备用副本；有引用命中时确认框显示影响。
7. 同一工作区：用缺 `.pkg` 的手控文件新建。对话框给出不阻断提示，保存后程序**直接入库**出现在网格里，不进任何候选区。再用一个 zip 建一次，来源类型无需手选。
8. 再建一个型号和程序，在第一个型号上登记借用，解除，5 秒内点撤销，借用回来。
9. 设置页增加一个厂商名，再打开新建程序，下拉里有它。

第 7–9 步仍在同一目录，用来覆盖中优先级，不另起一份数据。

## 17. 验证与提交

实现阶段先写会失败的编排测试，再写实现。候选稳定后：

```powershell
uv run ruff check src scripts
uv run mypy
uv run python -m pytest -q
```

只在 Windows `.venv`。用户已确认当前工作区候选验收通过；提交按 [Agent 工作流](../../docs/agent-workflow.md#验证与提交)执行，不 push。用户可见变化已记录于 CHANGELOG Unreleased。

## 18. 验收清单

- [x] 规格审查通过（`8 review: PASS`）
- [x] 录入、更新、借用与修复入口具备界面，恢复与错误状态由编排层处理
- [x] 默认值入口移除；通用与定制方案各显示实际程序
- [x] 启动恢复顺序及不可续跑时的写入门闩有测试
- [x] 新建型号后芯片不依赖索引行；程序名自动预填
- [x] 更新程序展示引用影响，旧程序默认保留备用副本
- [x] 导入零内容校验：缺 `.pkg` 的手控来源直接入库；界面无待补齐入口
- [x] 新建程序来源自动识别，zip 与多选文件均可使用
- [x] 借用、厂商设置与软件修复的服务错误和界面状态有测试
- [x] 用户确认当前工作区候选的真实界面验收通过
- [x] ruff、mypy、pytest 通过，覆盖率不低于 80%
