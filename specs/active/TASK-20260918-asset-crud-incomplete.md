# TASK-20260918-asset-crud-incomplete：程序新增 / 删除 / 待补齐实现规格

## 状态

| 项 | 状态 |
| --- | --- |
| 类型 | 实现规格（子任务 5） |
| 当前状态 | **已完成**（r5 复核通过，最终检查通过，已提交 `8b9fdab`） |
| 版本 | r5（审查通过） |
| 前置 | 父规格 `TASK-20260903-crud-write-semantics.md` D0.2、D2.4a、D2.5、D3、D7.1–D7.5、D10.1c；子任务 1（事务基础）、2（元数据/schema）、3（准入与导入原语）、4（型号/方案 CRUD）已完成 |
| 父规格 | `specs/active/TASK-20260903-crud-write-semantics.md`（第 743 行子任务 5 定义） |

## 目标与范围

交付 **asset（程序）层** 的增删与待补齐候选区闭环，五个部分：

1. **新增程序**（D0.2 层级 + D7.1–D7.4 导入）——落点 `通用/<模块>/<程序名>/` 或 `定制/<方案>/<模块>/<程序名>/`；
2. **删除程序**（D2.4a）——反查、隔离、空模块容器 `rmdir`、两级索引对账；
3. **待补齐候选区**（D7.5）——catalog 不完整时留在 `.fwasset/incomplete/`，返回 `created_incomplete`；
4. **补充文件服务**（D7.5）——禁止覆盖、staging 合并、CAS 提交、完整后原子切 `import_state`；
5. **删除待补齐项**（D10.1c）——不走 R8 asset 反查，仍过路径守卫与状态 CAS，可撤销。

**不在本子任务**：设为默认 / 改厂商 / 借用编辑（子任务 5a）、update 与改类型复合操作（子任务 6）、UI 编排（子任务 8）。本子任务不接 UI。

### 与子任务 4 的分工

子任务 4 做的是**容器**（型号、方案），本子任务做**容器里的内容**（程序）。两者共用 `WorkspaceTransaction`、`admission`、`quarantine`、`asset_reconcile`，事务收敛语义完全沿用子任务 4 已收口的 MSC-001 / MSC-014 / MSC-015 判据，不重新发明：

- 零产物失败 → `commit()` 收敛为 `clean`，可重试；
- 已落盘的半成品 → 不 `commit()`，`__exit__` 落 `recovery_required`，payload 带 `recovery_required: True`；
- 判据一律是**原路径是否仍在原位**，不是「有没有抛异常」。

## 复用清单（已存在，不重复实现）

| 能力 | 入口 | 位置 |
| --- | --- | --- |
| 准入全套校验 | `validate_new_path(..., kind="asset")` | `admission.py:51` |
| 导入成形（散文件 / 目录 / zip） | `stage_import_files` / `stage_import_directory` / `stage_import_archive` | `import_io.py:48,98,128` |
| 锁内提升（复验 + 建容器 + 原子提升） | `promote_import` | `import_io.py:177` |
| staging 分配 / 清理 | `allocate_staging_area` / `cleanup_staging_area` | `staging_io.py:104,337` |
| 隔离登记 / 撤销 | `register_delete` / `undo_delete` | `quarantine.py:345,409` |
| 引用反查 | `find_references_to(target, "asset")` | `reference_lookup.py:728` |
| 阻断级判定 | `is_blocking_issue` | `reference_lookup.py:63` |
| 索引对账 / 清空边界 | `reconcile_subtree` / `bulk_reindex_subtree` | `asset_reconcile.py:33` / `asset_index.py:606` |
| 候选区根 | `managed_root(ws, "incomplete_candidate")` → `.fwasset/incomplete` | `managed_paths.py:97` |
| 受管路径判定 | `managed_path_reason` → `incomplete_candidate` | `managed_paths.py:127` |
| 程序元数据读写 | `load_asset_info_with_status` / `save_vendor` | `asset_info.py:44,135` |
| catalog 类型匹配 | `_match_catalog_type`（含 `handcontrol_ui` 双文件硬约束） | `file_scan.py:118` |

**需新增的底层能力**：`asset_info.py` 的 `import_state` / `intended_firmware_type` 读写（当前只有 `vendor`）。

## 设计

所有服务位于新模块 `fwasset.core.services.asset_service`，返回 `ServiceResult`，中文消息，签名带 `configured_root` / `workspace_root`，与既有 `core/services/` 风格一致。

### A1 catalog 完整性判定（新增程序的分流点）

**唯一判据**：staging 会话内容能否被 `_match_catalog_type` 识别为某个 catalog 类型。

- 能识别 → 完整，走 A2 正式提升；
- 不能识别 → 不完整，走 A3 候选区。

**不自行实现第二套完整性规则**。`handcontrol_ui` 必须同时有 `.rom` 与 `.pkg` 这条硬约束已在 `file_scan.py:128-135`，复用即可——这正是「手控 UI 缺文件」的典型场景。

> 判据挂在 catalog 而非固定扩展名清单：`firmware_catalog.toml` 条目顺序影响匹配，硬编码会与它漂移。

需要从 `file_scan` 暴露一个窄接口（对 staging 目录判定类型），不要让服务层直接调私有 `_match_catalog_type`。

### A2 新增程序（完整路径）

入口：`create_asset(configured_root, workspace_root, *, source, source_kind, model_root, scope, scheme_name, module_name, asset_name, vendor, log_fn)`

- `source_kind`: `"files" | "directory" | "archive"`，对应三个 stage 原语；
- `scope`: `"通用" | "定制"`；`定制` 时 `scheme_name` 必填。

落点按 D0.2：

```
通用/<模块>/<程序名>/
定制/<方案>/<模块>/<程序名>/
```

步骤（锁外 / 锁内边界严格按 D8「用户选择一律发生在锁外」）：

1. **锁外**：gate、布局、参数校验（scope / scheme_name / module_name / asset_name 非空）；目标型号根存在且是型号根。
2. 进入 `WorkspaceTransaction(operation="create_asset")`。
3. stage 来源（`stage_import_*`）——**stage 阶段不调 `begin_product_write`**（`import_io` 已保证），失败清理会话并 `commit()` 收敛 `clean`。
4. **A1 完整性判定**：不完整 → 转 A3（同一事务内，见 A3 步骤 3 起）。
5. 完整 → `promote_import(transaction, ws, configured_root, session, target)`（内部完成 D3.7 复验 + `begin_product_write` + 建容器 + 原子提升）。
6. 写 `程序信息.toml`：`vendor`（父规格「顺序要点」：新建程序要求写入 vendor）。
7. `set_phase("indexed", details={"target": ...})`；`reconcile_subtree(ws, target)`；失败 → `ok=True, code="index_pending"`。
8. `commit()`，返回 `ok=True, code="ok"`，payload 含 `asset_path`。

**失败收敛**：第 3/4 步零产物 → `commit()` 为 `clean`；第 5 步 `promote_import` 抛 `AdmissionError` → 此时是否已 `begin_product_write` 决定收敛方式——`validate_new_path` 在 `begin_product_write` **之前**（`import_io.py:192-198`），故准入失败仍是零产物，`commit()` 为 `clean`；提升本身失败 → 按子任务 4 同一判据，目标路径是否存在决定 `clean` / `recovery_required`。

### A3 待补齐候选区（不完整路径）

服务返回 `ok=True, code="created_incomplete"`（父规格 D7.5 明文）——**不是错误**。

1. 候选目录：`managed_root(ws, "incomplete_candidate") / <candidate_id>`，`candidate_id` 由应用生成（uuid4 十六进制前 8 位 + 名称 slug），**不接受用户输入**。
2. **候选路径不执行 D3 的父目录领域归属与 dangling-anchor 检查**（D7.5 明文——它不在 `通用`/`定制` 结构内）；但仍过 `assert_within_workspace`。
3. 从 staging 会话原子移入候选目录（`begin_product_write` 后）。
4. 写候选侧 `程序信息.toml`：

   ```toml
   vendor = "..."
   intended_firmware_type = "handcontrol_ui"
   import_state = "incomplete"
   ```

   **清单真源在磁盘**（D7.5：不得只放内存或只放 SQLite）。

5. **不写索引**——候选项不是 `FirmwareAsset`，不进 `assets` 表。
6. `commit()`，payload 含 `candidate_path`、`candidate_id`、`missing`（缺什么，给 UI 提示用）。

**候选项不保存旧操作意图**（D7.5 明文）：不持久化 create / update / change 意图、旧资产身份或过期计划。用户补齐后**重新选择**新增还是更新。

> 这条直接决定 `程序信息.toml` 里**不写** `intended_operation` / `target_asset` 之类的字段。写了就会有过期计划问题。

### A4 `scan_incomplete_imports`

入口：`scan_incomplete_imports(workspace_root) -> tuple[list[IncompleteCandidate], list[ScanIssue]]`，放 `core/incomplete_scan.py`（与 `file_scan.py` 并列，不混入普通 scanner）。

- 遍历 `managed_root(ws, "incomplete_candidate")` 下一级目录；
- 严格读取每个候选的 `程序信息.toml`，`import_state == "incomplete"` 才认；
- 重新跑 A1 完整性判定：已完整（用户手工塞了文件）→ 诊断 `ready_to_promote`，不自动提升（用户需显式操作）；
- **诊断分级**（沿用既有 `ScanIssue` 结构）：元数据缺失 / 解析失败 / 目录空 / `import_state` 值非法 → 各自 issue，不静默跳过。

**基础设施已就位（起草时实测确认）**：在临时工作区建 `.fwasset/incomplete/<id>/`（只放 `.rom`，缺 `.pkg`）后——`managed_path_reason` 返回 `incomplete_candidate`、`should_exclude_managed_path` 为 `True`、`scan_firmware_assets` 命中 0 且无 issue。即**双盲区的「普通 scanner 不认」这一半已经成立**，本入口负责补上「候选 scanner 认」的另一半；诊断沿用既有 `ScanIssue`（`types.py:210`，`severity` / `message` / `path` 三字段），不另造结构。

**可发现性不变量**（D7.5 明文）：候选目录在任何中间状态下崩溃后都必须能被本入口发现。实现保证：认定只依赖**磁盘上的目录存在 + 元数据文件**，不依赖任何内存态或事务日志；元数据写失败时候选目录也要留下（带 issue 报出），**不得出现普通 scanner 排除它、候选 scanner 又不认它的双盲区**。

> 具体做法：先建目录并移入内容，再写元数据。反过来（先写元数据后移内容）崩溃会留下空候选。两种都能被发现，但前者的残留对用户有意义。

### A5 补充文件服务

入口：`supplement_candidate(configured_root, workspace_root, candidate_id, *, files, log_fn)`

D7.5 要求「一次真实写操作，需独立事务契约」：

1. **默认禁止覆盖已有文件**——同名（normcase 等价）即 `file_exists` 拒绝，**不提供强制覆盖**（与 D3「不提供强制覆盖」同口径）。
2. staging 合并验证：新文件先落 staging 会话，与候选现有内容合并后**再跑一次 A1 判定**，确认这批文件确实让它变完整（或仍不完整，允许分多次补齐）。
3. CAS 提交：以候选目录当前 `manifest_hash` 为 preimage，提交前复验未变（并发保护）；不匹配 → `stale_candidate`。
4. 失败回滚：任一步失败清理 staging，候选目录保持原样。
5. 补齐后**原子更新 `import_state`**——完整时删除 `import_state` 键（或置 `complete`，二选一，见待裁决 Q1）。

**本服务不自动提升到最终路径**。D7.5 说「完整后才提升」，但提升需要用户重新选择落点（候选项不保存意图），所以提升是用户下一次显式调用 A2 的事——本服务只负责让候选变完整。

> 这点必须在 UI 文案上说清楚（子任务 8）：「补齐完成，请选择放到哪里」，不是「已添加」。

### A6 删除程序（D2.4a）

入口：`delete_asset(configured_root, workspace_root, asset_path, *, confirm_shared, log_fn)`

严格按 D2.4a 八步：

1. **锁外** `find_references_to(asset_path, "asset")`；阻断级 issue → `lookup_blocked`。
2. 影响列表全部返回；跨 owner shared 命中且 `confirm_shared=False` → `confirmation_required`，payload 带影响列表。
3. **副本影响必须回传**（D2.4a 明文）：统计 `旧版本/` 下副本数，payload 含 `retired_copies: N`，供 UI 明示「该程序包含 N 个备用副本，也将一并删除」。
4. 锁内：`register_delete` 移入隔离区。
5. **零 TOML 改写**（R8）——借用条目保留，解析自然 missing，撤销即恢复。**本服务不碰任何 TOML**。
6. 索引：`bulk_reindex_subtree(ws, asset_path, assets=[])`（该边界确定为空）。
7. **空模块容器自动删除，仅可 `rmdir`**——遇任何内容（含 `旧版本/`、`程序信息.toml`）一律保留，不递归删除。
8. **容器被删除时，对模块父级再做一次 `reconcile_subtree`**（D2.4a 步骤 8），保证 firmware-type / model 级 `hidden_items` 不残留。

**撤销**：沿用 `undo_model_scheme_delete` 的同构实现（见 A8）。D10.1b 的「删除最后一个变体后撤销需重建模块容器」：隔离清单须**记录本次自动 `rmdir` 的空父目录**，撤销时先重建容器再移回 asset；父路径已被占用 → `undo_conflict`，隔离内容保留。

> 步骤 7 的 `rmdir` 与 D10.1b 的「重建容器」是一对。隔离记录必须带上被删容器路径，否则撤销时 `os.replace` 的父目录不存在。**这是本子任务最容易漏的一条**。

**`QuarantineRecord` 必须扩字段（已核源码，不是可选项）**：当前 `types.py:129-145` 的九个字段里没有容器信息，且 `quarantine.py:115-151` 的 `_read_record` 是**按固定字段清单重建 dict**——多写的键在读取时被静默丢弃。因此：

- `types.py` 的 `QuarantineRecord` 增加 `removed_containers: list[str]`（默认 `[]`）；
- `_read_record` 增加该字段的读取与类型校验（非 list 或元素非 str → 归一为 `[]`，与既有「损坏字段降级不阻断整份清单」口径一致）；
- `_register` 需要能接收该值——**倾向新增一个可选关键字参数**（`register_delete(ws, source, *, removed_containers=())`），不改既有两个调用方的行为；
- **旧记录兼容**：缺该键的历史记录读为 `[]`，撤销时不重建任何容器——与当前行为完全一致，不产生迁移负担。

> 不扩字段的替代方案（撤销时按 `original_path` 逆推父目录并无条件 `mkdir`）**不可取**：无法区分「本次删除时自动删掉的空容器」与「用户此前就手工删掉的目录」，会凭空造出用户没要的容器。

### A7 删除待补齐项（D10.1c）

入口：`delete_candidate(configured_root, workspace_root, candidate_id, *, log_fn)`

D10.1c 明文：incomplete **不是** `FirmwareAsset`，不能调 `find_references_to(..., "asset")`。

1. 严格读取受管元数据，验证 `import_state == "incomplete"` —— 不是则 `invalid_candidate`（防止误删已提升内容）。
2. **不做 R8 asset 反查**（它从未进过索引，也没有任何 TOML 引用它）。
3. 仍过**路径守卫**（`assert_within_workspace` + 确认位于候选区内）与**状态 CAS**（`manifest_hash` 比对，防并发补齐后误删）。
4. 移入可撤销隔离区（`register_delete`）。
5. **不做索引对账**——候选项从未进过索引。

### A8 撤销

统一入口 `undo_asset_delete(workspace_root, record_id)`，覆盖 A6 与 A7 两类记录。

实现**完全沿用子任务 4 `undo_model_scheme_delete` 的收敛判据**（MSC-009 / MSC-015 已收口），只增加两处差异：

- **重建模块容器**（D10.1b）：记录里带 `removed_containers` 时，先按序重建，再 `undo_delete`；
- **对账分流**：asset 记录撤销后 `reconcile_subtree(原边界)`；候选记录**不对账**。

## 布局与前置矩阵

| 入口 | `single_model` | `invalid` | `empty` | `multi_model` |
| --- | --- | --- | --- | --- |
| `create_asset` | 允许（asset 级不受型号级布局限制，沿用 D3 方案域校验） | `layout_invalid` | `invalid_target`（无型号根可选） | 允许 |
| `delete_asset` | 允许 | `layout_invalid` | 不适用 | 允许 |
| `supplement_candidate` / `delete_candidate` / `scan_incomplete_imports` | 允许（候选区不依赖业务布局） | 允许 | 允许 | 允许 |

> 与子任务 4 的差别：型号级操作限定 `multi_model`，asset 级不限定（父规格「方案与程序的既有操作继续兼容旧布局」）。候选区完全独立于布局。

## 失败码汇总

| 错误码 | 触发点 | 说明 |
| --- | --- | --- |
| `not_configured` / `root_changed` | 所有入口 | `check_reference_gate` |
| `layout_invalid` | `create_asset` / `delete_asset` | 工作区存在无法归类内容 |
| `invalid_target` | `create_asset` | 目标不是型号根 / 方案不存在 |
| `invalid_scope` | `create_asset` | `scope` 非 `通用`/`定制`，或 `定制` 缺 `scheme_name` |
| `out_of_workspace` / `path_exists` / `invalid_name` / `domain_violation` / `path_excluded` / `workspace_excluded` / `path_identity_conflict` | `create_asset` | 透传 `admission.AdmissionError.code`（MSC-017 同口径：`out_of_workspace` 是 `validate_new_path` 第一步） |
| `empty_source` / `duplicate_name` / `source_unreadable` / `source_overlap` / `archive_unsupported` / `archive_extract_failed` | `create_asset` | 透传 `import_io.AssetImportError.code` |
| `staging_unavailable` | `create_asset` / `supplement_candidate` | staging 分配失败 |
| `promote_failed` | `create_asset` | 原子提升失败，staging 已清理 |
| `created_incomplete` | `create_asset` | **`ok=True`**，内容不完整，已存入候选区 |
| `index_pending` | `create_asset` / `delete_asset` | `ok=True`，索引写入失败需重扫 |
| `lookup_blocked` | `delete_asset` | 反查含阻断级 issue |
| `confirmation_required` | `delete_asset` | 跨 owner shared 命中且未确认；payload 含影响列表与 `retired_copies` |
| `quarantine_failed` | `delete_asset` / `delete_candidate` | 判据同子任务 4 MSC-014：原路径仍在 → 零产物 `commit()` 为 `clean` 可重试；原路径已消失 → 不 `commit()`，`recovery_required` |
| `invalid_candidate` | `supplement_candidate` / `delete_candidate` | 候选不存在、元数据损坏，或 `import_state != "incomplete"` |
| `file_exists` | `supplement_candidate` | 补充文件与候选现有文件同名，禁止覆盖 |
| `stale_candidate` | `supplement_candidate` | CAS 复验失败（候选目录已被并发修改） |
| `undo_conflict` | `undo_asset_delete` | 撤销目标被占用或容器重建冲突，隔离内容保留 |
| `undo_failed` | `undo_asset_delete` | 三类触发点同子任务 4 MSC-015 判据 |
| `workspace_busy` / `workspace_recovery_required` | 所有入口 | 锁超时 / 存在待恢复现场 |

## 测试计划

**create_asset**：
- 三种 `source_kind` 各成功一次，落点符合 D0.2（`通用/<模块>/<程序名>/`、`定制/<方案>/<模块>/<程序名>/`）；
- `vendor` 写入 `程序信息.toml`；
- D3 校验码透传（含 `out_of_workspace`，与 MSC-017 同口径）；
- 模块容器不存在时自动创建，存在时复用；
- 提升失败 → `promote_failed`，工作区无残留；
- 索引写入失败 → `ok=True, code="index_pending"`，磁盘侧已存在。

**待补齐（A1/A3/A4）**：
- 只有 `.rom` 无 `.pkg` 的手控 UI 内容 → `ok=True, code="created_incomplete"`，**目标业务路径不存在**，候选目录存在且含三个元数据字段；
- 候选目录**不被普通 scanner 发现**（`scan_firmware_assets` 结果不含它），**能被 `scan_incomplete_imports` 发现**——同一份数据两个断言，直接锁死 D7.5 的「双盲区」不变量；
- 候选元数据缺失 / 解析失败 / 目录空 / `import_state` 非法 → 各产出对应诊断 issue，不静默跳过；
- 候选项**不写索引**（`query_assets` 查不到）。

**supplement_candidate**：
- 补入 `.pkg` 后候选变完整，`import_state` 被清除；
- 同名文件 → `file_exists`，候选目录内容零变化；
- 分两次补齐（仍不完整 → 再补 → 完整）全程可用；
- CAS 冲突（提交前并发改动候选目录）→ `stale_candidate`，候选保持原样；
- 中途失败 → staging 清理，候选目录零变化。

**delete_asset**：
- 无外部借用直接删除成功，隔离记录已登记；
- 跨 owner shared 命中且未确认 → `confirmation_required`，payload 含 `retired_copies`；
- 含 `旧版本/` 副本时 `retired_copies` 计数正确；
- 阻断级 issue → `lookup_blocked`；
- **零 TOML 改写**：删除前后所有 TOML 逐字节一致（R8 不变量，直接断言）；
- 删最后一个变体 → 空模块容器被 `rmdir`；
- 模块容器含 `旧版本/` 或 `程序信息.toml` → **容器保留**，不递归删除；
- 容器被删时对模块父级做了第二次 `reconcile_subtree`（断言调用，防 D2.4a 步骤 8 漏实现）。

**撤销（A8）**：
- asset 删除后撤销 → 内容逐项比对一致，索引恢复；
- **删最后一个变体后撤销 → 模块容器被重建**（D10.1b，父规格点名的新契约断言）；
- 撤销目标被占用 → `undo_conflict`，隔离内容保留；
- 候选项删除后撤销 → 重新能被 `scan_incomplete_imports` 发现（父规格第 627 行明文）。

**delete_candidate（D10.1c）**：
- 正常删除 → 移入隔离区，不做 asset 反查（断言 `find_references_to` 未被调用）；
- `import_state != "incomplete"` → `invalid_candidate`；
- 候选路径在候选区外 → 路径守卫拒绝；
- CAS 冲突 → 拒绝且候选保留。

**MSC-001 同构回归**（子任务 4 已收口的判据在本层复验）：
- `create_asset` 准入失败、`delete_asset` `confirmation_required`、`supplement_candidate` `file_exists` 各构造一次，断言返回后 `load_workspace_status(ws).state == "clean"`，不是 `recovery_required`。

**中断恢复**（阶段边界，非逐行崩溃注入，沿用子任务 4 尺度）：
- `create_asset` 提升成功、索引之前中断 → `recovery_required`，磁盘侧资产完整，补一次对账后收敛；
- `delete_asset` 隔离登记成功、容器 `rmdir` 之前中断 → 资产已移走，容器残留，状态 `recovery_required`，撤销仍可用。

## 已裁决问题（2026-09-18 用户确认）

**Q1**：补齐完成后 `import_state` **删除该键**，不置 `complete`。
理由：「候选项只能由补齐提升或删除离开候选区」（父规格第 733 行），置 `complete` 会让候选区出现既不是 incomplete 也没离开的中间态，`scan_incomplete_imports` 还得多认一种值。删除键语义干净——**有 `import_state` 就是待补齐，没有就是普通程序信息**。`intended_firmware_type` 同时删除（它只服务于候选期的类型提示）。

**Q2**：`create_asset` 的 `asset_name` **由用户输入，服务层必填**；**UI 预填一个从来源推导的默认值**（2026-09-18 二次确认）。

服务层契约不变：空串即 `invalid_name` 拒绝，**不做任何来源推导兜底**。推导只发生在 UI 打开对话框时——栏里预先填好、用户可改，提交到服务层的永远是非空名字。

> 为什么推导放 UI 不放服务层：预填让用户**在落盘前看见**将要创建的目录名并能改；服务层兜底则是用户提交空串后应用自己猜，名字要等建完才看得见。散选文件尤其明显——同样一组文件叫 `L36_v1` 还是 `L36`，当场看一眼就定了，不该硬编一套剥后缀规则在服务层。

**预填推导规则**（子任务 8 实现，此处定规则以免届时再议）：

| 来源 | 预填取值 |
| --- | --- |
| 文件夹导入 | 目录名 |
| zip 导入 | 压缩包名去 `.zip` |
| 散选文件 | **取 `.rom` 文件名去扩展名**；无 `.rom` 时取第一个非元数据文件名去扩展名 |

散选规则定为「`.rom` 优先」而非「公共前缀」：核对 `firmware_catalog.toml` 后确认，**唯一会出现多文件散选的类型是 `handcontrol_ui`**（`usb_flow = "paired_files"`，且 `file_scan.py:128-135` 硬约束必须同时有 `.rom` 与 `.pkg`），配对固定是 `.rom` + `.pkg`（`settings.py:186-187`）。取 `.rom` 名即可覆盖该场景，比公共前缀算法简单且结果可预期。

## 验证记录

### 自动化验证（r5，2026-09-19 最终检查）

在 Windows `.venv` 下执行：

- `uv run ruff check src scripts` → `All checks passed!`（退出码 0）。
- `uv run mypy` → `Success: no issues found in 50 source files`（退出码 0）。
- `uv run python -m pytest -q` → `977 passed, 1 skipped`，`TOTAL` 覆盖率 94.60%（门槛 80%），退出码 0。

> 首次 pytest 运行在结果汇总打印后出现一次 Qt 退出期崩溃（退出码 139），无任何用例失败；重跑退出码 0 且无 FAILED/ERROR，判定为 teardown 偶发，非用例缺陷。

### 人工验证

不适用。本子任务不接 UI（UI 编排归子任务 8），行为由测试完全断言，按 `docs/agent-workflow.md#验证与提交` 的例外判据自动化通过即可提交。

- Review：`docs/code-review/REVIEW-20260918-asset-crud-incomplete.md`，ACI-001～008 + 002a/002b/003a/003b/008a 共 13 项全部收口。
- CHANGELOG：不适用（服务层能力尚未接入 UI，用户不可感知；由子任务 8 接入时统一记录）。
- 迁移说明：不适用。`QuarantineRecord.removed_containers` 旧记录缺键读为 `[]`，行为与扩字段前一致，无迁移负担。

## DoD

- [x] `asset_service.py` 实现 `create_asset` / `delete_asset` / `supplement_candidate` / `delete_candidate` / `undo_asset_delete`，签名与本规格一致。（另含 ACI-003 修复引入的 `promote_candidate`）
- [x] `incomplete_scan.py` 实现 `scan_incomplete_imports`，诊断分级完整。
- [x] `asset_info.py` 扩展 `import_state` / `intended_firmware_type` 读写，沿用既有「严格读取 + 文本级合并保留未知键」模式。
- [x] `file_scan.py` 暴露供服务层使用的窄接口做 catalog 完整性判定，服务层不直接调私有 `_match_catalog_type`。（`classify_staged_content`）
- [x] `types.py` 的 `QuarantineRecord` 增加 `removed_containers`，`quarantine.py` 的 `_read_record` 补读取与降级校验、`_register` 补可选参数（D10.1b 重建模块容器所需；不扩字段则该契约无法实现，见 A6）。旧记录缺该键读为 `[]`，不产生迁移负担。
- [x] 测试计划全部用例落地，`uv run python -m pytest -q` 通过，覆盖率不低于 `pyproject.toml` 门槛。
- [x] `uv run ruff check src scripts` / `uv run mypy` 通过。
- [x] 父规格点名的新契约断言落地：删除最后一个变体后撤销需重建模块容器（`test_asset_service.py:1256`）、删除待补齐项的撤销（`:1302`）。
- [x] 验证方式：本子任务无 UI（子任务 8 接入），按 `docs/agent-workflow.md#验证与提交` 判据——行为可由测试完全断言，自动化通过即可提交；候选区双盲区、零 TOML 改写（`test_asset_service.py:983`）、容器重建三条为必须断言项。
