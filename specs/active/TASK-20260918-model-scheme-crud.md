# TASK-20260918-model-scheme-crud：型号 / 方案 CRUD 实现规格

## 状态

| 项 | 状态 |
| --- | --- |
| 类型 | 实现规格（子任务 4） |
| 当前状态 | **已完成**（MSC-001~017 全部收口；自动化检查 + `.local/verify_subtask4.py` 五场景通过，用户 2026-09-18 确认验证通过） |
| 版本 | r4，修订自 r3（Codex 终审确认 MSC-005/006/007 已修到位，新增 MSC-008 一条，由统筹核对源码后落地；三轮审查累计 MSC-001~008 全部收口） |
| 前置 | 父规格 `TASK-20260903-crud-write-semantics.md` D2、D3、D5.1–D5.3、D9；子任务 1（事务基础）、2（元数据/schema）、3（准入与导入原语）已完成 |
| 父规格 | `specs/active/TASK-20260903-crud-write-semantics.md`（第 742 行子任务 4 定义、第 762 行 D9 验收） |

### 当前交接

```text
阶段：审查（实现复核）
负责人：审查 Codex（pane w9:pF）；统筹 omp（主会话）
候选：r2；基线：52d35ab
范围：审查只读，不修代码；问题回统筹（omp 主会话）裁决后由实现方修复
输入：本规格全文；快照 C:/Users/fanzehao/AppData/Local/Temp/fwasset-msc-r2/
  （changes-r2.patch 为任务差异、model_scheme_service.py / test_model_scheme_service.py 为新增全文）；
  工作区当前未提交状态即 r2 现场
下一步：Codex 出具复核摘要 → 统筹处理问题 → 落 Review 与状态更新
```

### r2 修订摘要

1. **MSC-001**（严重）：所有无产品写入的校验（gate、布局、`validate_new_path`、反查、用户确认）挪到 `WorkspaceTransaction` 进入之前；锁内保留的「一次重验」失败分支改为先 `transaction.commit()`（零产物时 `commit()` 是安全的空提交）再返回错误码，不再遗留触发 `recovery_required` 的残留分支。
2. **MSC-002**（严重）：`rename_model` 遇 `single_model` 统一返回 `migration_required`，由 `_rename_target` 自身的布局检查在调用 `build_rewrite_plan` 之前拦截，不进入 R8 根重命名路径，不再使用 `root_rename_unsupported`；矩阵与错误码表同步。
3. **MSC-003**（中等）：`create_scheme` 补齐 `set_phase("indexed", ...)` 写入，与 `create_model` 的恢复判据（`phase == "indexed"` 且 `products` 非空）保持同构，不再依赖未声明的隐式恢复分支。
4. **MSC-004**（中等）：D9 验收补两条断言（仅一个型号时 rename；创建两型号后删除来源型号触发 `no_other_model` 前提），并将 `test_single_model_workspace_allows_full_crud` 改名为 `test_multi_model_workspace_with_single_model_allows_full_crud`，避免与父规格 D9.0 的 `single_model`（旧布局）术语混淆。

### r3 修订摘要

5. **MSC-005**（严重）：`undo_model_scheme_delete` 在调用 `quarantine.undo_delete`（真正把隔离内容移回工作区，`quarantine.py:459` `os.replace` + `:464` 改写隔离清单）**之前**补 `transaction.begin_product_write()` 并 `record_product`，不再让整段撤销过程 generation 全程停在偶数；`UndoConflictError` 分支（`undo_delete` 内部占用检查发生在 `:454-457`，早于 `:459` 的 `os.replace`，确认零产物）在已 `begin_product_write()`、generation 为奇数的前提下直接 `commit()`——已核实 `WorkspaceTransaction.commit()`（`workspace_transaction.py:584-591`）对奇偶两种起始 generation 都会正确推进到偶数、写 `clean`、清日志，与 MSC-001 划线的「零产物空提交安全」结论同构，不需要额外分支。
6. **MSC-006**（严重）：`rename_model` / `rename_scheme` 在 `begin_product_write()` 之前补一次锁内 `validate_new_path` 重验，对齐父规格 D3 第 7 条与 `create_model`/`create_scheme` 已有的锁内重验口径；重验失败时按 MSC-001 划线（此时未 `begin_product_write`，零产物）先 `commit()` 再返回对应 `AdmissionError` 错误码。
7. **MSC-007**（中等）：在「对既有模块的改动」登记 `WorkspaceTransaction.commit()` 的公开契约补充——docstring 需明述「未 `begin_product_write()` 时的空提交是受支持的正常退出路径」，并要求补一条单测锁定该行为，与本规格多处失败路径（D1/D2 准入失败、D4 步骤 2/3、undo 的 `UndoConflictError` 分支）依赖的语义保持声明与实现一致。

### r4 修订摘要

8. **MSC-008**（中等）：`undo_model_scheme_delete` 中隔离侧 `directory_manifest` 抛 `ManifestError` 的分支（发生在 `begin_product_write()` 之后、`record_product` 与 `undo_delete` 之前，**零产物**）此前只规定映射为 `undo_failed`，未规定事务如何收束——按本规格自身的零产物空提交契约补 `commit()`，否则一次纯读的 hash 计算失败会把工作区打入 `recovery_required`（与 MSC-001 要根除的是同一类故障）。同步补 `undo_failed` 错误码表条目（此前缺失）与三条测试断言（窗口外撤销、`ManifestError`、撤销成功/冲突两路径的 generation 收束）。

### 审查结论

三轮独立审查（Codex）累计发现 MSC-001~008，全部收口。Codex 终审意见：**规格审查通过**。实现阶段优先编写的测试：① 撤销成功 / `UndoConflictError` 的 generation 收束；② `ManifestError` → `undo_failed` + `clean`；③ 重命名锁内重验拒绝。

## 范围

型号 / 方案的创建、重命名、删除与事务回滚（D2.1–D2.4，不含 D2.4a 删除程序）、空白型号显示、最后一个型号删除及撤销、零型号工作区新增（D9）、机芯类型写入（D5.1–D5.3）。

**仅支持 `multi_model` 布局**。任何型号级入口先 `detect_workspace_layout`；`single_model` 返回 `migration_required`（提示手动整理目录，不做自动迁移，D9.3 不实施）；`invalid` 返回 `layout_invalid`；`empty` 允许新建首个型号。方案/程序级入口（`scheme` kind）不受此前置限制，沿用 `admission.py` 既有的方案域校验。

工程尺度依据父规格「工程尺度（2026-09-18 用户裁决）」：删除可撤销、创建失败回滚、重命名失败契约必须完整；不做受签发入口（普通函数 + 配置根 gate 即可）、不做逐行崩溃注入（只测阶段边界）、不做细粒度 TOCTOU 重验（D3 第 7 条仅锁内重验一次，不展开为多点重验）。

## 复用的既有基础（签名已核对源码，不重新设计）

- `fwasset.core.workspace_transaction`：
  - `WorkspaceTransaction(workspace_root, *, operation: str)` — 上下文管理器，`__enter__` 校验 `WorkspaceStatus` 为 `clean` 且 generation 为偶数，否则抛 `WorkspaceRecoveryRequiredError`；`is_active`、`workspace_root`、`products` 只读属性。
  - `begin_product_write() -> None`（`:568-578`）：首次改动产品数据前调用，把 generation 由偶递增为奇（seqlock 开始），幂等（已是奇数直接返回）。
  - `set_phase(phase: str, *, details: Mapping[str, Any] | None = None) -> None`（`:502-511`）：更新操作日志阶段并深合并 `details`，原子落盘。
  - `record_product(path: str | Path, manifest: str) -> None`（`:513-536`）：记录产物路径 + manifest hash，供 D1.4c 原语校验。
  - `commit() -> None`（`:580-591`）：结束 seqlock（推进为偶数）、状态回 `clean`、清空操作日志。**只有全部步骤成功才调用**；异常退出（`__exit__` 未提前 `commit`）自动落 `recovery_required`（`:601-607`）。
  - `WorkspaceLock(workspace_root, *, timeout_seconds=2.0)`：命名互斥体，获取失败 `WorkspaceBusyError`。
  - `load_operation_log(workspace_root) -> OperationLog | None`：崩溃恢复读操作日志，`None` 表示无待恢复现场。
  - `workspace_lock_is_held(workspace_root) -> bool`：线程级持锁校验，`staging_io` / `quarantine` 内部用它拒绝未持锁调用。
- `fwasset.core.staging_io`：
  - `allocate_staging_area(workspace_root, transaction) -> Path`（`:104-120`）：持锁事务内分配 staging 会话目录（`.fwasset/staging/<uuid>`）。
  - `promote_staging(transaction, workspace_root, staging_area, target) -> None`（`:123-162`）：把 staging 目录原子提升为 `target`；**`_assert_promotable_target`（`:165-191`）会拒绝任何已存在的同名目标（含大小写等价）**——D2.1/D2.2 新建型号/方案时，`通用/` `定制/` 等子目录须先在 staging 内搭好完整骨架再整体提升，**不得**先 `mkdir` 目标再往里写内容。
  - `cleanup_staging_area(workspace_root, staging_area) -> None`：删除整个 staging 会话（失败路径清理用）。
- `fwasset.core.managed_paths`：
  - `detect_workspace_layout(workspace_root) -> WorkspaceLayout`（`:370-418`）。
  - `managed_root(workspace_root, kind)`、`assert_managed_write(...)`：受管目录写授权。
- `fwasset.core.quarantine`：
  - `register_delete(workspace_root, source) -> QuarantineRecord`：登记 `undoable_delete`，5 秒撤销窗口；调用方须已持锁。
  - `undo_delete(workspace_root, record_id) -> QuarantineRecord`：窗口内撤销，目标被占用抛 `UndoConflictError`。
- `fwasset.core.reference_lookup`：
  - `find_references_to(configured_root, workspace_root, target_path, target_kind) -> ServiceResult`：`payload["result"]` 为 `ReferenceLookupResult(hits, issues, model_roots)`。
  - `check_reference_gate(configured_root, workspace_root) -> ServiceResult | None`：统一配置根 gate（`not_configured` / `root_changed`）。
  - `enumerate_model_roots(workspace_root) -> list[Path]`：型号根枚举。
  - `is_blocking_issue(issue) -> bool`：阻断级 issue 判定。
- `fwasset.core.services.reference_service`（D2.3 复合配置计划的落地依据，本任务**不直接调用**其 `build_rewrite_plan`——见下方「D2.3 与 R8 的关系」）。
- `fwasset.core.admission`：
  - `validate_new_path(target, *, kind: AdmissionKind, configured_root, workspace_root) -> Path`（`:51-75`）：D3 全套 1–6；`AdmissionKind` 目前是 `"model" | "scheme" | "asset"`。
  - `AdmissionError(code, message, payload)`。
- `fwasset.core.asset_reconcile.reconcile_subtree(workspace_root, subtree_root, *, catalog_path=None, path=None, cancel_event=None) -> None`：扫描 + 单事务整批重建；`bulk_reindex_subtree` 传 `assets=[]` 才不扫描直接清空。
- `fwasset.core.asset_index.bulk_reindex_subtree(workspace_root, subtree_root, assets, *, path=None, scanned_at=None) -> None`。
- `fwasset.core.manifest.directory_manifest_hash(root, *, exclude_names=frozenset()) -> str`：`directory_manifest` 仅接受目录（`is_dir()` 前置检查），文件需单独处理（见 D2.2 的 `方案配置.toml` 写入路径）。
- `fwasset.core.model_config`：`slugify_model_id`、`save_model_id`、`MODEL_CONFIG_FILENAME`。
- `fwasset.core.platform_config`：`PlatformDefaults`、`save_platform_config`、`PLATFORM_CONFIG_FILENAME`。
- `fwasset.core.scheme_config`：`discover_schemes`、`SchemeConfig`（只读，**当前没有写入口**——D2.2 需要新增 `方案配置.toml` 序列化，见下）。
- `fwasset.core.services.platform_default_service.ensure_platform_blocks`：D5.3 要求它**退出新写路径**（本任务的新建型号入口不调用它）。

## D0：型号/方案级新增服务契约

所有函数位于新模块 `fwasset.core.services.model_scheme_service`（沿用 `core/services/` 既有风格，返回 `ServiceResult`，中文消息）。函数签名统一带 `configured_root: str | Path | None` 与 `workspace_root: str | Path`，与 `reference_lookup` / `admission` 的 gate 参数顺序一致。

### D1 `create_model`

```python
def create_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_name: str,
    chassis_type: ChassisType,
    log_fn: Callable[..., None] = print,
) -> ServiceResult
```

**锁外（MSC-001 修订：无产品写入的校验全部前移到事务外）**：

1. `check_reference_gate` 门闩（`not_configured` / `root_changed`）。
2. `detect_workspace_layout(workspace_root)`：`single_model` → `migration_required`；`invalid` → `layout_invalid`；`multi_model` / `empty` 放行。
3. `chassis_type` 必须是 `ChassisType` 四选一之一（非法值 → `invalid_chassis_type`，不接受空串——D5.2 新建型号必须写单块）。

以上三项都是纯读校验（`check_reference_gate` 读配置、`detect_workspace_layout` 读目录结构、`chassis_type` 是入参枚举校验），失败时函数直接返回错误码，**不进入 `WorkspaceTransaction`**，不产生任何 `recovery_required` 风险。

**锁内步骤**（`WorkspaceTransaction(workspace_root, operation="create_model")`）：

1. `validate_new_path(workspace_root / model_name, kind="model", configured_root=configured_root, workspace_root=workspace_root)`（D3 全套 1–6）——**保留在锁内**，因为父规格 D2.1 明确「`model_id` 枚举、占用检查与 TOML 原子写必须在同一次锁持有期内」，这一步就是该「占用检查」，不能移到锁外（移出会在准入通过与实际写入之间留出并发窗口）。**MSC-001 修订**：此步失败（任何 `AdmissionError`）时，先调用 `transaction.commit()` 再返回错误码——此时尚未调用 `begin_product_write()`，`transaction.products` 为空，`commit()` 只是把 generation 保持偶数、状态写回 `clean`、清空操作日志，是安全的空提交，不会绕过父规格「锁内重验」要求，只是让失败路径干净退出而不是坠入 `recovery_required`。
2. `transaction.begin_product_write()`。
3. `staging = allocate_staging_area(workspace_root, transaction)`；在 staging 内搭建骨架：
   - `staging/通用/`、`staging/定制/`（空目录，`mkdir`）；
   - `staging/型号配置.toml`：`model_id = slugify_model_id(model_name)`，若与既有型号 `model_id` 冲突则追加 `-2`、`-3`…（复用 `model_id_service._allocate_id` 同款去重逻辑，内联实现，不依赖 `ensure_model_ids` 的批量语义）；
   - `staging/平台配置.toml`：单块 `[[platform]]`，`name = chassis_type`，`defaults = {}`（`save_platform_config` 序列化格式，D5.2）。
4. `target = workspace_root / model_name`；`promote_staging(transaction, workspace_root, staging, target)`（一次性把整个骨架原子提升，避免「先建根目录再逐项写入」触发 `_assert_promotable_target` 拒绝）。
5. `transaction.set_phase("indexed", details={"target": str(target)})`。
6. `bulk_reindex_subtree(str(workspace_root), str(target), [])`（新型号零资产，直接清空式建索引，不必 `reconcile_subtree` 扫描一个空目录——空目录扫描结果恒为 `[]`，两者等价但 `bulk_reindex_subtree(..., [])` 更直接）。索引失败 → 不回滚磁盘，返回 `code="index_pending"`（`ok=True`，因为型号已合法创建，只是索引未同步）。
7. `transaction.commit()`。

**成功 payload**：`{"model_root": str(target), "model_id": <分配的 id>, "chassis_type": chassis_type}`。

**失败契约**（步骤 3–4 staging/提升失败，均已 `begin_product_write` 之后）：

- staging 分配失败（磁盘/受管根问题）→ `staging_unavailable`。此时已 `begin_product_write` 但 `transaction.products` 为空，`__exit__` 自动落 `recovery_required`——**这是预期行为，不是 MSC-001 的残留分支**：一旦调用 `begin_product_write()`，事务已进入「本次操作可能已改变磁盘」的语义区间（即使本次实际未写），必须留给启动恢复流程判定，不能静默 `commit()` 掩盖。恢复判据见下节。
- 提升失败（`_assert_promotable_target` 复核发现目标已存在，或 `os.replace` 失败）→ `StagingError`，捕获后 `cleanup_staging_area` 清理 staging，返回 `promote_failed`；**由于本操作是单次原子提升（步骤 4 一次性完成骨架），不存在「TOML 已写子目录未写」的中间态**，D2.1 描述的「按创建记录逐项回滚」在本实现中简化为「staging 整体清理，工作区侧零变化」——`record_product` 只在步骤 4 成功后才记录，提升失败时 `transaction.products` 为空，无需额外回滚动作；**但因已 `begin_product_write`，同上仍落 `recovery_required`，由启动恢复清理残留 staging 并收敛为 `clean`**（见下节恢复判断，不是需要人工排障的异常态）。
- 步骤 6 索引失败：不影响 `ok=True`，见上。

**崩溃恢复**（启动时按 `load_operation_log` 恢复，归属子任务 1 的事务基础已提供 `recovery_required` 阻写语义；本节只定义 `create_model` 特有的恢复判断。**注意**：`validate_new_path` 校验失败已在锁外发生或已 `commit()` 退出，不会产生待恢复现场——以下两类只覆盖 `begin_product_write()` 之后的中断）：

- `operation == "create_model"` 且 `phase` 早于产物记录（即 `transaction.products` 为空）→ 判定为「staging 未提升，工作区侧无变化」，直接清理残留 staging 会话（若存在）并将状态收敛为 `clean`（复用 `recover_interrupted_workspace` 的既有阻写→人工确认流程，本规格不新增专用恢复函数，因为没有磁盘侧半成品需要清理）。
- `phase == "indexed"` 之前崩溃但 `transaction.products` 非空（即提升已完成、索引对账未做）→ 型号已合法存在，只需补一次 `bulk_reindex_subtree(..., target, [])`。
- 以上两类恢复动作由 UI 编排层（子任务 8）在启动时调用 `load_operation_log` 后按 `operation` 字段分流处理；本规格只固化判断依据（`products` 是否为空 + `phase` 字段），不新增独立的 `recover_create_model` 函数——因为两种情形都是「重放同一段幂等逻辑」（清理 staging 残留 / 补索引对账），没有需要单独封装的分支复杂度。

### D2 `create_scheme`

```python
def create_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    scheme_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult
```

**锁外（MSC-001 修订，同 `create_model`）**：

1. gate。
2. `_has_model_marker(model_root)` 校验（复用 `managed_paths._has_model_marker`，非型号根 → `invalid_target`）。**方案创建不要求 `multi_model` 布局**——`model_root` 已是型号根即可操作，旧布局下型号根本身就是 `single_model` 的根，方案 CRUD 按父规格「方案与程序的既有操作继续兼容旧布局」（D9.2）保持可用。

两者均为纯读校验，失败直接返回错误码，不进入 `WorkspaceTransaction`。

**锁内步骤**（`operation="create_scheme"`）：

1. `validate_new_path(model_root / "定制" / scheme_name, kind="scheme", configured_root=configured_root, workspace_root=workspace_root)`——**保留在锁内**（占用检查语义同 `create_model`，父规格 D2.1/D2.2 同粒度要求）。**MSC-001 修订**：失败时先 `transaction.commit()`（此时未 `begin_product_write`，`products` 为空，空提交安全）再返回错误码。
2. `transaction.begin_product_write()`。
3. staging 内建骨架：
   - `staging/`（空目录，方案目录本身内容为空——不强制要求先有模块）；
   - `staging/方案配置.toml`：内容 `name = "<scheme_name>"`（**不写 `platform` 字段**，D2.2/D5.3）。序列化新增小函数 `serialize_scheme_config(name: str) -> str`（本任务在 `scheme_config.py` 新增，格式：固定头注释 + `name = "..."`，风格对齐 `model_config._serialize_model_config` 的「固定文件头 + tomli-w dumps」）；对应新增 `save_scheme_config(scheme_root: Path, name: str) -> Path`（`atomic_write_text` 原子写，供本服务与未来重命名复用）。
4. `target = model_root / "定制" / scheme_name`；`promote_staging(...)`。
   - **已存在同名空目录不复用**——D2.2 规定按 `path_exists` 拒绝；此判定已由步骤 1 的 `validate_new_path`（含 `_assert_target_absent`）覆盖，无需额外检查。
5. `transaction.set_phase("indexed", details={"target": str(target)})`（**MSC-003 修订**：补齐与 `create_model` 同构的 phase 写入，使崩溃恢复判据 `phase == "indexed"` 对 `create_scheme` 同样成立——r1 遗漏此步，导致「提升后、索引前」中断落不进任何声明的恢复分支）。
6. `bulk_reindex_subtree(str(workspace_root), str(target), [])`。索引失败 → 不回滚磁盘，返回 `code="index_pending"`（`ok=True`，同 `create_model`）。
7. `transaction.commit()`。

**成功 payload**：`{"scheme_root": str(target), "scheme_name": scheme_name}`。

**失败/崩溃恢复**：与 `create_model` 同构（staging 提升前失败零产物、提升失败清理 staging、索引失败不影响 `ok=True`；恢复判据同样是 `products` 是否为空 + `phase == "indexed"`，MSC-003 修订后两者判据口径完全一致）。

### D3 `rename_model` / `rename_scheme`

父规格 D2.3 覆盖「型号/方案/模块/程序」四类目标；本子任务只交付 `target_kind ∈ {"model", "scheme"}` 两种（模块/程序重命名属子任务 5/5a/6）。

```python
def rename_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    new_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult

def rename_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    scheme_root: str | Path,
    new_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult
```

两者共用内部实现 `_rename_target(*, target_kind: Literal["model", "scheme"], ...)`。

**锁外（MSC-001 修订）**：

1. gate。
2. `target_kind == "model"` 时 `detect_workspace_layout`：`single_model` → **直接返回 `migration_required`（MSC-002 修订，见下）**；`invalid` → `layout_invalid`；`multi_model` 放行。
3. `new_path = target.parent / new_name`；`validate_new_path(new_path, kind=target_kind, ...)`（D3 1–6）。
4. **构建复合配置计划**（`build_rewrite_plan` 只读取现状、生成待签发 plan，不写磁盘，可在锁外完成；若失败见下）：
   - **MSC-002 修订**：本步骤只在 `target_kind == "model"` 且已通过步骤 2 的 `multi_model` 检查后才执行——`single_model` 已在步骤 2 直接返回 `migration_required`，**不再调用 `build_rewrite_plan`**，不进入 R8 根重命名路径，`root_rename_unsupported` 不再使用（该错误码从本规格移除，见下方矩阵与错误码表同步修订）。旧布局根改名与 `create_model`/`delete_model` 遇 `single_model` 时的处理口径完全一致，均为 `migration_required`。
   - 调用 `reference_service.build_rewrite_plan(configured_root, workspace_root, RewriteRequest(operation="rename", target_kind=target_kind, old_path=str(target), new_path=str(new_path)))` 取得 R8 plan（型号自身 `型号配置.toml` 随 rename-model 已被 `_assemble_plan` 纳入 `own_moved` 分支，`:552-554`；方案改名的 R8 plan 只覆盖**借用方**引用改写，不含方案自身 `方案配置.toml.name`）。
   - **方案改名额外的 `name` 字段改写**：R8 的 `RewritePlan.files` 结构（`FileRewrite`）足以承载「任意 TOML 文件的 preimage/postimage CAS」，但 `build_rewrite_plan` 目前只处理 `shared_modules` 与 `platform.defaults` 改写，不知道「方案自身 `name` 字段」这类语义。

     > **统筹裁决（2026-09-18）：外挂追加方案否决，改为在 R8 内部支持 `scheme_config`。** 外挂追加不止缺一个 token 重算入口——`_validate_plan_for_apply:751` 的 `kind not in ("model_config", "platform_config")` 会先把追加条目判为「未知计划条目类型」直接返回 `invalid_plan`，`:756-764` 的文件名白名单同样只认两种文件名。即使公开 `recompute_plan_token` 也走不通。且 `_PLAN_SALT`（`:101`）是进程随机盐，设计意图明确是「plan 只能由 build 签发、调用方不得手工构造」，公开重算入口等于把这条不变量作废——父规格第 69 行把它列为现状事实。**正确做法**：在 `build_rewrite_plan` 内部增加 `scheme_config` 条目类型（新 `kind` + 文件名白名单 + 自身 `name` 字段改写），由 `build_rewrite_plan` 统一签发并算 token。这是 R8 的功能扩展而非绕过，改动在 `reference_service.py` 内闭合，调用方仍只拿到已签发的 plan。**`recompute_plan_token` 不新增，`_append_scheme_name_rewrite` 之类的外挂函数不实现**——`scheme_config` 条目由 `build_rewrite_plan` 内部的 `_assemble_plan` 直接生成，详见下方「对 `reference_service` 的改动」。
   - `target_kind == "model"` 时不需要这一步（型号没有独立于 `型号配置.toml` 之外的「显示名」字段——型号名就是目录名，不落盘）。
   - 若 `build_rewrite_plan` 失败（如 `unsupported_semantic_change`、`lookup_blocked`）→ 直接返回该错误，不进入 `WorkspaceTransaction`，不移动目录。

以上四步全部只读或只构建待签发 plan，失败均在锁外直接返回错误码。

**锁内步骤**（`WorkspaceTransaction(workspace_root, operation="rename_model")` / `"rename_scheme"`）：

5. **锁内重验 `validate_new_path(new_path, kind=target_kind, ...)`（MSC-006 新增）**：`begin_product_write()` 之前再校验一次新路径，补齐父规格 D3 第 7 条「最终原子提升前、持写锁时再验一次」——r2 只在锁外校验过一次（步骤 3），`create_model`/`create_scheme` 已有锁内重验，rename 路径遗漏，口径不一致。失败（任何 `AdmissionError`）→ 按 MSC-001 划线：此时未调用 `begin_product_write()`，`transaction.products` 为空，先 `transaction.commit()`（安全空提交）再返回该 `AdmissionError` 对应错误码。
6. `transaction.begin_product_write()`；`os.replace(target, new_path)`（**移动目录本身**，不经过 staging——D2.3 步骤 3 明确是「移动目录」，与新建走 staging 提升不同：这里旧身份已存在，移动是原地重命名，staging 机制是为「新身份诞生」设计的，不适用于已有目录的改名）。若 `os.replace` 失败（含跨卷、权限）→ **`transaction.commit()`**（此时 `products` 为空，安全空提交）**再**返回 `rename_failed`（**MSC-001 修订**：r1 未提交即返回会落 `recovery_required`，此时磁盘与配置均未变，属于典型的「无产品写入却坠入恢复态」场景）。
7. `apply_rewrite_plan(plan, configured_root)`：
   - **完整回滚成功**（`code == "rolled_back"`）→ `os.replace(new_path, target)` 移回；`commit()`；返回 `rewrite_failed`（目录已移回、配置已回滚，属于「已完整补偿」的干净结束，可以安全 `commit()`）。
   - **`rollback_conflict`** → **停止自动回移**，`transaction` 保持未提交状态、`__exit__` 落 `recovery_required`（**这是预期行为，不是 MSC-001 残留分支**：磁盘上目录已在新路径、部分 TOML 已改写且回滚冲突，是真实的半成品态，必须交给启动恢复流程人工核实，不能静默 `commit()` 掩盖），返回 `rename_inconsistent`，payload 含 `plan.files` 的 `post_path` 清单与冲突详情（`apply_rewrite_plan` 返回的 `rollback_conflict` 列表）。
   - 回移本身失败（`os.replace` 第二次调用抛异常）→ 同样保持未提交、落 `recovery_required`，返回 `rename_inconsistent`。
8. 索引：**对移动前后最小共同现存祖先调用 `reconcile_subtree`**——`target_kind == "model"` 时共同祖先是 `workspace_root`（型号目录改名，父目录就是工作区根，本身就是最小共同祖先）；`target_kind == "scheme"` 时共同祖先是 `model_root / "定制"`（方案改名不会跨型号，`定制/` 目录必然同时是旧路径与新路径的祖先）。索引失败 → 提示重新读取程序列表，不回滚磁盘（父规格步骤 5 失败契约）。
9. `commit()`。

**成功 payload**：`{"old_path": str(target), "new_path": str(new_path), "applied_files": [...]}`。

**并发修改**：步骤 4–7 间检测到并发修改 → `apply_rewrite_plan` 内部的 preimage 校验会自然产出 `stale_plan`（R8 既有行为），本服务透传；若发生在步骤 4（锁外构建 plan 之后、进入事务之前）或步骤 5（锁内重验），直接返回不进入/不推进事务（步骤 5 失败已按 MSC-001 划线先 `commit()`）；若发生在步骤 7（锁内 apply 时），`transaction` 未提交、落 `recovery_required`（同 `rollback_conflict` 分支，因为 apply 可能已写入部分文件）。

**对 `reference_service` 的改动（统筹已裁决，范围闭合在 R8 内部）**：

新增第三种计划条目类型 `scheme_config`，用于方案自身 `方案配置.toml` 的 `name` 字段改写。落点三处，均在 `services/reference_service.py` 内：

1. `_validate_plan_for_apply:751` 的 kind 白名单加入 `"scheme_config"`；`:756-758` 的 `expected_name` 映射加入 `SCHEME_CONFIG_FILENAME`（当前是二选一三元表达式，改为 dict 查表）。
2. `_assemble_plan`（`:553` 附近 `own_moved` 分支）：`request.target_kind == "scheme" and is_rename` 时，读取方案自身 `方案配置.toml` 为 preimage，序列化 `name = 新目录名` 为 postimage，追加 `FileRewrite(kind="scheme_config", ...)`。与型号的 `own_moved` 处理同构。
3. token 由 `build_rewrite_plan:697` 现有的 `plan.token = _plan_token(plan)` 统一计算，**`_plan_token` 保持私有**。

这样方案改名的复合计划由 `build_rewrite_plan` 一次签发，满足父规格 D2.3 步骤 2「R8 改写项 + 方案自身 `name` 新内容一并持有 preimage/CAS，统一 apply」，也不触碰「plan 只能由 build 签发」这条不变量。

**须审查者确认**：此改动扩展了 R8 的条目类型矩阵，属跨子任务改动（`reference_service.py` 归属子任务 6 的 R8 恢复算法范畴）。若审查认为应推迟，退路是方案改名暂不同步 `方案配置.toml.name`（`name` 与目录名不一致），但这会留下父规格 D2.3 明确要禁止的「已改写成功、name 写失败」同类不一致态，不推荐。

### D4 `delete_model` / `delete_scheme`

```python
def delete_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    *,
    confirm_shared: bool,
    log_fn: Callable[..., None] = print,
) -> ServiceResult

def delete_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    scheme_root: str | Path,
    *,
    confirm_shared: bool,
    log_fn: Callable[..., None] = print,
) -> ServiceResult
```

`confirm_shared` 由 UI 在展示反查结果后传入（父规格「用户选择一律发生在锁外」——反查预览本身是只读操作，**不需要持锁**，可在锁外调用 `find_references_to` 做预览；提交删除时锁内重验一次，见下方锁内步骤 2）。

**锁外预览**（UI 调用，不在本服务函数内）：调用 `find_references_to(configured_root, workspace_root, target, "model" | "scheme")` 展示 `hits`；按父规格 D2.4「必须勾选」条件限定为**跨 owner 的 shared 命中**——即 `hit.owner_root != str(target)`（型号删除）或方案删除时 `hit.owner_root` 对应型号根之外的 shared 命中。本型号/方案自身 `defaults` 随目录删除不算需要确认的外部借用。

**锁外（MSC-001 修订）**：

1. gate；`target_kind == "model"` 时校验 `_has_model_marker(model_root)`；`target_kind == "scheme"` 时校验 `scheme_root.parent.name == "定制"`。均为纯读校验，失败直接返回错误码，不进入 `WorkspaceTransaction`。

**锁内步骤**（`WorkspaceTransaction(workspace_root, operation="delete_model")` / `"delete_scheme"`）：

2. **重验反查**（D3 第 7 条同款「锁内重验一次」）：重新调用 `find_references_to`；筛出跨 owner 的 shared 命中；若存在且 `confirm_shared` 不为 `True` → **先 `transaction.commit()`（此时未 `begin_product_write`，`products` 为空，安全空提交）再**返回 `confirmation_required`，payload 携带最新 `hits`（不阻断，只是要求前端拿到最新清单后重新提交一次 `confirm_shared=True`——这是父规格 D1.8「TOCTOU 覆盖确认」同款模式的删除版，但删除的工程尺度已裁决「不做细粒度 TOCTOU 重验」，故这里只做**一次**锁内重验，不做「记录锁外预览指纹再比对」的额外校验，比 D1.8 更简化）。**MSC-001 修订**：r1 在此直接 `return` 会因未提交而落 `recovery_required`，把一次普通的「需要用户二次确认」变成工作区阻写故障，故补 `commit()`。
3. 反查 `issues` 含阻断级（`is_blocking_issue`）→ 同上先 `commit()`（`products` 仍为空）再返回 `lookup_blocked`（**MSC-001 修订**，理由同步骤 2）。
4. `transaction.begin_product_write()`。
5. `register_delete(workspace_root, target)`（`quarantine.register_delete`，**调用前已持锁**，满足模块要求）；该调用内部完成「移入隔离区」，等价于父规格步骤 3。
6. **零 TOML 改写**（父规格步骤 4）——不调用任何 R8 apply，借用条目保留、解析自然 missing。
7. 索引：`bulk_reindex_subtree(str(workspace_root), str(target), [])`（父规格步骤 5，**不是 `delete_asset`**）。索引失败 → 保留磁盘结果（已移入隔离区）并提示重扫，不回滚隔离动作。
8. `commit()`。

**成功 payload**：`{"quarantine_record_id": record["id"], "original_path": str(target)}`（`record` 为 `register_delete` 返回值）。

**撤销**（`undo_delete_model_scheme`，复用 `quarantine.undo_delete`，本任务只加一层索引对账）：

```python
def undo_model_scheme_delete(
    workspace_root: str | Path,
    record_id: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult
```

锁内（`WorkspaceTransaction(workspace_root, operation="undo_model_scheme_delete")`；`quarantine.undo_delete` 只校验 `workspace_lock_is_held`，不自带事务，本函数需自行包一层）：

1. **`transaction.begin_product_write()`（MSC-005 修订，先于调用 `quarantine.undo_delete`）**：`undo_delete` 会执行 `os.replace(quarantine_path, original)`（`quarantine.py:459`，把隔离内容移回工作区）并改写隔离清单（`:464` `_save_quarantine_manifest`），这是实打实的产品数据变更；r2 全程不启动 seqlock，整段撤销过程 generation 停在偶数，违背 D8「首次产品变更前置 odd generation」——撤销途中崩溃时，恢复流程看到偶数 generation 会误判「没有进行中的写操作」，而磁盘可能已移动一半。`begin_product_write()` 之后 `record_product(original_path, manifest)` 记录目标路径。

   **`manifest` 实参（统筹补充，r3 遗留项收口）**：`record_product` 只校验路径在工作区内、不要求路径已存在（`workspace_transaction.py:519-526`），所以「移动前先登记目标路径」本身合法，与既有调用方同序——`staging_io.py:156` 与 `import_io.py:272` 都是先 `record_product` 后写盘。但既有调用方的 `manifest` 均取自**源侧**：`staging_io.py:153` 用 `manifest_hash(directory_manifest(area))`，在 `os.replace` 之前对 staging 源目录算；`import_io.py:270` 建空目录用 `manifest_hash([])`。本处照此取源侧，即**在调用 `undo_delete` 之前对隔离侧 `record["quarantine_path"]` 计算** `manifest_hash(directory_manifest(quarantine_path))`——移回后内容与该 hash 一致，D1.4c「删除本次产物」据此校验身份。

   **注意**：`directory_manifest` 只接受目录（`manifest.py:61` 非目录即抛 `ManifestError`）。型号/方案的隔离产物一定是目录，本子任务成立；但 `ManifestError` 仍须捕获并转为 `undo_failed`，不得让裸异常穿透到 service 边界（AGENTS.md 要求）。

   **MSC-008 修订（事务收束）**：该 `ManifestError` 分支发生在 `begin_product_write()` 之后、`record_product` 与 `undo_delete` 之前——**零产物**（隔离内容尚未移动、清单未改写）。按本规格自身的零产物空提交契约，须**先 `transaction.commit()` 收束为 `clean`，再返回 `undo_failed`**，否则一次纯读的 hash 计算失败会把工作区打入 `recovery_required`，与 MSC-001 要根除的正是同一类故障。测试计划须含该断言（失败后 `WorkspaceState` 为 `clean`、隔离记录仍在、可重试撤销）。
2. `quarantine.undo_delete(workspace_root, record_id)`：
   - **成功** → 对 `original_path` 的**父目录**（型号删除是 `workspace_root`，方案删除是 `型号根/定制`）调用 `reconcile_subtree`，随后 `commit()`（此时 `products` 非空，`commit()` 把 generation 由奇推回偶、状态回 `clean`，是正常收尾）。
   - **`UndoConflictError`**（目标被占用；`undo_delete` 内部占用检查在 `:454-457`，早于 `:459` 的 `os.replace`，确认该分支下 `undo_delete` 未移动任何文件、隔离清单未改写，是零产物分支）→ **直接 `transaction.commit()`（不回退 `begin_product_write()`）再**透传 `undo_conflict`。**MSC-005 结论**：此时 generation 已因步骤 1 被推为奇数，但 `commit()`（`workspace_transaction.py:584-591`）的逻辑是「若 generation 为奇则递增为偶，写 `clean`，清操作日志」，对奇偶两种起始状态都正确收敛——不依赖「调用 `commit()` 时事务是否真的产生了磁盘变更」这一前提，只要 `commit()` 被调用时磁盘状态已经稳定（此处确认稳定：零产物），提交就是安全的。这与 MSC-001「零产物空提交安全」是同一条不变量的两个触发时机（未 `begin_product_write` vs. 已 `begin_product_write` 但确认零产物），不需要为此新增分支或回退 generation 的特殊逻辑，也不违背 MSC-001 的划线。
   - **其余失败**（窗口已过等，同样零产物）→ 同样直接 `commit()` 再保留目录并提示重扫（父规格「撤销失败则保留目录并提示重扫」）。

**空型号/空方案与最后一个型号**：`delete_model` 不因目录内零资产而拒绝（父规格「空型号与空方案保留显示…删除最后一个型号仍保留配置根」）；`workspace_root` 本身不受影响，`detect_workspace_layout` 在删除后重算会因型号计数变化而在 `multi_model`/`empty` 间转换，UI 层据此决定是否显示「空工作区，新增型号」入口——本服务不需要为「最后一个型号」写特殊分支，`delete_model` 的算法与「删除非最后一个型号」完全一致，D9.1 的「保留工作区根」是 `create_model`/`delete_model` 都不触碰 `workspace_root` 本身这一既有事实的自然结果，不需要额外代码路径。

## D5：机芯类型写入复核

- `create_model` 锁内步骤 3（staging 骨架搭建）是机芯类型的**唯一写入点**——`平台配置.toml` 单块、`defaults={}`（D5.1/D5.2）。
- **型号建成后机芯类型不可改**：本服务**不提供** `update_chassis_type` 之类入口；`平台配置.toml` 后续只被 D1.7（设为默认，子任务 5a）与 D5.5（存量归一，子任务 7）触碰，均不在本子任务范围。
- `ensure_platform_blocks` **不被本服务调用**——`create_model` 直接写规范单块，不经过任何「按方案 platform 名扩块」的路径，满足父规格「正式 CRUD 入口不得再从方案 platform 扩充 block」（D5.3）。

## D9：布局前置与验收断言复核

### 布局前置矩阵

| 入口 | `single_model` | `invalid` | `empty` | `multi_model` |
| --- | --- | --- | --- | --- |
| `create_model` | `migration_required` | `layout_invalid` | 允许（创建首个型号） | 允许 |
| `rename_model` | `migration_required`（**MSC-002 修订**：由 `_rename_target` 自身的 `detect_workspace_layout` 检查在调用 `build_rewrite_plan` 之前直接拦截，不进入 R8 根重命名路径；与 `create_model`/`delete_model` 口径统一，不再使用 `root_rename_unsupported`） | `layout_invalid` | 不适用（无型号可改） | 允许 |
| `delete_model` | `migration_required`（父规格「方案与程序的既有操作继续兼容旧布局」不含型号级删除——但 `single_model` 下 `model_root` 就是 `workspace_root`，删除会清空整个工作区根，属破坏性歧义操作——**本服务显式拒绝**：`delete_model` 增加检查「`model_root` 与 `workspace_root` 身份相同时返回 `migration_required`」，理由见下，**MSC-002 修订**：与 `create_model`/`rename_model` 口径统一为同一错误码） | `layout_invalid` | 不适用 | 允许 |
| `create_scheme` / `rename_scheme` / `delete_scheme` | 允许（不受型号级布局限制，型号根已知即可操作） | 不适用（方案操作以 `model_root` 为起点，不重新判定整个工作区布局） | 不适用 | 允许 |

**`delete_model` 拒绝旧布局根删除的理由**：父规格 D9.2 只说「方案与程序的既有操作继续兼容旧布局」，未提及型号级删除；而 `enumerate_model_roots` 在 `single_model` 布局下把工作区根本身当作唯一型号根（`reference_lookup.py:162-163`）。若放行，`delete_model` 会把整个配置根移入隔离区，`workspace_root` 配置项仍指向一个已清空的路径，后续任何操作都会因 `not_configured`/路径不存在而失败——这不是「保留工作区根」的 D9.1 语义（D9.1 假定的是 `multi_model` 下型号是工作区的**子目录**）。因此在 `single_model` 布局下，型号级删除统一提示 `migration_required`，与 `create_model`/`rename_model` 口径一致。

### D9 验收断言清单（对应父规格第 762 行四条 + `no_other_model`）

```python
# 1. 零型号工作区可新增
def test_create_model_in_empty_workspace(tmp_ws):
    assert detect_workspace_layout(tmp_ws) == "empty"
    result = create_model(str(tmp_ws), str(tmp_ws), "L99程序", "单3D")
    assert result["ok"] is True
    assert detect_workspace_layout(tmp_ws) == "multi_model"

# 2. 空白型号在零资产及重启后仍可见（重启用「重新枚举型号根」模拟，
#    不依赖资产索引——enumerate_model_roots 只看目录/配置标志）
def test_blank_model_visible_without_assets(tmp_ws):
    create_model(str(tmp_ws), str(tmp_ws), "L99程序", "单3D")
    roots = enumerate_model_roots(tmp_ws)
    assert len(roots) == 1
    # 索引侧零资产不影响型号列表来源（型号列表用目录枚举，非索引查询）
    # 此断言只验证 enumerate_model_roots 不依赖 asset_index，索引查询由
    # 子任务 8 的型号列表 ViewModel 验证。

# 3. 删除最后一个型号保留配置根且可撤销
def test_delete_last_model_keeps_root_and_undoable(tmp_ws):
    create_model(str(tmp_ws), str(tmp_ws), "L99程序", "单3D")
    model_root = tmp_ws / "L99程序"
    result = delete_model(str(tmp_ws), str(tmp_ws), model_root, confirm_shared=False)
    assert result["ok"] is True
    assert tmp_ws.is_dir()  # 配置根仍在
    assert detect_workspace_layout(tmp_ws) == "empty"
    record_id = result["payload"]["quarantine_record_id"]
    undo = undo_model_scheme_delete(str(tmp_ws), record_id)
    assert undo["ok"] is True
    assert model_root.is_dir()
    assert detect_workspace_layout(tmp_ws) == "multi_model"

# 4. multi_model 布局下仅剩一个型号时也可新增、重命名、删除
#    （MSC-004 改名：原名 test_single_model_workspace_allows_full_crud 与
#    父规格 D9.0 的 single_model——专指旧布局——术语冲突；这里的
#    "只有一个型号" 描述的是 multi_model 布局下的型号计数，不是布局种类）
def test_multi_model_workspace_with_single_model_allows_full_crud(tmp_ws):
    create_model(str(tmp_ws), str(tmp_ws), "L99程序", "单3D")
    # 此时工作区恰好只有一个型号（multi_model 布局，计数为 1）——
    # MSC-004 补充：先验证仅一个型号时 rename 本身即可成功，不依赖
    # 后续新增第二个型号才能改名
    assert detect_workspace_layout(tmp_ws) == "multi_model"
    rn0 = rename_model(str(tmp_ws), str(tmp_ws), tmp_ws / "L99程序", "L99预改名")
    assert rn0["ok"] is True
    assert (tmp_ws / "L99预改名").is_dir()
    # 新增第二个
    r2 = create_model(str(tmp_ws), str(tmp_ws), "L100程序", "单2D")
    assert r2["ok"] is True
    # 重命名第一个（两个型号时再次改名，验证不受型号数量影响）
    rn = rename_model(str(tmp_ws), str(tmp_ws), tmp_ws / "L99预改名", "L99改名")
    assert rn["ok"] is True
    # 删除第二个
    dl = delete_model(str(tmp_ws), str(tmp_ws), tmp_ws / "L100程序", confirm_shared=False)
    assert dl["ok"] is True

# 5. 借用 no_other_model（D9.2a，跨子任务但本任务的 delete_model 是触发前提，
#    实际借用校验在子任务 5a shared_module_service；MSC-004 修订：原断言用
#    单型号直接枚举，没有覆盖 no_other_model 的实际触发前提——多型号场景下
#    删除来源型号后，借入方发现无其他来源。改为：创建两个型号（L99 作为
#    借入方、L100 作为潜在来源），删除 L100 后验证排除借入方自身的
#    enumerate_model_roots 结果为空，这才是子任务 5a 借用服务实际会遇到的前提）
def test_no_other_model_precondition(tmp_ws):
    create_model(str(tmp_ws), str(tmp_ws), "L99程序", "单3D")  # 借入方
    create_model(str(tmp_ws), str(tmp_ws), "L100程序", "单2D")  # 潜在来源
    dl = delete_model(str(tmp_ws), str(tmp_ws), tmp_ws / "L100程序", confirm_shared=False)
    assert dl["ok"] is True
    roots = [r for r in enumerate_model_roots(tmp_ws) if r.name != "L99程序"]
    assert roots == []  # 来源型号已删除，子任务 5a 的借用服务据此返回 no_other_model
```

## 测试计划

新增 `src/fwasset/tests/test_model_scheme_service.py`，覆盖：

**create_model**：
- `empty` 工作区成功创建，`model_id` 正确 slugify；
- `model_id` 冲突时自动去重（两个中文名 slugify 后相同）；
- `single_model` → `migration_required`；`invalid` → `layout_invalid`；
- 非法机芯类型 → `invalid_chassis_type`；
- D3 各校验码透传（`out_of_workspace`、`path_exists`、`invalid_name`、`path_identity_conflict`——复用 `test_admission.py` 已验证的底层逻辑，本层只测「服务正确调用了 `validate_new_path` 并透传错误码」，不重复 admission 的全部用例）。**MSC-017 新增**：`out_of_workspace` 需覆盖「型号级入口（工作区外绝对路径）」与「方案级入口（`..` 词法逃逸）」两条，断言零写（目标未创建）且 `load_workspace_status(tmp_ws).state == "clean"`；
- staging 提升失败（模拟 `_assert_promotable_target` 拒绝：提升前手工在目标位置创建同名文件）→ `promote_failed`，工作区无残留（型号目录不存在）；
- 索引写入失败（mock `bulk_reindex_subtree` 抛异常）→ `ok=True, code="index_pending"`，磁盘侧型号已存在。

**create_scheme**：
- 成功创建，`方案配置.toml` 内容为 `name = "..."`（不含 `platform`）；
- 目标非型号根 → `invalid_target`；
- 已存在同名空目录 → `path_exists`（不复用）。

**rename_model / rename_scheme**：
- 成功改名，型号根 `型号配置.toml` 随目录移动内容不变（`model_id` 不变）；
- 方案改名后 `方案配置.toml` 的 `name` 字段与新目录名一致；
- 存在 `shared_static` 借用命中重命名对象时，R8 plan 正确改写借用方配置；
- 模拟 `apply_rewrite_plan` 返回 `rollback_conflict`（构造并发修改场景）→ 返回 `rename_inconsistent`，目录停留在新路径，原目录不存在（因为回移被主动跳过）；
- 模拟 `apply_rewrite_plan` 完整回滚成功 → 目录移回原路径，返回 `rewrite_failed`；
- `os.replace` 移动目录本身失败（mock）→ `rename_failed`，配置文件未被 apply（用「plan 未被调用」或「apply_rewrite_plan mock 未触发」断言）；
- 索引对账边界正确：型号改名后对 `workspace_root` 调用 `reconcile_subtree`；方案改名后对 `定制/` 调用；
- **MSC-006 新增**：锁外校验通过后、锁内重验前构造并发占用（新路径在锁外校验之后、`begin_product_write` 之前被第三方创建同名目标）→ 锁内重验触发 `path_exists`，返回该错误码前 `load_workspace_status(tmp_ws).state == "clean"`（未落 `recovery_required`），且 `os.replace`（移动目录本身）未被调用。

**delete_model / delete_scheme / undo**：
- 无外部借用时 `confirm_shared=False` 直接成功；
- 存在跨 owner shared 命中且 `confirm_shared=False` → `confirmation_required`；传 `True` 后成功；
- 存在阻断级 issue（如损坏的借入方 `型号配置.toml`）→ `lookup_blocked`；
- 删除后 5 秒内撤销成功，目录内容与撤销前一致（用 `directory_manifest_hash` 比对）；
- 撤销窗口外撤销 → `undo_failed`，且 `WorkspaceState` 收束为 `clean`（MSC-008：零产物分支不得留 `recovery_required`）；
- 隔离侧 `directory_manifest` 抛 `ManifestError` → `undo_failed`，`WorkspaceState` 为 `clean`、隔离记录仍在、可重试撤销（MSC-008）；
- 撤销成功与 `UndoConflictError` 两条路径的 generation 收束断言（成功：奇→偶 + `clean`；冲突：已进入奇数后 `commit()` 仍收敛为偶数 + `clean`，隔离内容保留）；
- 撤销时原路径被占用（撤销前在原位置新建同名目录）→ `undo_conflict`，隔离内容保留；
- `single_model` 布局下 `delete_model(model_root == workspace_root)` → `migration_required`；
- **MSC-005 新增**：撤销成功路径断言撤销过程中 `begin_product_write()` 被调用（可通过 mock 或检查中间 generation 状态验证曾经历奇数 generation，而非全程停在偶数）；`UndoConflictError` 分支断言 `commit()` 后 `load_workspace_status(tmp_ws)` 为 `(state="clean", generation` 为偶数`)`，即使撤销过程已启动过 `begin_product_write()`。

**D9 验收**：上一节列出的 5 个断言函数直接收入本测试文件。

**MSC-001 回归**：对 `create_model`/`create_scheme`（重名触发 `path_exists`）、`rename_model`/`rename_scheme`（新路径非法触发 `invalid_name`）、`delete_model`（跨 owner shared 命中且 `confirm_shared=False` 触发 `confirmation_required`）各构造一次校验失败，断言失败返回后 `load_workspace_status(tmp_ws).state == "clean"`（不是 `recovery_required`）——防止 r1 的「校验失败即坠入恢复态」回归。

**中断恢复（阶段边界测试，非逐行崩溃注入）**：
- 模拟 `create_model` 在 `begin_product_write()` 之后、`promote_staging` 之前进程退出（不调用 `commit()`，模拟异常路径）→ 下次 `WorkspaceTransaction.__enter__` 检测到 `recovery_required`，验证型号目录未创建（staging 会话可能残留，属已知可清理的孤儿，不在本轮验收范围——`cleanup_staging_area` 的批量清理属子任务 1 已交付的维护职责）；
- 模拟 `promote_staging` 成功、`bulk_reindex_subtree` 之前退出（不 `commit`）→ 验证磁盘型号目录存在（内容完整），`recovery_required` 状态下重新走一次 `bulk_reindex_subtree(..., [])` 后型号在索引中可查（此断言验证「补一次索引对账即可收敛」，不要求实现自动恢复流程本身——自动恢复编排属子任务 8）；
- `rename_model` 在 `apply_rewrite_plan` 返回 `rollback_conflict` 后验证 `WorkspaceState` 落为 `recovery_required`（不由 `commit()` 清除）。

## 失败码汇总

| 错误码 | 触发点 | 说明 |
| --- | --- | --- |
| `not_configured` / `root_changed` | 所有入口 | `check_reference_gate` |
| `migration_required` | 型号级入口遇 `single_model` | 提示手动整理目录 |
| `layout_invalid` | 型号级入口遇 `invalid` | 工作区存在无法归类内容 |
| `invalid_chassis_type` | `create_model` | 非枚举值 |
| `invalid_target` | `create_scheme`/`rename_scheme`/`delete_scheme` | 目标不是型号根/方案目录 |
| `out_of_workspace` / `path_exists` / `invalid_name` / `domain_violation` / `path_excluded` / `workspace_excluded` / `path_identity_conflict` | 全部创建/重命名入口 | 透传 `admission.AdmissionError.code`（**MSC-017 补登**：`out_of_workspace` 由 `validate_new_path` 的**第一步** `assert_within_workspace` 抛出，先于其余检查；零产物，`commit()` 收敛为 `clean`，与 `model_id_service` / `platform_default_service` / `shared_module_service` 的同码口径一致） |
| `staging_unavailable` | 创建入口 | staging 分配失败 |
| `promote_failed` | 创建入口 | 原子提升失败，staging 已清理 |
| `index_pending` | 创建/重命名/删除入口 | `ok=True`，索引写入失败需重扫 |
| `unsupported_semantic_change` / `lookup_blocked` / `canonical_conflict` / `invalid_request` / `invalid_operation` | `rename_*` | `build_rewrite_plan` 透传 |
| `rename_failed` | `rename_*` | `os.replace` 移动目录本身失败（apply 之前） |
| `rewrite_failed` | `rename_*` | apply 完整回滚成功 |
| `rename_inconsistent` | `rename_*` | apply `rollback_conflict` 或回移失败 |
| `stale_plan` | `rename_*` | 并发修改 |
| `confirmation_required` | `delete_*` | 存在跨 owner shared 命中且未确认 |
| `quarantine_failed` | `delete_*` | `register_delete` 抛 `QuarantineError`（MSC-014 修订）：判据是**原目标路径是否仍在原位**——移动前失败（manifest 计算失败、`assert_within_workspace`/`assert_managed_write` 拒绝）→ 原路径仍存在，零产物，`commit()` 收敛为 `clean` 再返回，**可重试**；移动后失败（`os.replace` 成功之后，含清单转正写入失败）→ 原路径已消失，非零产物半成品，不 `commit()`，`with` 块以未提交状态退出，`__exit__` 落 `recovery_required`，由 `_delete_target` 外层 `try/except QuarantineError` 捕获转 `ServiceResult`（`payload.recovery_required = True`），**需人工恢复** |
| `undo_conflict` | `undo_model_scheme_delete` | 撤销目标被占用 |
| `undo_failed` | `undo_model_scheme_delete` | 三类触发点，均需先收束再返回：① 隔离侧 manifest 计算失败（`ManifestError`，MSC-008，零产物，`commit()` 为 `clean`）；② `undo_delete` 移动前失败（窗口已过等，零产物，`commit()` 为 `clean`）；③ **`load_quarantine_manifest` 纯读失败（`QuarantineError`，MSC-015 修订）**——发生在 `begin_product_write()` 之前，与移动无关，须在事务内单独捕获并 `commit()` 为 `clean`，消息不含「已移动」字样，不带 `recovery_required` payload，与移动后失败（同码但走外层 `except`、`payload.recovery_required = True`，见 docstring MSC-009）严格区分 |
| `workspace_busy` | 全部入口 | 锁获取超时（`WorkspaceLock`） |
| `workspace_recovery_required` | 全部入口 | 工作区存在待恢复现场 |

## DoD

- [x] `model_scheme_service.py` 实现 `create_model` / `create_scheme` / `rename_model` / `rename_scheme` / `delete_model` / `delete_scheme` / `undo_model_scheme_delete`，签名与本规格一致。
- [x] `scheme_config.py` 新增 `serialize_scheme_config` / `save_scheme_config`。
- [x] `reference_service.py` 新增 `scheme_config` 计划条目类型（kind 白名单 + 文件名映射 + `_assemble_plan` own_moved 分支），`_plan_token` 保持私有。
- [x] **MSC-007 新增**：`workspace_transaction.py` 的 `WorkspaceTransaction.commit()` docstring 补充公开契约——明述「未 `begin_product_write()`（或已调用但确认零产物）时的空提交是受支持的正常退出路径，会将 generation 收敛为偶数、状态写回 `clean`、清空操作日志」；`test_workspace_transaction.py` 补一条单测锁定该行为（分别覆盖「从未 `begin_product_write`」与「`begin_product_write` 后确认零产物」两种起始 generation）。本规格 D1/D2 准入失败分支、D4 步骤 2/3、`undo_model_scheme_delete` 的 `UndoConflictError` 分支均依赖此契约。
- [x] D9 验收 5 条断言全部通过。
- [x] 测试计划全部用例落地，`uv run python -m pytest -q` 通过，覆盖率不低于 `pyproject.toml` 门槛。
- [x] `uv run ruff check src scripts` / `uv run mypy` 通过。
- [x] 人工验证：UI 尚未接入（子任务 8），本子任务按流程文档「无 UI 可操作」用独立临时目录场景脚本验证 D9 四条 + 撤销 + 重命名回滚，记录命令与结果。
