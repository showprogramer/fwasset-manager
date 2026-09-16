# TASK-20260916-chassis-scan-chain：机芯类型字段与扫描诊断分级

状态：已完成

## 目标与规则

本任务是 [CRUD 写语义](../active/TASK-20260903-crud-write-semantics.md) 子任务 2 的首个切片：交付 D0.1 `ChassisType` 真源、D0.1a `chassis_type` 生产链，以及扫描诊断的 severity 分级（`ScanIssue`）。`vendor` 与 `程序信息.toml`（D6.2、D6.3）、厂商名单（D6.1）、索引 schema 提升（D6.4，与 `vendor` 同批）归后续切片。

### 规则（父规格 D0.1 / D0.1a，不得放宽）

- `core/types.py` 新增 `ChassisType = Literal["单3D", "单2D", "双2D", "上3D下2D"]` 与 `FirmwareAsset.chassis_type: ChassisType | Literal[""]`（必填字段，legacy / 未识别留 `""`）。`platform` 的生产逻辑（`_infer_asset_context`）一字不改，仅在字段注释标注 legacy。
- 生产链（`scan_firmware_assets` 与 `scan_firmware_subtree` 共享 `_scan_assets`，对同一路径字段级一致）：
  1. 每次扫描按 `context_root`（工作区根）调用一次 `enumerate_model_roots`，逐资产按 `_owner_root_for` 的**最深匹配**语义归属型号根；不属于任何型号根 → `chassis_type = ""`，不产生 issue。归属规则不得在 `file_scan` 复制第二套——需要跨模块复用时把归属 helper 提为 `reference_lookup` 的公共函数（单一真源）。
  2. 严格读取型号根 `平台配置.toml`（用 `load_platform_config_strict`，无名块按 `parse_error`）：恰好一个块且 `name` 属于 `ChassisType` 枚举 → 写入 `chassis_type`；`missing`、多块、非枚举 name → `""` 且无 issue。
  3. `parse_error` / `parser_missing` → `""` 且产生 **warning** 级 `ScanIssue`，不阻断索引对账。
  4. 同一型号根的配置在单次扫描内只读一次盘（缓存）。
- 诊断结构（D0.1a 第 5 条）：`core/types.py` 新增 `ScanIssue`（TypedDict：`severity: Literal["warning", "error"]`、`message: str`、`path: str`，无关联路径留空串；chassis 警告的 `path` 记 `平台配置.toml` 完整路径）。`scan_firmware_assets` / `scan_firmware_subtree` 返回 `tuple[list[FirmwareAsset], list[ScanIssue]]`：
  - 目录读取失败（`os.walk` onerror）与用户取消 → `error` 级；
  - chassis 配置 `parse_error` / `parser_missing` → `warning` 级；
  - 全量与子树扫描返回同一诊断结构。
- `asset_reconcile.reconcile_subtree`：由「errors 非空即拒绝写库」收窄为**只因 `severity == "error"` 的 issue 阻止**；取消与既有错误语义不变。
- `services/scan_service.py`：payload 新增 `warnings: list[str]`（warning 级 message）；`errors` 保留、仅含 error 级 message；`message` 文案不变（UI 展示 warning 归子任务 8，`workbench_window` 消费点零改动）。
- 不接索引列：`chassis_type` 不进 `asset_index` 列与 schema（D6.4 与 `vendor` 同批提升 `schema_version`，本片零 schema 变化）。
- 调用点随签名适配：`file_scan.find_handcontrol_folders`、`scripts/spike_pyside6.py`、`scripts/verify_r3_write_api.py` 及构造 `FirmwareAsset` 字面量的测试。

### 边界（不做）

- 不改 `_infer_asset_context` 的 `platform` 生产逻辑（父规格非目标）。
- 不实现 `程序信息.toml` / `vendor`（下一片）、不提升 `schema_version`、不接 UI 展示。
- 不改 `platform_config.py` 既有两个读取函数的语义。

## 验收清单

- [x] `ChassisType`、`chassis_type`、`ScanIssue` 落 `core/types.py` 真源；`platform` 生产逻辑零改动。
- [x] 单块且 name 属枚举 → `chassis_type` 写入；missing / 多块 / 非枚举 name → `""` 且无 issue。
- [x] `parse_error` / `parser_missing` → `""` + warning 级 issue；同一型号根单次扫描只读一次盘。
- [x] 全量与子树扫描对同一路径的 `chassis_type` 字段级一致；单型号布局（根即型号）与多型号布局均正确归属，无型号根资产为 `""`。
- [x] 目录读取失败 / 取消 → error 级 issue；`reconcile_subtree` 仅因 error 级阻止，warning 级放行写库。
- [x] `scan_service` payload 含 `warnings`，`errors` 仅含 error 级，`message` 文案不变；UI 消费点零改动。
- [x] `uv run ruff check src scripts`、`uv run mypy`、`uv run python -m pytest -q` 全部通过（覆盖率门槛 80%）。

## 验证

- 自动化（r2 最新）：`uv run ruff check src scripts` → 退出码 0；`uv run mypy` → 退出码 0；`uv run python -m pytest -q` → 736 passed / 1 skipped，覆盖率 94.55%（门槛 80%）。附注：r2 首轮全量 pytest 在解释器退出阶段出现一次性 Qt teardown segfault（复跑 exit 0，无法复现，非本改动引入）。
- 人工：独立临时目录一次性场景脚本已执行，11 项检查全部通过（exit 0）——单块枚举写入、missing / 多块 / 非枚举留空无 issue、损坏 TOML 留空 + 恰一条 warning（path 记配置完整路径）且资产全量扫出、全量/子树字段级一致 + 子树诊断只含本子树、单型号布局归属根 + 全量 = 子树。命令：`uv run python "%TEMP%\fwasset-snapshots\TASK-20260916-chassis-scan-chain\scenario\verify_chassis_scenarios.py"`。用户 2026-09-16 指示：非人工验证项由统筹直接测试后转 Codex 处理，场景证据采信。
- 文档：Codex 独立审查两轮（r1 阻断 CSC-001 / CSC-002 → r2 修复 → 复审通过），Review 见 [REVIEW-20260916-chassis-scan-chain.md](../../docs/code-review/archive/REVIEW-20260916-chassis-scan-chain.md)（Codex 执行环境写入受限，由统筹按其最终结论代为落盘；最终检查通过后因沙箱写入受阻，提交由统筹按其检查结论代为执行）。CHANGELOG 不写（`chassis_type` 尚无用户可感知入口）；迁移说明不适用（无 schema / 数据格式变化）。
