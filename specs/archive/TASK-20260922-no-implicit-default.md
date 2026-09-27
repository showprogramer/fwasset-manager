# 默认程序：取消一切自动推断，仅显式「设为默认」生效

- 状态：已完成
- 日期：2026-09-22
- 来源：用户决策——「没有手动设置却出现默认值自动设置，没必要」；范围选择「全部去掉」
- 影响面：`ui_common/view_models/scheme_workbench_model.py`、`core/shared_module_resolver.py`（第 3 节第 5 项）、`ui_qt/data_grid.py`（未设默认文案）、相关测试；不改核心写服务

## 实现记录（2026-09-23）

- `default_platforms_for`：只按 `defaults[模块] == directory_name` 精确匹配；删除空串推断与 A4 无键推断。随之删除只为推断服务的 `_common_assets_matching_module`、`_module_has_defaults_key`、`_module_key_for_asset`。
- `default_badge`：不再有 `["*"]` 哨兵分支。
- 回源 `_pick_fallback`：空串直接返回 None；删除 A4 唯一变体兜底整段。
- `source_type` 的 `common_default` 改为查徽章（即查配置），两处 `"_默认" in dir_name` 判定删除。
- `_resolve_follow_default`：键缺失或值为空串都报 `no_source_default`，不再回落模块目录。`data_grid` 对该 reason 显示「源型号未设默认」，其余仍是「借用来源缺失」。
- 测试：`l36_tree` 等三处 fixture 的 `""` 改为显式变体目录名（单变体模块的 `directory_name` 就是模块目录名）；A4 回源用例反转为「不回源」；新增 `test_empty_default_value_means_no_default`、`test_follow_default_empty_value_means_no_source_default`、`test_follow_default_explicit_variant_hits`；徽章用例补断言「目录名含 `_默认` 不带徽章」。
- 验证：ruff、mypy 通过；pytest 1231 passed、1 skipped，覆盖率 92.63%。

## 1. 问题

用户新增程序（如手控）后，未做任何「设为默认」操作，程序却被标成/当成默认。根源是展示与回源层的两条**自动推断**规则：

1. **A4 隐式唯一默认**（`scheme_workbench_model.py:744` `default_platforms_for`，回源同款推断 :1275、:1291）：某模块通用区只有一份程序、且平台配置.toml 无该模块键 → 自动视为默认（标 ★默认、可被方案回源选中）。
2. **目录名含「_默认」推断**（`scheme_workbench_model.py:1149`、`:1453`）：目录名带 `_默认` 即归类 `common_default`，不查配置。

另有配置级隐式约定：`defaults[模块] = ""`（空串 = 该模块唯一变体即默认）——同属自动推断，一并取消。

## 2. 决策

**默认状态只能来自唯一显式入口：右键「设为默认」写入 平台配置.toml 的 `defaults[模块] = "变体目录名"`。** 除此之外一律不标默认、不参与回源。

## 3. 改动项

1. `default_platforms_for`：删除 A4 隐式推断分支（:779-788）及 `defaults[模块] = ""` 的唯一变体推断（:769-773）——空串条目按「该模块未设默认」处理。
2. `default_badge`：唯一变体/空串条目不再产生 ★默认；仅配置精确匹配变体目录名时显示。
3. 方案回源（:1275-1277 唯一变体兜底、:1291-1307 A4 兜底）：删除；未命中显式配置时返回空并给出「未设默认」类提示，不静默回落。
4. 目录名 `_默认` 子串推断（:1149、:1453）：删除 `source_type` 的 `common_default` 判定一律走配置查询；目录名本身不再有语义（历史目录如「量产_默认」仅是普通名字，配置里显式引用它仍生效）。
5. `follow_default` 共享借用（`shared_module_resolver`）：解析源默认失败时明确报「源型号未设默认」，不回落唯一变体。
6. 「设为默认」服务（`write_edit_service.set_asset_default`）与 A2 建文件逻辑不变——它是唯一显式入口。

## 4. 非目标

- 不改 平台配置.toml 的 schema 与读写实现。
- 不改已显式配置的默认行为；存量配置中显式写了变体名的条目继续生效。
- 不做存量配置迁移（空串条目自然失效为「未设默认」，扫描诊断不报错）。

## 5. 行为后果（需随实现同步 UI 文案）

- 模块只有一份程序且未显式设默认 → 无 ★默认徽章；烧录/回源/借用该模块默认时提示「未设默认」，不再静默选中唯一变体。
- 历史目录名含 `_默认` 不再自带默认身份；如需默认须右键显式设置一次。

## 6. 验收标准

1. 空白工作区新增程序（各模块仅一份），无 ★默认徽章、平台配置.toml 无写入。
2. 右键「设为默认」后徽章出现、配置落盘、回源/借用可命中。
3. 删除配置中的键 → 徽章消失、回源提示未设默认，不再回落唯一变体。
4. `defaults[模块] = ""` 的历史配置 → 按「未设默认」处理，不报错。
5. `ruff` / `mypy` / `pytest` 通过；受影响的 A4/徽章/回源用例按新语义重写，覆盖率 ≥ 80%。
