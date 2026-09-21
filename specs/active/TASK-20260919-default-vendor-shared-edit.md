# TASK-20260919-default-vendor-shared-edit：默认、厂商与借用编辑

状态：实现完成，审查通过（WES-001～009 已闭合）

## 目标与规则

子任务 5a 提供三个受管写入口：仅改厂商（D1.6）、设为默认（D1.7），以及登记、覆盖与解除借用（D1.8 / D10.1a）。实现位于 `core/services/write_edit_service.py`；旧 UI 入口不得直接写 TOML，UI 接入留待子任务 8。

所有入口先执行 `check_reference_gate(configured_root, workspace_root)`；随后以工作区路径守卫确认所有实参仍在当前工作区。每次落盘写均在 `WorkspaceTransaction` 写锁内严格重读，并在首次可能改动产品数据前调用 `begin_product_write()`。锁内、尚未发生产品写入的拒绝分支必须 `commit()`，以 clean 偶数 generation 收束；CAS 失败和已开始写后的失败遵循对应分支的磁盘状态，不用新读取结果覆盖用户已确认的 preimage。

本任务代码、测试与本规格由实现者写入；候选从 r1 修复后递增为 r2。`REVIEW-20260919-default-vendor-shared-edit.md` 的 WES-001 至 WES-004 是本候选阻断项；WES-006 在本规格中定为**纳入本子任务**。

### D1.6：仅改厂商

`update_asset_vendor(configured_root, workspace_root, asset, vendor)`：

1. 以 `scan_firmware_subtree(workspace_root, asset.path)` 冷扫并以 `same_path_identity` 确认恰好一个完整合法资产；该扫描入口以 workspace 为 context root，保留 category、model_directory 等上下文字段，与全根扫描一致。
2. 严格读取 `程序信息.toml`；解析失败或解析器缺失返回 `metadata_corrupt`，原文件不改；文件不存在是合法空文档。旧值等于新值返回 `unchanged`，不加锁、不写盘、不推进 generation。
3. 锁内重复冷扫和严格读取；preimage 的字节仍等于当前文件才原子写入合并后的 vendor 文本，未知键保留。preimage 不一致返回 `stale_plan`。
4. 写成功后用同一保留上下文的子树扫描生成新资产；扫描异常或 error issue 返回 `rescan_failed`，payload 包含 `detail` 或 `issues`；`replace_asset(old_path, scanned_asset)` 抛 `AssetIndexError` 返回 `index_update_failed`，payload 包含 `detail`。两者均不回滚已落盘 metadata，并明确提示重扫列表。
5. 不改程序目录、引用、defaults 或资产身份；操作不可撤销。

### D1.7：设为默认

`set_asset_default(configured_root, workspace_root, asset)` 仅接受本型号通用区的完整合法资产。模块键来自扫描资产的 canonical firmware label，默认值来自资产目录名。

1. 平台配置的已归一 gate 必须同时满足：恰好一个 block、block name 属于 `ChassisType`、该 block 内该 canonical 模块键不存在别名组。
2. 对 canonical 键收集所有 raw key 与值：同 canonical 的多 raw key 且值不同返回 `canonical_conflict`；同 canonical 的多 raw key 且值相同返回 `canonical_duplicate`。两个码的 payload 都含 `module_key`、`keys` 和 `values`；其余平台结构不满足返回 `platform_not_normalized`。判定复用 `reference_service._check_canonical_conflicts` 的 canonical 比较规则（而非另造字符串归一规则），并在本入口补足同值别名拒绝。
3. 锁外与锁内均对模块目录执行 `find_references_to(..., "module")`。严格读取问题返回 `reference_incomplete`；仍有 `shared_follow_default` 命中时返回 `follow_default_migration_required`，payload 含命中，且不静默改变 legacy 指向。
4. 锁内严格读取 `平台配置.toml`、取得 preimage，再将唯一 block 的 canonical key 设为派生 variant；CAS 失败返回 `stale_plan`。写成功后不回滚后续刷新类失败，磁盘为准。
5. 操作不可撤销；恢复方式是重新设回先前版本。

### D1.8：登记或覆盖借用

`register_shared_module(configured_root, workspace_root, target_model_root, source_asset, *, mode="static", overwrite_token=None)` 仅允许新产生 `static` 或 `follow_asset`。

1. 目标必须是存在的型号根且配置 `model_id` 完整；不存在目标返回 `target_model_missing`，存在但不是型号根/没有合法 id 返回 `invalid_target_model`。来源必须是完整合法资产，所属来源型号有合法 id；受管排除目录中的来源返回 `retired_anchor`。相同 root 或相同 model_id 返回 `self_reference`；目标外没有其他型号返回 `no_other_model`。
2. 锁外预览已有 canonical module 的登记；若存在，签发 token：`{"generation": preview_generation, "fingerprint": fingerprint(existing)}`，返回 `confirmation_required` 和 existing/token。fingerprint 是规范化条目身份与内容（canonical module、source id/group/module/relative path、mode、source platform）的 SHA-256。
3. 第二次调用携带 token 后，锁内重读目标配置与当前 generation。只有 token 的 generation、fingerprint 与锁外预览记录均有效，且锁内的当前条目仍有相同 fingerprint 且锁内 generation 与 token generation 相同，才允许覆盖；任一不一致返回 `stale_plan` 并携带最新 existing/新 token，绝不直接覆盖未确认的新条目。没有既有条目时按普通登记写入；携带无关或缺失 token 仍返回 `confirmation_required`。
4. 写入以严格读取全量 `型号配置.toml`、保留无关 raw key 与未知字段、preimage/CAS 写入为准；结构不合法返回 `canonical_conflict`；CAS 失败返回 `stale_plan`。登记本身不可撤销。

### D10.1a：解除借用及受签发撤销

`clear_shared_module(...)` 纳入本子任务。严格读取目标 `型号配置.toml` 后删除一条 canonical shared entry，不删除任何固件文件。返回的成功 payload 签发仅存于服务内、有效期 5 秒的 undo 数据，包含目标路径、完整 preimage、完整 postimage、canonical key 与到期时间；不暴露 preimage 内容给 UI。

`undo_clear_shared_module(...)` 仅接受服务签发且未到期的 undo token：当前配置文件字节必须仍等于 postimage，才以 CAS 恢复 preimage；期间登记其他借用、修改 TOML 或 token 过期均返回 `undo_conflict`，不覆盖当前文件。clear 的严格读取失败返回 `config_parse_error` 且不执行删除。clear 与成功 undo 都在事务锁与 generation 内完成；清除不存在条目可幂等返回 `unchanged`，不签发 undo。

## 错误码

| 入口 | 码 | 含义 |
| --- | --- | --- |
| D1.6 | `metadata_corrupt` / `stale_plan` / `rescan_failed` / `index_update_failed` | 元数据不可读、CAS 冲突、重扫失败、索引替换失败 |
| D1.7 | `platform_not_normalized` / `canonical_conflict` / `canonical_duplicate` / `follow_default_migration_required` / `reference_incomplete` / `stale_plan` | 配置 gate、别名冲突/重复、legacy 迁移阻断、反查不完整、CAS 冲突 |
| D1.8 | `target_model_missing` / `invalid_target_model` / `self_reference` / `no_other_model` / `retired_anchor` / `confirmation_required` / `stale_plan` / `canonical_conflict` | 目标准入、来源准入、覆盖确认与并发保护 |
| D10.1a | `config_parse_error` / `undo_conflict` | 严格读取失败或不可安全恢复 |

所有入口还可透传统一 gate 的 `workspace_not_configured` / `workspace_mismatch`、路径守卫的 `out_of_workspace`，以及事务的 `workspace_busy` / `recovery_required`。

## 验收清单

- [x] D1.6 保留未知键；相同 vendor 零写、generation 不变；metadata 损坏不覆盖；重扫失败与索引替换失败分别返回可诊断错误并保留磁盘写入。
- [x] D1.7 拒绝 legacy 单 block、残留 `follow_default`、同值别名与异值别名；后两者返回不同 canonical 错误码和影响 payload；成功路径仅写唯一 canonical default。
- [x] D1.8 service 级拒绝 self reference、最后仅剩一个型号后的 `no_other_model`、受管来源与无效目标；覆盖需先确认，期间 generation 或条目内容改变则 `stale_plan`，确认后可成功覆盖。
- [x] D10.1a clear 严格读取、签发 5 秒 undo；undo 成功恢复原条目；postimage 被改动时返回 `undo_conflict`，不覆盖改动。
- [x] 每个新增/修复行为先有失败测试并已观察到预期 RED，再写最小实现；相关测试和全量 Python 检查通过。

## 验证

- 自动化：先运行 `uv run python -m pytest src/fwasset/tests/test_write_edit_service.py -q --no-cov` 取得 RED，逐项实现后运行同一命令取得 GREEN；候选稳定后运行 `uv run ruff check src scripts`、`uv run mypy`、`uv run python -m pytest -q`。
- 人工：不适用；本任务验收点由服务级自动化断言覆盖。
- 文档：本 TASK 与 `REVIEW-20260919-default-vendor-shared-edit.md` 关联；修复后由审查者复核 WES-001～004。
