# 新建程序：来源类型自动识别（取消手动选类型）

- 状态：已完成
- 日期：2026-09-22
- 来源：用户口头描述（第 1 条），本文为修饰后的可执行需求
- 涉及文件：`src/fwasset/ui_qt/entry_flows.py`（`open_create_asset`）

## 1. 背景与问题

「新建程序」对话框当前要求用户先在「来源」下拉框手动选择来源类型（文件夹 / zip / 散选文件），再点「选择来源」弹出对应的文件对话框。用户期望：**直接选择来源，类型由程序自动判断**，不手动选类型。

后端 `create_asset` 已支持三种来源（`source_kind` = `directory` / `archive` / `files`），本需求是纯 UI 交互改动，不涉及核心层。

## 2. 既有能力（勿重复实现）

zip 来源后端已自动解压：`create_asset` → `stage_import_archive`（`src/fwasset/core/import_io.py:157`）会解压进会话临时目录并最多剥掉一层包装目录。本需求**不需要**新增任何解压逻辑，选 zip 的体验即「选完直接保存」。

## 3. 平台约束（非倒退）

Windows 原生 `QFileDialog` 无法在同一窗口混选「文件夹」和「文件」，因此不能真正做到一个按钮选任意来源。最接近用户意图的方案：把「选择来源」拆为两个按钮，类型推断由程序完成，用户全程无感。

## 4. 目标交互

1. 移除来源类型下拉框（`kind` QComboBox）及 `chosen["kind"]` 的手动赋值链路。
2. 「选择来源」按钮改为单个「选择文件」→ `QFileDialog.getOpenFileNames`（可多选，可选中 `.zip`）。

   **2026-09-23 追加**：原设计并列「选择文件夹」+「选择文件」两个按钮。用户确认程序内容都是平铺文件（`.bin` / `.rom` + `.pkg` / `.hex`，无子目录），新建只会选文件，故删除「选择文件夹」按钮。需要整夹导入时先压成 zip。`classify_create_source` 的 `directory` 分支保留——它是 `ui_common` 的公共函数，换版本仍是文件夹专用（`layout_update_service.update_asset` 对非目录来源返回 `invalid_args`）。
3. 选择完成后程序自动推断 `source_kind`：
   - 单选一个 `.zip` 文件 → `archive`
   - 选 1 个非 zip 文件或多选文件 → `files`
   - `directory` 仍由 `classify_create_source` 支持，但新建程序对话框不再产生它
4. 来源展示改为只读文案（如「已选择：文件夹 D:\src」「已选择：3 个文件」），不再依赖下拉框状态。
5. 下游行为不变：
   - `create_asset` 按推断值传 `source_kind`；
   - `prefill_asset_name`、`handcontrol_gap_hint` 使用推断后的 kind 计算预填程序名与提示。
6. 未选来源直接保存时，保持现有提示「请先选择来源。」

## 5. 验收标准

- 对话框中不存在任何用于选择来源类型的下拉框，也不存在「选择文件夹」按钮。
- 两种来源各走通一次创建（单个 zip / 多选文件），入库结果与改动前一致。
- 预填程序名与 handcontrol 缺口提示在三种来源下行为与改动前一致。

## 6. 验证

- 推断在 `classify_create_source` / `describe_create_source`（`ui_common/asset_name_prefill.py`）。对话框只负责两个按钮和只读文案。取消文件对话框不改已选来源。
- `uv run ruff check src scripts`、`uv run mypy` 通过。
- `uv run python -m pytest -q`：1229 passed，1 skipped，覆盖率 93.05%。日志里的 junction 子进程 `UnicodeDecodeError` 与本次改动无关。
