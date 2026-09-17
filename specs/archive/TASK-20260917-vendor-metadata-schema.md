# TASK-20260917-vendor-metadata-schema：vendor 元数据生产链与索引 schema v4

状态：已完成

## 目标与规则

父规格 [TASK-20260903-crud-write-semantics.md](TASK-20260903-crud-write-semantics.md) 子任务 2 的剩余切片，规则以 **D6.1–D6.4** 为准；`chassis_type` 生产链已由 `a380695` 落地，本任务把两字段同批纳入索引并一次性提升 `schema_version`（D6.4）。

1. **`FirmwareAsset.vendor: str`**（`core/types.py`）：必填；`""` = legacy / 未知厂商（D6.3 存量语义）。`core/types.py` 是真源，同步生产者、消费者与测试。
2. **`程序信息.toml` 读写原语**（新 `core/asset_info.py`，模式参照 `core/platform_config.py`）：
   - `load_asset_info_with_status(asset_dir) -> tuple[dict, AssetInfoStatus, str]`；状态四类 `ok / missing / parse_error / parser_missing`；TOML 语法错误或顶层非 dict → `parse_error`；
   - vendor 取值：`ok` 且 `vendor` 为非空 str → 该值；`ok` 但键缺失 / 非字符串 / 空串 → `""` 且不产生 issue（与缺文件同语义，值级宽松）；
   - `save_vendor(asset_dir, vendor) -> tuple[AssetInfoStatus, str]`：严格读取现有文件后合并写入（**保留未知键**），经 `config_io.atomic_write_text`；`missing` 按空文档起写；`parse_error / parser_missing` → 拒绝写入并保留原文件；IO 异常自然抛出（服务层映射归子任务 5a）。
3. **扫描生产链**（`core/file_scan.py`）：每个已匹配资产目录读一次 `程序信息.toml`（每资产一目录，天然单次读盘，无需缓存）；`missing` / 值缺失 → `""` 无 issue；`parse_error / parser_missing` → `""` + **warning 级 ScanIssue**（`path` 为该文件完整路径），每资产至多一条；全量与子树扫描对同一路径**字段级一致**；`FirmwareAsset.files` 已排除该文件，维持现状。
4. **厂商名单读取侧（D6.1）**（`core/settings.py`）：config.toml 顶层 `vendors: list[str]`，缺省 `["摩众", "国瑞", "亿微", "明锐"]`；`load_vendor_candidates()` 返回规范化名单；`normalize_vendor_list`：trim 后非空、按 casefold 去重并保留首次输入的展示大小写；非列表 / 含非字符串项 / 配置损坏 → 回退缺省名单，**不重写文件**；显式空列表按原样返回空。`config.example.toml` 补 `vendors` 示例。名单增删写入口归子任务 8，届时按 D6.1「不得静默覆盖整个 config.toml」实现（保留 `[paths]`、原子写）。
5. **索引 schema v3 → v4（D6.4）**（`core/asset_index.py`）：
   - `SCHEMA_VERSION = 4`；assets 表新增 `chassis_type`、`vendor` 两列（`TEXT NOT NULL DEFAULT ''`）；
   - `_ASSET_COLUMNS / _ASSET_BINDINGS / _asset_to_row / _row_to_asset` 同步；`_row_to_asset` 的 chassis_type 临时特判（`"chassis_type" in keys`）移除，直接读列；
   - **不加 v3→v4 ALTER 迁移分支**：v3 及更早版本走既有「版本不兼容，请重新扫描生成」报错路径（D6.4「不匹配即要求重扫」）；既有 v1/v2 分支行为不变；
   - **核对既有迁移分支的 schema_meta 写值必须是字面中间版本号**，不得引用 `SCHEMA_VERSION` 常量——否则升常量后旧库会被静默错标 v4 而缺新列；
   - `vendor` / `chassis_type` 不进 `keyword_fields`、不加专用索引（无查询消费，消费方随 UI 子任务 8 定）。
6. 全仓同步所有 `FirmwareAsset` 构造点补 `vendor`（rg 核对 `src/` 与 `scripts/`，含测试夹具；mypy 会强制必填键）。

**范围边界**：不做 D1.6 仅改厂商服务（子任务 5a）、设置页与重扫引导 UI（子任务 8）、导入重写 `程序信息.toml`（子任务 3/5）、`manifest.py` 的 `exclude_names` 语义（子任务 6 定副本元数据边界）、不动 USB / 受管路径（1a 已覆盖）、不删 `"旧"` 泛化关键词（子任务 7）。

## 验收清单

- [x] `程序信息.toml` 五态扫描行为：合法 vendor / 缺文件 / 缺键 / 非字符串值 / 损坏 TOML（末态 `""` + warning issue 断言）
- [x] `save_vendor` 原子写、保留未知键、损坏文件拒绝写入且原文件不变
- [x] 全量与子树扫描 vendor 字段级一致
- [x] `load_vendor_candidates`：缺省、规范化（trim / casefold 去重保留首次展示）、非列表与损坏回退缺省、空列表原样
- [x] schema v4 新列 roundtrip；v3 库打开报「请重新扫描生成」且零 ALTER；旧迁移分支不受影响
- [x] v3 旧库全量重扫自动重建缓存并保留 `hidden_items`（ISS-20260917-04，人工验证发现后修复）
- [x] `FIRMWARE_ASSET_KEYS` 全键校验覆盖 vendor（缺键被拒）
- [x] `uv run ruff check src scripts`、`uv run mypy`、`uv run python -m pytest -q` 全过（覆盖率 ≥80%）
- [x] 独立临时目录场景脚本通过（不触碰真实数据）
- [x] 迁移说明 `docs/migrations/MIGRATION-20260917-vendor-metadata-schema.md`：schema v4、打开旧库需重扫、目录与 TOML 不受影响、只重建搜索缓存
- [x] Review 留档（schema / 扫描索引 / 类型契约命中触发条件）；CHANGELOG Unreleased 一条（索引升级与重扫提示）；人工验证结论

## 验证

- 自动化：三条门禁命令 → 全过（ruff 通过；mypy 通过；pytest 771 passed, 1 skipped，覆盖率 94.59% ≥ 80%）。场景脚本（独立临时目录）18 项全部通过：五态扫描、`save_vendor` 重扫生效、全量/子树一致、v4 roundtrip、v3 报重扫且零 ALTER、v3 库重扫自动重建且保留 hidden_items。
- 人工：2026-09-17 用户实机验证通过——v3 旧库启动提示「版本不兼容，请重新扫描生成」（预期）；全量重扫成功落库、不再重复报错、资产列表正常加载；隐藏项保留；固件目录与 `config.toml` 未被改动。
- 文档：迁移说明已建；Review 已留档（`docs/code-review/REVIEW-20260917-vendor-metadata-schema.md`，4 项发现全部修复）；CHANGELOG Unreleased 已加索引升级与重扫修复两条。

## 人工验证最短步骤

1. `uv run fwasset` 启动应用（当前 `.runtime/fwasset.db` 为 v3）：显示「版本不兼容，请重新扫描生成」提示且应用不崩溃（预期）。
2. 执行一次全量扫描：扫描应成功完成、不再出现第二次「加载失败」，资产列表正常加载。
3. 若此前隐藏过目录/资产：确认隐藏状态在重扫后仍生效。
4. 确认固件目录与 `config.toml` 未被程序改动。
