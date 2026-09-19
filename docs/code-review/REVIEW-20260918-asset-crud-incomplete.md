# REVIEW-20260918-asset-crud-incomplete：程序新增 / 删除 / 待补齐

状态：已收口（r5：ACI-001～008 及复核新增 002a/002b/003a/003b/008a 共 13 项全部修复并通过复核；最终检查 ruff / mypy / pytest 全量通过，验证记录见 `specs/active/TASK-20260918-asset-crud-incomplete.md`）

范围：`2446969..68478b0`（r2 初审）；r3 复核 = `68478b0` → 当前工作区未提交差异 + 受影响调用链。审查覆盖子任务 5 的服务契约、候选区生命周期、事务收敛、隔离撤销与索引对账。r4 复核（本次）= r3 快照 → 当前差异中的 ACI-003（promote_candidate）与 ACI-008 两处 + promote_candidate 受影响调用链（classify_staged_content / scan_incomplete_imports / save_vendor）；新发现的三处缺陷均经独立脚本实测复现。

## 问题

### ACI-001（严重）新增成功后清空索引边界

- 定位：`src/fwasset/core/services/asset_service.py:202-208`（r2）
- 触发：`create_asset` 完成正式提升后执行索引同步。
- 影响：调用 `bulk_reindex_subtree(..., assets=[])` 会删除目标边界内的资产行且不插入新资产；服务仍返回成功。与规格 A2 第 7 步要求的 `reconcile_subtree` 相反。
- 处理结论（r3 已修复）：`asset_service.py:222` 改为 `reconcile_subtree(str(ws), str(destination))`（磁盘为真源，`asset_reconcile.py:33` 先扫描后整批写库），失败 → `ok=True, code="index_pending"`（`asset_service.py:223-226`）。回归断言 `test_asset_service.py:126-131` 确认创建后 `query_assets` 可查到新资产行；`test_asset_service.py:257` 的索引失败用例同步改打 `reconcile_subtree`。**通过**。

### ACI-002（严重）补充文件事务不满足失败零改动

- 定位：`asset_service.py:390-453,468-473`（r2）
- 触发：补充文件重名于本批其他来源、空来源、CAS 哈希异常、逐文件移动中途失败，或清除候选元数据失败。
- 影响：本批同名来源会在 staging 内静默覆盖；空来源会报告成功；CAS 哈希异常在零产品时把工作区留为 `recovery_required`；逐文件 `shutil.move` 可能只合入一部分文件；元数据清除失败仍返回 `complete=True`。这违反规格 A5 的"失败时候选保持原样"和事务收敛契约。
- 处理结论（r3 部分通过，修复引入两处新缺陷）：原触发条件已逐条落地——来源验证复用 `stage_import_files`（`asset_service.py:417`，空来源/批内 normcase 重名/受管来源/元数据跳过口径一致，`import_io.py:48-96`）；锁外记录 preimage（:409）；锁内 CAS 复验先于 `begin_product_write`（:462-470 在 :472 之前），`ManifestError`/CAS 失败零产物收敛 clean（`test_asset_service.py:1246`）；逐项 copy2 并跟踪（:478-485），补偿完整 → `promote_failed` + clean 可重试（`test_asset_service.py:572`），补偿失败 → 不 commit、`recovery_required`（`test_asset_service.py:1340`）；`clear_candidate_metadata` 非 ok → 回滚新增文件、恢复候选元数据、不报 `complete=True`（`test_asset_service.py:1304`）；禁覆盖按 normcase 同时查批内与已有名（:425-435）。修复引入的两处新缺陷（ACI-002a/002b）已在 r3.1 修复并通过回归。**通过**（含 r3.1）。

### ACI-002a（严重，r3 新增）补齐批次写入失败时 `meta_status` 未绑定异常穿透服务边界

- 定位：`src/fwasset/core/services/asset_service.py:494,506`
- 触发：本批补充使候选**变完整**（`matched is not None`），且逐文件 copy 在循环内失败（`write_failed` 在 :483 赋值，`meta_status` 从未在 :494 赋值）。
- 影响：`if matched is not None and meta_status == "ok"`（:506）抛 `UnboundLocalError` 裸异常穿透服务边界，违反「错误码必须经 ServiceResult 返回」契约（AGENTS.md）；事务未 commit，`__exit__` 落 `recovery_required`。已实测复现（rom+pkg 补齐批次、copy2 进入候选目录时失败）：返回前异常穿透，工作区 `recovery_required`，`fw.pkg` 半成品残留在候选目录。
- 处理结论（r3.1 已修复）：`asset_service.py:477-478` 先初始化 `meta_status`/`meta_err`，copy 失败分支不再引用未绑定变量；错误一律经 ServiceResult 返回。回归测试 `test_asset_service.py:1340`（补齐批次 copy 失败 → `promote_failed` + clean，不裸抛、无残留）。**通过**。

### ACI-002b（严重，r3 新增）copy 中途失败的目标半成品文件不在回滚清单内

- 定位：`src/fwasset/core/services/asset_service.py:478-485,498-505`
- 触发：`shutil.copy2` 在写入目标途中失败（磁盘满等）——目标文件已创建并写入部分字节后才抛 `OSError`。
- 影响：`added.append(destination)` 只在 copy2 成功后执行（:485）；中途失败的半成品目标文件不在 `added` 内，逆序 unlink 回滚（:501-505）不删它。结果：服务返回「候选保持原样，可重试」（clean 提交）但候选目录残留半成品文件。已实测复现两种形态：(1) 补 readme.txt（仍不完整）→ `promote_failed` + clean + `readme.txt` 半成品残留，消息宣称「候选保持原样」失实；(2) 补 fw.pkg（变完整）→ 与 ACI-002a 叠加，`UnboundLocalError` 穿透 + `recovery_required` + 残留。下次重试同批文件时残留半成品还会触发 `file_exists` 拒绝，用户无法自愈。
- 处理结论（r3.1 已修复）：copy 失败分支先 `destination.unlink(missing_ok=True)` 清理本次半成品目标（`asset_service.py:485-492`），清理失败同样纳入补偿判定；回归测试 `test_asset_service.py:1372`（copy 半成品失败 → 候选零残留 + clean + 同批重试不再被 `file_exists` 拒绝）。**通过**。

### ACI-003（严重）补齐完成的候选项没有可执行的提升路径

- 定位：`asset_service.py:447-460`、`import_io.py:208-217`、规格 A5/Q1。
- 触发：`supplement_candidate` 使候选内容完整。
- 影响：服务删除 `import_state` 后仍把目录留在 `.fwasset/incomplete`；候选扫描不再返回它，`supplement_candidate` / `delete_candidate` 也会拒绝它，而 `create_asset` 的导入原语禁止把受管候选区作为来源。"补齐完成，请选择放到哪里"没有对应服务入口，内容成为无法通过产品流程处理的孤儿。
- 处理结论（r4 复核：**不通过**，新发现 ACI-003a/003b 两处缺陷）：修复框架正确——锁外完成 gate / 布局 / 落点参数校验（`asset_service.py:612-640`，与 create_asset 同口径）；锁内先候选身份复验（:673）→ manifest CAS（:676-683，`ManifestError` 或哈希变化 → `stale_candidate` + clean，零产物收敛正确）→ `validate_new_path` 准入复验（:685-691，在 `begin_product_write` 之前，失败透传 AdmissionError code + clean）→ `record_product` + `_create_missing_containers` + `os.replace`（:694-712，目标已落盘 → 不 commit 落 recovery_required；未落盘 → clean 可重试，与子任务 4 判据一致）→ `reconcile_subtree` 对账、失败 `index_pending`（:727-733，与 ACI-001 同口径）。候选期元数据随目录整体移动、Q1 语义正确（vendor 保留）。回归测试断言行为而非实现细节（`test_asset_service.py:468/510/536/570`，实测通过）。但 A1 完整性判定的**语境**与 create 分流不一致（ACI-003a），且 `import_state` 一票否决门把 scan 报 `ready_to_promote=True` 的候选挡在唯一提升入口之外（ACI-003b）——提升正道目前仅对无 `dir_keywords` 门槛的类型（handcontrol_ui）且经 `supplement_candidate` 补齐的候选可用，ACI-003 的孤儿影响在其余场景仍存在。

### ACI-003a（严重，r4 复核新增）promote_candidate 的 A1 判定语境与 create 分流不一致，keyword 型内容永远判「不完整」

- 定位：`asset_service.py:658-665`（r4 新增块）。
- 触发：候选内容属于 `dir_keywords` 匹配型 catalog 条目（mainboard / 蓝牙 / 腿部 / 语音等，即除 handcontrol_ui 外的全部类型），且候选处于可提升状态（无 `import_state`，或已修复 003b 后到达 A1 门）。
- 影响：create 分流的 A1 判定在**业务落点**语境执行（`classify_staged_content(target, filenames)`，`asset_service.py:180`；`_match_catalog_type` 按 `dir_keywords` 匹配路径段，`file_scan.py:136`）；promote_candidate 却在**候选区路径**语境执行（:659 `classify_staged_content(candidate_path, content_files)`——路径段是 `.fwasset/incomplete/<id>`，不含任何 keyword）。同一份 `fw.bin` 在「主板」落点语境判 mainboard（完整），在候选语境判 None（不完整）→ `invalid_candidate` 拒绝。实测复现：`fw.bin` 创建时选「自定义模块」进候选区，提升到「主板」→ 拒绝，消息「候选内容仍不完整」失实（create 同语境判完整）。参数校验后 `target`（:633-640）即在作用域内，判定本可与 create 完全同口径——用户重新选择落点、内容在新落点语境判完整性，正是 D7.5「候选项不保存旧操作意图」的语义。现有 4 个回归测试全部使用手控模块 + rom/pkg（handcontrol_ui 分支不检查 `dir_keywords`，`file_scan.py:124-135`），故未暴露。
- 修复方向：:659 改为 `classify_staged_content(target, content_files)`，与 create 分流同语境；补一条 keyword 型内容（如 mainboard `.bin`）的提升回归测试。
- 处理结论（r5 已修复）：A1 判定改为落点语境 `classify_staged_content(target, content_files)`（`asset_service.py:657-660`，与 create 分流 `:180` 完全同口径）；docstring 同步修正（:604-612）。回归测试 `test_asset_service.py:613`（mainboard keyword 型内容提升到「主板」落点成功 + import_state 消失）。**通过**。
- 优先级 P1 / 置信度 0.95（已实测复现）。

### ACI-003b（严重，r4 复核新增）ready_to_promote 状态的候选被唯一提升入口拒绝，孤儿问题在手工补齐场景仍存在

- 定位：`asset_service.py:650-656`（r4 新增块）。
- 触发：用户手工向候选目录塞入缺失文件（绕过应用——规格 A4 明文场景，scan 诊断 `ready_to_promote=True`，`incomplete_scan.py:170`）。
- 影响：手工塞文件不会清除 `import_state`（Q1：该键只在 `supplement_candidate` 成功时删除），promote_candidate:650 对 `import_state == "incomplete"` 一票否决 → `invalid_candidate`。实测复现：rom-only 候选手工补 `fw.pkg` 后 scan 报 `ready_to_promote=True`，promote 拒绝，消息「候选内容仍不完整，请先补齐缺失文件再提升」——内容实际完整，消息失实；用户也无法自愈（重试同样被拒，`delete_candidate` 只能整目录删除丢内容）。规格 A4 的设计意图是「已完整（用户手工塞了文件）→ 诊断 ready_to_promote，不自动提升（**用户需显式操作**）」——用户显式操作的唯一入口就是 promote_candidate（ACI-003 修复的全部意义），但它恰好拒绝这种状态。ACI-003 的原始影响（「补齐完成，请选择放到哪里」没有对应服务入口，内容成为孤儿）在手工补齐场景完整保留。另：该状态门先于 A1 内容门执行（:650 在 :658 之前），两个门会给出互相矛盾的结论（scan 说可提升、promote 说不完整）。docstring :608「元数据仍标 `import_state == "incomplete"`」与 Q1 语义冲突，需同步修正。
- 修复方向：`import_state` 门不再一票否决——放行至 A1 完整性判定（与 ACI-003a 联动：target 语境判完整即可提升，不完整才拒绝并提示缺失项）；消息区分「内容仍不完整」与「候选状态异常」。补「手工塞齐 → scan ready_to_promote=True → promote 成功」回归测试。
- 处理结论（r5 已修复）：`import_state` 门取消一票否决，放行至 A1 判定（`asset_service.py:651-670`）——内容完整即提升，不完整才拒绝且消息区分两种状态；移动后若元数据仍带 `incomplete` 键则 `clear_candidate_metadata` 清除候选期键（:722-744，Q1：正式程序不得残留候选期键），清除失败按非零产物收敛 `recovery_required`。回归测试 `test_asset_service.py:672`（手工塞齐 → scan `ready_to_promote=True` → promote 成功 → 候选键消失）。**通过**。
- 优先级 P1 / 置信度 0.95（已实测复现）。

### ACI-004（严重）删除索引失败仍返回普通成功

- 定位：`asset_service.py:627-656,776-788`（r2）
- 触发：删除后的边界清空或模块父级对账失败；撤销后的对账失败。
- 影响：磁盘操作已成功但 SQLite 仍可保留幽灵行或缺行，调用方收到 `code="ok"`，无法触发重扫提示。规格已为 `delete_asset` 定义 `index_pending`。
- 处理结论（r3 已修复）：三处全部落地——(1) 边界清理失败（`asset_service.py:739-741`）、(2) 容器删除后模块父级对账失败（:753-757）、(3) 撤销后对账失败（:908-918）均返回 `ok=True, code="index_pending"`，消息含「重新读取程序列表」提示。回归测试 `test_asset_service.py:632`（边界清理）、`:660`（模块父级）、`:932`（撤销对账）分别断言。**通过**。

### ACI-005（严重）撤销重建容器后失败会以 clean 收束副作用

- 定位：`src/fwasset/core/quarantine.py:434-519`、`asset_service.py:765-774`（r2）
- 触发：`undo_delete` 已创建 `removed_containers`，随后 `os.replace` 或隔离清单收尾失败。
- 影响：隔离内容仍在时，调用方按"零产物"执行 `commit()`，但工作区已多出应用创建的空容器；失败返回宣称内容保留可重试，却隐藏了产品树变化。
- 处理结论（r3 已修复）：`_rebuild_removed_containers` 返回本次实际创建的容器（`quarantine.py:444-496`，已存在不计入、不回滚）；`os.replace` 失败 → 逆序 `rmdir` 补偿（:565-577）：完整 → `QuarantineError` 可重试，不完整 → 新增 `UndoCompensationIncompleteError`（:80-87）；清单收尾失败 → 同样 `UndoCompensationIncompleteError`（:579-588，沿用「内容已移动 → recovery_required」口径）。service 侧 `asset_service.py:892-896` 捕获后 re-raise 不 commit，外层 :924-931 返回 `undo_failed` + `payload.recovery_required=True`，`__exit__` 落 `recovery_required`。回归测试 `test_quarantine.py:1022`（replace 失败补偿完整可重试）、`:1059`（多级容器部分创建逆序回滚）、`:1100`（rmdir 回滚失败 → recovery_required）、`:1150`（清单收尾失败 → recovery_required）覆盖。注：`model_scheme_service.py` 的 `undo_delete` 调用不带 `removed_containers`（:503），不触发重建路径，未受影响。**通过**。

### ACI-006（中等）布局前置与锁边界未按规格实现

- 定位：`asset_service.py:126-157,571-580`（r2）
- 触发：工作区为 `invalid`，或删除反查扫描较慢。
- 影响：`create_asset` 没有返回规格矩阵规定的 `layout_invalid`；`delete_asset` 把规格要求的锁外反查放入 `WorkspaceTransaction`，无产品写入的全工作区扫描占用写锁。
- 处理结论（r3 已修复）：`create_asset` 锁外 `detect_workspace_layout` → invalid 返回 `layout_invalid`（`asset_service.py:140-143`，测试 `test_asset_service.py:220`）；`delete_asset` 反查/阻断检查/跨 owner 确认/`retired_copies` 全部移到锁外（:648-688），测试断言 `find_references_to` 期间 `workspace_lock_is_held=False`（`test_asset_service.py:740`）；锁内先 `preview_token_is_current` → `stale_plan` clean（:696-699，测试 :769）；目标 `directory_manifest_hash` 锁外记录（:659）、锁内复验（:711-724），`ManifestError` 映射 `stale_plan`，外部绕过事务改写同样拦截（测试 :804）。测试来源脚手架统一移到工作区外（`_source_dir`，`test_asset_service.py:90-99`），生产布局规则未放宽；索引 db 隔离 fixture 同步外移（:59-68），无其它用例污染（105 passed 复核通过）。**通过**。备注：`stale_plan` 不在子任务 5 失败码表内，但它是子任务 4 已收口的既有错误码（父规格 :289/:334），复用合理。

### ACI-007（中等）候选扫描的"内容为空"和完整性判定口径漂移

- 定位：`src/fwasset/core/incomplete_scan.py:83-102,147-160`（r2）
- 触发：候选目录只含 `程序信息.toml`，或导入内容位于子目录。
- 影响：元数据文件被算作有效内容，空候选不会产出"目录空"诊断；完整性只看顶层文件名，而创建分流使用递归相对路径，同一内容在两个入口可能得到不同结论。
- 处理结论（r3 已修复）：新增 `_candidate_content_files`（`incomplete_scan.py:78-92`）：`rglob` 递归收集相对路径（正斜杠归一，与 `asset_service._session_filenames` :105-111 同口径），排除**根层** `程序信息.toml`（normcase 精确匹配，子目录同名文件仍计为内容）。空目录诊断（:104-122）与 `classify_staged_content`（:169）共用同一份清单。回归测试 `test_incomplete_scan.py:75`（元数据-only → 「目录空」warning）、`:95`（嵌套 rom+pkg → `ready_to_promote=True`）。**通过**。备注：与 create 分流存在一处**既有**（非本批引入）口径差——create 侧 staging 清单不排除根层元数据（staging 落位前元数据已被 `stage_import_files` 跳过，语义等价），不影响本判据。

### ACI-008（中等）必需元数据写入失败仍可能报告成功

- 定位：`asset_service.py:198-200,276-283,447-453`（r2）
- 触发：`save_vendor`、`save_candidate_metadata` 或 `clear_candidate_metadata` 返回非 `ok` 状态。
- 影响：正式资产可能缺少新建契约要求的 vendor；候选可能没有可发现的磁盘真源；完成状态可能与返回值相反。
- 处理结论（r4 复核：两处收口点通过，但同批新增代码复发同型缺陷 ACI-008a，整体未完全收口）：`create_asset` 的 `save_vendor` 非 ok → 不 commit、返回 `promote_failed` + `payload.recovery_required=True`（`asset_service.py:216-223`）；`_create_incomplete_candidate` 的 `save_candidate_metadata` 非 ok → 同样不 commit、`recovery_required`（`asset_service.py:305-314`，堵住双盲死角——实测确认失败现场候选目录已落盘、`scan_incomplete_imports` 对无元数据候选产出「缺少元数据文件」warning，可发现性未破）；`supplement_candidate` 的 `clear_candidate_metadata` 未被破坏（ACI-002 收口，`asset_service.py:521-523`）。回归测试 `test_asset_service.py:1617`（vendor 失败：磁盘资产已落盘 + recovery_required）、`:1641`（候选元数据失败：候选目录已落盘 + recovery_required），实测通过；两处收敛均符合「已落盘半成品」判据（原路径/目标已落盘 → 不 commit）。但同批新增的 `promote_candidate` vendor 补写路径（:718-721）复发「只记日志后报成功」模式，见 ACI-008a。

### ACI-008a（中等，r4 复核新增）promote_candidate 的 vendor 补写失败被静默吞掉，仍返回成功

- 定位：`asset_service.py:718-721`（r4 新增块）。
- 触发：候选期 vendor 为空（用户创建时未填），提升后 `save_vendor(destination, "")` 返回非 ok（`parse_error` / `write_error`）。
- 影响：与 ACI-008 原始触发条件完全相同——正式资产缺少新建契约要求的 vendor 字段（父规格「新建程序要求写入 vendor」），但服务返回 `ok=True, code="ok"` 且事务已 commit（实测工作区 clean），调用方无从得知，仅 `log_fn` 一行。实测复现：`save_vendor` 返回 `("parse_error", ...)` → promote_candidate 返回 `ok=True, code="ok"`。r4 刚在 create_asset / _create_incomplete_candidate 两处收口此模式，同一批新增代码又引入同样写法。附带：`_save_keys` 的 IO 异常（`atomic_write_text` 的 OSError，`asset_info.py` docstring「IO 异常自然上抛」）在该调用点未捕获，会裸异常穿透服务边界（违反「错误码必须经 ServiceResult 返回」），`__exit__` 落 recovery_required。
- 修复方向：与 `create_asset:216-223` 同口径——非 ok → 不 commit、返回 `promote_failed` + `payload.recovery_required=True`（此时候选已移走、资产目录已落盘，属非零产物，人工补写 vendor 即可收敛）；IO 异常包裹为同一 ServiceResult 返回。补「提升后 vendor 写入失败 → recovery_required」回归测试。
- 处理结论（r5 已修复）：vendor 补写非 ok 或 IO 异常 → 不 commit、返回 `promote_failed` + `payload.recovery_required=True`（`asset_service.py:745-756`，与 create_asset 同口径）；同批顺带收口提升移动后 `clear_candidate_metadata` 失败的同类收敛（:722-744，ACI-003b 联动）。回归测试 `test_asset_service.py:699`（提升后 vendor 失败 → recovery_required，资产已落盘）。**通过**。
- 优先级 P2 / 置信度 0.97（已实测复现）。

## 结论

r4 复核：ACI-003/008 的修复框架正确，两处 ACI-008 收口点与 promote_candidate 的事务收敛判据（CAS / 准入 / os.replace 目标落盘 / reconcile 失败 index_pending）经代码复核与 6 个相关回归实测属实；但**复核不通过**——r4 新增代码引入三处缺陷：ACI-003a（A1 判定用候选区路径语境，与 create 分流的落点语境不一致，keyword 型内容永远判「不完整」）、ACI-003b（scan 报 `ready_to_promote=True` 的候选被 `import_state` 一票否决挡在唯一提升入口外，ACI-003 孤儿影响在手工补齐场景仍存在，拒绝消息失实）、ACI-008a（promote_candidate 的 vendor 补写失败只记日志仍返回 `ok=True`，ACI-008 同型复发）。三者均经独立脚本实测复现。ACI-001/002/002a/002b/004/005/006/007 维持 r3/r3.1 通过结论，本次未发现被 r4 差异破坏。

r5 修复：ACI-003a（A1 判定改落点语境，与 create 分流同口径）、ACI-003b（`import_state` 门放行至 A1 判定 + 移动后清除候选期键）、ACI-008a（vendor 补写失败按非零产物收敛 `recovery_required`，含 IO 异常包裹）全部收口，各带回归测试（`test_asset_service.py:613/:672/:699`）。待统筹复核 r5 差异（仅 promote_candidate 三处 + 三条回归）后进入最终自动化检查。
