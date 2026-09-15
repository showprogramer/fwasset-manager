# TASK-20260915-atomic-dir-primitives：staging 提升与删除产物原语

状态：已完成

## 目标与规则

本任务是 [CRUD 写语义](TASK-20260903-crud-write-semantics.md) 子任务 1 的收尾切片（继 TASK-20260905 受管路径与布局、TASK-20260915 事务状态基础之后）：交付后续导入（子任务 3）、程序新增/更新（子任务 5/6）共同依赖的「原子目录原语」——目录 manifest（D4.2 hash 定义）、staging 分配与原子提升（D7.1）、操作日志负载扩展（D8.3 数据形状）与「删除本次产物」原语（D1.4c）。隔离区两类记录与撤销（D2.5）归下一片。

- **目录 manifest（D4.2）**：`core/manifest.py` 提供 `FileEntry`（相对路径、字节数、内容 SHA-256）、`directory_manifest(root, *, exclude_names)` 与 `manifest_hash(entries)`、`directory_manifest_hash(...)`。相对路径经 `normcase` + 正斜杠归一并按字典序排序；**不含 mtime**；空目录不产生条目；空树的哈希为空输入的 SHA-256。树内出现任何 reparse point（junction / 符号链接，目录或文件）→ 抛领域异常，不静默跳过。`exclude_names` 按文件名精确（normcase）排除，留给子任务 6 的副本元数据；`程序信息.toml` 是资产内容，**默认不排除**。
- **操作日志负载（D8.3 数据形状）**：`operation.json` 扩展为 `OperationLog`（真源 `core/types.py`，TypedDict）：`operation: str`、`phase: str`、`started_at: float`、`products: list[OperationProduct]`（`path: str` + `manifest: str`）、`details: dict[str, Any]`（各操作自定义负载，如 plan 摘要与旧/新路径）。**旧格式（无 `products` / `details`）必须可读**，缺省为空列表 / 空字典。`WorkspaceTransaction` 新增：
  - `set_phase(phase, *, details=None)`——更新 phase 并把 `details` 深合并入日志（原子替换，沿用共享冲突重试路径）；
  - `record_product(path, manifest)`——向 `products` 追加条目，同路径重复记录时覆盖 manifest（幂等）；
  - `products` 只读属性（内存镜像，与落盘一致）；
  - 两者均要求事务已进入且未提交，否则抛 `WorkspaceTransactionError`。
  - 新增模块级 `load_operation_log(workspace_root) -> OperationLog | None`：严格读取并校验日志形状（含旧格式缺省）；**仅文件缺失返回 `None`**，存在但为空对象或形状无效一律抛 `WorkspaceRecoveryRequiredError`。
  - `record_product` 要求产物路径为位于本事务工作区内的绝对路径（`assert_within_workspace`，否则拒绝记录）——产物身份是删除原语的受信来源，不得登记工作区外或相对路径。
  - staging 分配/提升/产物删除统一绑定事务所属工作区（`workspace_root` 只读属性 + 规范化比较）：事务 A 不得操作工作区 B 的 staging 或产物，否则状态与记录会落在错误的工作区。
  - 提升与清理拒绝 staging 根本身：会话目录必须是 staging 根的直接子目录，根及其嵌套路径一律拒绝（防止搬走/删掉所有权标记与其他会话）。
  - 清理拒绝 reparse 点会话根：会话目录在遍历前若是链接或重定向路径直接拒绝，避免跟随链接删除 staging 根内容；遍历中再次复核，失守即中止。
  - 删除动作在逐文件 `unlink` 前逐段复核路径链（不跟随 reparse point）：校验后子目录若被换成 junction，立即中止并抛 `delete_failed`，不跟随链接删除树外文件；目录树遍历中同样复核。
- **staging 分配（D7.1）**：`core/staging_io.py` 的 `allocate_staging_area(workspace_root, transaction)` 在 `.fwasset/staging/<uuid.hex>/` 下创建会话目录。前置：进程内已持有该工作区锁（`workspace_transaction` 新增只读判定 `workspace_lock_is_held(workspace_root) -> bool`）且事务已进入未提交；目标路径经 `assert_managed_write(expect="staging")`；路径由应用生成，不接受用户输入。
- **原子提升（D7.1）**：`promote_staging(transaction, workspace_root, staging_area, target)`，前置全部满足才动手：
  1. 事务已进入且未提交，且 `begin_product_write` 已调用（**generation 为奇数**——落盘 D8 不变量①「产品数据变更前必须先进入非 clean 状态」）；
  2. `staging_area` 经 `assert_managed_write(expect="staging")` 且实际存在；
  3. `target` 为绝对路径、`assert_within_workspace` 通过、**不落在 `.fwasset` 容器或任一内部受管根内**（`managed_path_reason(..., workspace_root=...)` 判定；`旧版本/` 属业务区域，允许）、父目录存在且为目录；
  4. `target` 不存在——对 `target.parent` 逐项 `normcase` 精确比较（覆盖大小写变体），命中 → 冲突错误（`path_exists` 语义）。
  落盘顺序：**先 `record_product(target, manifest)` 后 `os.replace`**——崩溃夹在两者之间只会留下「日志多于现场」（恢复时按缺失处理）；反向顺序会留下无日志的孤儿产物。`os.replace` 失败（目标被抢占）→ 冲突错误，staging 原样保留。
- **「删除本次产物」原语（D1.4c）**：`delete_recorded_product(transaction, workspace_root, target)` 三重校验全部通过才动手：
  1. **路径身份**：`target`（normcase）出现在事务日志 `products` 中；
  2. **日志状态**：事务已进入且未提交（清理只发生在失败回滚期）；
  3. **内容**：`target` 现存时，其 `directory_manifest_hash` 等于记录值；`target` 不存在 → 视为已清理，直接成功返回。

  删除动作**逐文件 + 自底向上 rmdir**，只删 manifest 记录过的文件；发现计划外文件、记录文件缺失、`unlink`/`rmdir` 失败 → 立即中止、**保留剩余现场**、抛 `ProductCleanupConflict`（字段：`reason ∈ {"identity", "log_state", "manifest", "delete_failed"}`、`target`、期望/实际哈希与说明文本，中文消息）。**禁止 `shutil.rmtree` 式盲目递归删除**。service 层后续把冲突映射为 `update_inconsistent` 并附恢复材料。
- **staging 清理**：`cleanup_staging_area(workspace_root, staging_area)` 删除整个 staging 会话目录（同样逐文件 + rmdir，`assert_managed_write` 限定在 staging 根内；不要求事务存活，供失败与恢复路径共用）。失败即中止并保留剩余内容。
- 领域异常统一挂在 `WorkspaceTransactionError` 家族下（`manifest.py` / `staging_io.py` 各自定义子类），消息使用中文；service 映射留后续接入片。
- **不做**：D3 完整准入（父目录领域归属、dangling anchor、Windows 保留名——子任务 3）、受管候选区、隔离区（下一片）、真实业务操作接入、UI。

## 验收清单

- [x] manifest 稳定：同内容不同路径、不同创建顺序、改 mtime 哈希不变；`exclude_names` 生效；树内 junction/符号链接抛领域异常；空树有确定哈希；嵌套目录与空子目录行为正确。
- [x] 操作日志负载按 `OperationLog` 落盘且旧格式可读；`set_phase` / `record_product` 原子更新，事务外或提交后调用被拒；`record_product` 拒绝工作区外/相对路径；`load_operation_log` 仅缺失返回 `None`，空对象与无效形状被拒。
- [x] staging 分配在持锁事务内成功；未持锁、事务未进入、跨工作区时被拒。
- [x] 提升满足全部前置与「先记录后替换」顺序；目标已存在（含大小写变体）、越界、落入 `.fwasset`/受管根、父目录缺失、未过 `begin_product_write`、staging 根本身、跨工作区一律拒绝且 staging 完好。
- [x] `delete_recorded_product`：三重校验通过后逐文件删除并清空目录树；未记录路径（`identity`，含工作区外伪造目标）、内容已变（`manifest`）、事务已提交（`log_state`）、计划外文件与删除受阻（`delete_failed`，含校验后 junction 替换）均保留现场并抛可诊断冲突；目标已不存在时幂等成功。
- [x] `cleanup_staging_area` 完整清理且拒绝 staging 根外路径、staging 根本身与 reparse 点会话根。
- [x] 既有 15 个事务测试与 `scripts\verify_workspace_transaction.py` 场景保持通过；新增 27 个测试先红后绿。
- [x] `uv run ruff check src scripts`、`uv run mypy`、`uv run python -m pytest -q` 全部通过。
- [x] Codex 独立审查四轮：P2×4 与 P1×2 全部复现—修复—回归（staging 根会话、工作区绑定、junction 会话清理、空日志、工作区外产物登记、校验后 junction 替换），终轮结论 "No blocking issues found"。

## 验证

- 自动化：`uv run python -m pytest src\fwasset\tests\test_manifest.py src\fwasset\tests\test_staging_io.py src\fwasset\tests\test_workspace_transaction.py -q --no-cov`（42 passed）；`uv run ruff check src scripts`、`uv run mypy`（43 文件无问题）、`uv run python -m pytest -q`（688 passed、1 skipped、覆盖率 94.50%）。
- 隔离场景：`scripts\verify_workspace_transaction.py` 六场景全部断言通过（含场景 6：事务内 staging 分配 → 提升 → 记录 → `delete_recorded_product` 清理）；无 UI，按无 UI 等效规则以脚本关闭。
- Codex 审查：四轮独立审查，终轮 "No blocking issues found"。

## 待决事项

- 副本元数据文件名与 `exclude_names` 的正式取值（子任务 6 定稿）；受管候选区与隔离区物理细节（下一片）；`details` 内 plan 摘要的结构由各业务操作片定义。
