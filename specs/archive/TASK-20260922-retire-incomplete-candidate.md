# 待补齐机制整体退役：导入零内容校验

- 状态：已完成
- 日期：2026-09-22
- 来源：用户决策——「没必要检测，我们明确知道里面有什么内容；手控未来后缀会变化，不要锁死」
- 关联规格：specs/archive/TASK-20260922-create-asset-source-auto-detect.md（同一对话确认，UI 来源自动识别）
- 影响面：`core/services/asset_service.py`、`core/incomplete_scan.py`、`core/file_scan.py`（仅调用方）、`ui_qt/entry_flows.py`、`ui_qt/workbench_window.py`、测试

## 1. 决策

用户导入来源时**明确知道内容是什么程序**，应用侧的 catalog 完整性检测没有价值，且带来两个实际问题：

1. 缺文件（如手控 zip 缺 `.pkg`）被拦进候选区，打断正常导入；
2. 检测依赖 `firmware_catalog.toml` 的扩展名硬编码（`handcontrol_ui` 必须同时有 `.rom` + `.pkg`），而手控固件后缀未来会变化，锁死即阻碍。

**结论：导入链路零内容校验，选了什么就原样入库；待补齐机制整体退役。**

## 2. 改动范围

### 2.1 核心层

1. `create_asset`（`asset_service.py:180`）：删除 `classify_staged_content` 分流与 `_create_incomplete_candidate` 分支，staging 内容直接 `promote_import` 落库。
2. 更新链路（`asset_service.py:661` 一带）：同样移除完整性检测，更新内容原样替换。
3. 候选区机制下线：
   - `scan_incomplete_imports`（`incomplete_scan.py`）及调用方；
   - `promote_candidate` 服务与候选元数据读写（`save_candidate_metadata` 等）；
   - `_looks_like_handcontrol` 在导入链路的使用（`intended_firmware_type` 仅候选区使用，随候选区退役）；
   - staging 会话（`stage_import_*` / `cleanup_staging_area` / 事务）**保留**——它是导入的临时工作区，不是候选区。
4. handcontrol 硬约束降级：`file_scan.py` 中 `.rom` + `.pkg` 双文件规则**不再作为任何导入准入判据**；catalog 扩展名匹配仅保留在既有工作区资产的扫描识别/展示侧，不属于本轮改动。

### 2.2 UI 层

1. 工具菜单「待补齐」入口（`workbench_window.py:298`）移除。
2. 新建/更新成功后的 `open_incomplete` 自动弹窗（`entry_flows.py:232`、`entry_flows.py:514`）移除，结果回调不再处理 `open_incomplete` action。
3. `open_incomplete` 对话框及 `promote_candidate` 调用整体删除。

### 2.3 历史数据收尾

1. 已存在的候选目录（受管 `incomplete_candidate` 根下）：应用不再管理；扫描时作为诊断 warning 报出路径，提示用户手动处理（移走或删除）。
2. 不做自动提升/自动删除——内容去向由用户决定。
3. `managed_paths` 中候选区根常量保留（历史目录识别与排除扫描需要），不再产生新写入。

## 3. 非目标

- 不改既有资产的扫描识别规则（扩展名匹配、`handcontrol_ui` 识别展示）——那是 scanner 侧，用户没反馈问题。
- 不改备份/退位、共享来源、platform 归一等其他写语义。
- 不改 zip 自动解压与「最多剥一层」成形逻辑。

## 4. 文档同步

- AGENTS.md「`handcontrol_ui` 必须同时有 `.rom` 和 `.pkg`」一条改为「仅用于既有资产扫描识别，不作为导入准入条件」。
- README / architecture 中涉及「待补齐候选区」的段落同步删除或改为历史说明。

## 5. 验收标准

1. 任意内容创建直接入库：缺 `.pkg` 的手控来源、未知后缀文件、纯任意文件均可创建成功，全程无候选区分流。
2. 菜单无「待补齐」入口；创建/更新不再弹出补齐对话框。
3. 工作区存在历史候选目录时，扫描以 warning 报出路径，应用行为不受影响。
4. `test_asset_service.py` 中候选区相关用例（A1/A3/A4 等）按新语义重写或移除；`ruff` / `mypy` / `pytest` 通过，覆盖率不低于 80%。

## 6. 验证

- 新建、换版本、同模块更新都不再按 catalog 把内容送入候选区；缺 `.pkg` 的手控来源、未知后缀直接落在业务目录。
- 主界面没有「待补齐」。历史 `.fwasset/incomplete` 子目录在扫描时报 warning，不进资产列表。
- 已中断并停在候选元数据阶段的写入，启动恢复仍补写原有三键。
- `ruff`、`mypy` 通过。`pytest`：1229 passed，1 skipped，覆盖率 92.63%。
