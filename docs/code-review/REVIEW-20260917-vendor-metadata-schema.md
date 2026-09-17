# REVIEW-20260917: vendor 元数据生产链与索引 schema v4

| 项 | 内容 |
| --- | --- |
| 类型 | 任务审查（schema / 扫描索引 / 类型契约命中留档条件） |
| 模块 | `core/asset_info.py`（新）、`core/file_scan.py`、`core/settings.py`、`core/asset_index.py`、`core/types.py` |
| 状态 | ✅ 首轮完整审查 + 人工验证缺陷修复完成，4 项发现全部修复并复核 |
| 相关 TASK | TASK-20260917-vendor-metadata-schema（父规格 D6.1–D6.4） |
| 基线 | `a380695`；审查覆盖 r1（初始实现）至 r4（测试隔离修复）全差异 |

## 审查范围

TASK-20260917 全部差异：`FirmwareAsset.vendor` 契约、`程序信息.toml` 读写原语、扫描五态生产链、厂商名单读取侧、索引 schema v3 → v4（两列 + 绑定 + roundtrip）、迁移分支字面版本号核对、全仓构造点同步、四份测试文件与迁移说明。

## 发现与处理

### ISS-20260917-01 ✅ 两条 v3 时代迁移测试断言过期

- 定位：`tests/test_asset_index.py` `test_schema_version_one/two_migrates_to_v3`（原 405/454 行）
- 触发：`SCHEMA_VERSION` 升 4 后任意运行即失败；与 r2 改动无关
- 影响：断言要求 v1/v2 库 init 后迁移到当前版本可用，与规格规则 5「v3 及更早版本一律报『请重新扫描生成』」冲突；生产行为符合规格，仅测试未同步
- 处理（r3）：改断言 `pytest.raises(AssetIndexError, match="重新扫描生成")`，测试名同步 `requires_rescan`，v1/v2 库构造夹具原样保留

### ISS-20260917-02 ✅ `_toml_str` 控制字符未转义，可写出非法 TOML

- 定位：`core/asset_info.py` `_toml_str`
- 触发：`save_vendor` 的 vendor 值含 `\n` / `\r` / `\b` / `\f` 等（TOML basic string 禁止裸控制字符）
- 影响：写入报成功但文件实际损坏，下次读取变 `parse_error`，触发 warning 级 ScanIssue
- 处理（r2）：按 TOML v1.0 补全 `\b \t \n \f \r` 短转义，其余 < 0x20 与 0x7F 用 `\uXXXX`；`test_save_vendor_roundtrips_control_chars` roundtrip 覆盖

### ISS-20260917-03 ✅ 两条测试隐式依赖本机真实默认库

- 定位：`tests/test_scheme_workbench_model.py:1745`（`bind(None, ...)`）、`tests/test_qt_smoke.py::test_model_switch_is_debounced_and_prefix_safe`
- 触发：`query_assets(path=None)` 解析到 `asset_index.ASSET_INDEX_PATH`（本机 `.runtime/fwasset.db`，v3）；schema 升 4 后在本机报「请重新扫描生成」，干净环境不失败
- 影响：测试结果依赖机器本地状态，掩盖真实回归信号
- 处理（r4）：前者改 `bind(tmp_path / "index.db", ...)`（同文件既有模式）；后者 monkeypatch `fwasset.core.asset_index.ASSET_INDEX_PATH` 到 tmp_path；断言语义与 UI 生产代码未动

### ISS-20260917-04 ✅ 全量重扫遇版本不兼容无法落库（死循环，人工验证发现）

- 定位：`core/asset_index.py` `save_assets`（开头 `init_asset_index`）
- 触发：v3 旧库存在时，读路径提示「请重新扫描生成」（预期）；用户重扫后 `save_assets` 被同一版本门闩拦截抛 `AssetIndexError`，扫描结果无法保存，再次显示「加载失败」——按提示操作永远无法恢复（除非手动删 `.runtime/fwasset.db`）
- 影响：schema 升级后所有存量用户被锁死在重扫失败循环；隐藏项（`hidden_items`）所在的库文件只能手动删除，用户隐藏状态面临静默丢失
- 处理（r5）：`save_assets` 捕获版本不兼容的 `AssetIndexError` 后 `_rebuild_incompatible_index`——只 DROP `assets` / `scan_meta` / `schema_meta` 按当前 schema 重建，`hidden_items` 原样保留（真源是目录树 + TOML，缓存可整体重建）；读路径行为不变（旧库打开仍提示重扫）。v1/v2 库经既有 ALTER 分支变 v3 后同路径处理；v1 库无 `hidden_items` 表时由 `CREATE TABLE IF NOT EXISTS` 补建空表

## 复核与验证（r5 后重跑）

- r2–r5 差异由统筹逐项复核（转义表、夹具保留、patch 目标 `default_index_path()` 运行时读模块全局、rebuild 的 DROP 范围）
- 门禁：`uv run ruff check src scripts` 通过；`uv run mypy` 通过（45 文件）；`uv run python -m pytest -q` → 771 passed, 1 skipped，覆盖率 94.59%（门槛 80%）
- 场景脚本（独立临时目录）：五态扫描、`save_vendor` 重扫生效、全量/子树一致、v4 roundtrip、v3 报重扫且零 ALTER、v3 库重扫自动重建且保留 hidden_items，18 项全部通过

## 剩余风险

- `save_vendor` 的服务层写入口与 IO 异常 → `ServiceResult` 映射归子任务 5a；当前无生产调用方，风险未暴露
- `vendor` / `chassis_type` 无查询消费（不进 `keyword_fields`、无专用索引），消费方随 UI 子任务 8 定；此前字段仅作缓存存在
- v1/v2 库经既有 ALTER 分支迁到 v3 结构后才报重扫（规格「分支行为不变」）；库文件被修改但不影响「重扫重建」结论
- `load_vendor_candidates` 只读 `config.toml` 顶层 `vendors`，损坏回退缺省不写盘；名单增删写入口归子任务 8（届时按 D6.1 原子写、保留 `[paths]`）
