# TASK-20260921 子任务 7：存量 platform 归一（D5.5）+ legacy `"旧"` 关键词退役（D4.3③）

- 状态：已完成（2026-09-21）。规格 r7 审查 PASS；实现经 3 轮独立审查 PASS（P7-IMP-001…006 全部闭合）
- 实现审查闭合项：P7-IMP-001（预览遇阻断级 issue 不发布可执行内容；`follow_default` 前置移到 `chassis_type_locked` / `unchanged` 之前）、P7-IMP-002（应用文件头两行须同时满足三条件才排除）、P7-IMP-003（受管容器与受管根不下钻、不误报）、P7-IMP-004（D5.4 四条端到端断言）、P7-IMP-005（预览 `reference_incomplete` 与薄封装直通断言）、P7-IMP-006（容器名 `normcase` 比较；`is_retired_versions` 由 `managed_path_reason` 派生）
- r1 → r2：闭合 P7-001（`follow_default` 归属收敛为入向单向）、P7-002（`stale_plan` 判据改为可重验的等价输入）、P7-003（冲突分类互斥优先级）、P7-004（`scheme_config` 的独立 `"旧"` 固化为本轮改动）、P7-005（多块 rename/update 端到端回归）。
- r2 → r3：闭合 P7-006（CAS 只覆盖「锁内读 preimage 后」的窗口，删除时序错误的契约与断言）、P7-007（`chassis_type_locked` 与 `already_normalized` 解耦，判据独立为「恰好一个枚举块 + 请求值不同」）。
- r3 → r4：闭合 P7-008（整体规范化重写会丢弃注释与未知字段，纳入不可逆点与确认范围；新增 `discarded_content` 预览字段，不因此拒绝归一）。
- r4 → r5：闭合 P7-008 复发（注释识别按 TOML 词法，覆盖缩进与行尾注释、排除字符串内 `#`、文件头精确匹配）、P7-009（新增 4.2a `_read_platform_source` 单次读取，自持 `data`，不改既有 `platform_config` 返回契约）、P7-010（缺 `name` 的块按 `load_platform_config_strict` 判据拒绝，不静默跳过或删除）。
- r5 → r6：闭合 P7-011（`name` / `defaults` 值必须是 TOML 字符串，非字符串拒绝而非静默 `str()` 强转）、P7-012（文件头排除限定为文件开头按序的前两处独占行注释）。
- r6 → r7：闭合 P7-013（`unchanged` 补上 `already_normalized == True` 前提，单枚举块的别名/冲突收敛不被误判为未变更）。
- 日期：2026-09-21
- 父规格：specs/active/TASK-20260903-crud-write-semantics.md（D5.3 / D5.4 / D5.5 / D4.3③ / D8 / D10.2；子任务 7 条目）
- 上游：5a（`TASK-20260919-default-vendor-shared-edit`）、6a（`TASK-20260920-layout-normalize-update`）、6b（`TASK-20260920-change-semantics-retired-versions`），均已实现并审查通过。本轮只**消费**其公开契约。
- 关联但本轮不得修改：5a / 6a / 6b Task、父 Task、任何既有 Review。

## 1. 目标与非目标

### 目标

1. **D5.5 存量归一入口**：把一个型号根 `平台配置.toml` 的多个 legacy `[[platform]]` 块合并为**恰好一个**枚举内机芯类型块，逐模块收敛 `defaults` 冲突。
2. **`follow_default` 迁移前置**：归一会删除 legacy 块，未迁移的 `follow_default` 会失去 `source_platform` 指向的来源块。目标型号相关的 `follow_default` 存在 unresolved/failed 时**阻止归一**。
3. **低层 API 收紧**（D5.5 第三条）：归一完成后不得再由 `set_default_variant` / `ensure_platform_blocks` 从方案 `platform` 或任意块名重新制造多块。
4. **D4.3③ legacy `"旧"` 退役**：`SCAN_EXCLUDE_DIR_KEYWORDS` 删除泛化 `"旧"`，前置是 ① D3.5 已禁止创建被扫描器排除的路径（已落地，本轮只核对不回退）；② 提供「因 legacy `"旧"` 被排除的目录」报告入口供「软件修复」展示。精确段 `旧版本` 的排除由 `managed_paths` 承担，**保持不变**。

工程尺度沿用父规格 2026-09-18 裁决（适用于子任务 4–8）：不做受签发入口防伪造、不做逐行崩溃注入（只测主要阶段）、不做细粒度 TOCTOU（写锁 + 锁内重验即可）。用户消息中文；服务返回 `ServiceResult`。

### 非目标

- **不实施 UI**（子任务 8）。本轮只交付服务层入口与报告数据；「软件修复」页面、确认对话框、影响展示归 8。
- 不改 5a `set_asset_default` / `update_asset_vendor` / 借用三入口的公共契约。归一只让 `platform_not_normalized` 从「永久阻断」变成「有出路」，**不放宽** 5a 的 gate 三条件。
- 不改 6a / 6b 的 `update_asset` / `change_asset_semantics` / `build_clear_defaults_plan` / `apply_rewrite_plan` 契约。
- 不改 `migrate_follow_default_refs` 自身行为（本轮只调用与读取其 payload）。
- 不改 `_infer_asset_context` 的 legacy `FirmwareAsset.platform` 生产链（D0.1 / D5.5 第二条）。
- 不改写 legacy `方案配置.toml` 的 `platform` 字段（保留不动）。
- 不做机芯类型变更（D5.2：型号建成后不可改，改机芯类型 = 建新型号）。归一是**一次性设定**，不是改类型入口。
- 不做批量/全工作区一键归一。归一按**单个型号根**执行，多型号由调用方逐个发起。
- 不做归一撤销（D10.2 同类：一次性操作，操作前确认）。

## 2. 依赖的既有公开入口（只用 API）

| 符号 | 模块 | 本轮用法 |
| --- | --- | --- |
| `migrate_follow_default_refs(configured_root, workspace_root, log_fn) -> ServiceResult` | `services/reference_service` | 归一前置迁移的**唯一**入口。幂等、可重入。`payload` 含 `changed` / `unchanged` / `failed` / `unresolved` / `converted` / `kept`。 |
| `find_references_to(configured_root, ws, target, kind)` | `reference_lookup` | 反查 `shared_follow_default` 命中与阻断级 issue。 |
| `is_blocking_issue` / `BLOCKING_ISSUE_CATEGORIES` | 同上 | 阻断级 issue 一律阻止归一。 |
| `check_reference_gate(configured_root, workspace_root)` | 同上 | 统一 configured-root gate。 |
| `enumerate_model_roots(ws)` | 同上 | 型号根归属与身份比较。 |
| `load_platform_config_with_status` / `load_platform_config_strict` | `platform_config` | **口径参照，不直接调用**：本服务按 4.2a 自持一次读取（需要 `data`）。`platforms` 派生与前者同口径；无名块按后者判为 `parse_error`。两函数本身不改。 |
| `PlatformConfigStatus` | 同上 | 状态枚举复用，不另起。 |
| `serialize_platform_config(platforms)` | 同上 | 规范格式整体重写。 |
| `canonical_module_dir(name)` | 同上 | 模块键归一（含 `机芯版→机芯板` 与快捷键别名）。 |
| `WorkspaceTransaction(ws, operation=...)` | `workspace_transaction` | 写锁 + generation seqlock + 操作日志；`begin_product_write` / `commit`。 |
| `assert_within_workspace` / `same_path_identity` / `PathGuardError` | `path_guard` | 路径守卫与身份比较。 |
| `managed_path_reason` / `should_exclude_managed_path` | `managed_paths` | `旧版本` 精确段排除（与本轮 `"旧"` 退役互不影响）。 |
| `ChassisType` | `types` | `Literal["单3D","单2D","双2D","上3D下2D"]` 真源。 |
| `_check_canonical_conflicts` | `services/reference_service` | 5a `_normalized_platform_for_default` 已在用；本轮**不复用**它，冲突判定按第 4 节自有规则（见 4.3 说明）。 |

**不新增**受签发 plan 结构：归一只改**一个型号根的一个文件** `平台配置.toml`，用 5a 同款 preimage/CAS 即可；不经 `RewritePlan`（R8 plan 的语义是跨文件级联改写，这里没有跨文件）。

## 3. 落点

| 内容 | 文件 |
| --- | --- |
| 归一预览 + 执行服务（D5.5） | **新建** `src/fwasset/core/services/platform_normalize_service.py` |
| 低层 API 收紧（D5.5③） | `src/fwasset/core/services/platform_default_service.py` |
| legacy `"旧"` 退役 | `src/fwasset/core/settings.py`（删关键词）、`src/fwasset/core/scheme_config.py`（删其独立关键词表中的 `"旧"`）、`src/fwasset/core/file_scan.py`（注释）、`src/fwasset/core/managed_paths.py`（注释） |
| legacy `"旧"` 排除目录报告 | **新建** `src/fwasset/core/legacy_exclusions.py` |
| 归一预览/结果 TypedDict | `src/fwasset/core/types.py` |
| 测试 | **新建** `src/fwasset/tests/test_platform_normalize.py`、`src/fwasset/tests/test_legacy_exclusions.py`；既有 `test_settings.py` / `test_file_scan.py` / `test_managed_paths.py` / `test_platform_default_service.py` 按第 9 节改 |

不新建 view model、不改 `ui_qt/`、不改 `ui_common/`。

## 4. D5.5 归一契约

### 4.1 签名

```python
# core/types.py
class PlatformBlockView(TypedDict):
    block_index: int
    name: str                  # 原块名（legacy 名原样）
    is_chassis_type: bool      # name 是否属于 ChassisType 枚举
    defaults: dict[str, str]   # 原始键 → 变体名（未 canonical 化，原样展示）

class ModuleConflictView(TypedDict):
    module_key: str            # canonical 模块键
    raw_keys: list[str]        # 参与该 canonical 键的全部原始键（排序）
    values: list[str]          # 去重后的候选变体名（排序）
    kind: Literal["unique", "alias_duplicate", "value_conflict"]
    resolved: str              # unique/alias_duplicate 时为唯一值；value_conflict 时为 ""

class PlatformNormalizePreview(TypedDict):
    model_root: str
    blocks: list[PlatformBlockView]
    conflicts: list[ModuleConflictView]
    chassis_candidates: list[str]        # 现有块名中属于 ChassisType 的（排序、去重）
    needs_module_choice: list[str]       # kind == "value_conflict" 的 module_key（排序）
    follow_default_hits: list[dict]      # 需先迁移的残留命中（见 4.2）
    follow_default_blocked: bool         # True 时禁止执行归一
    already_normalized: bool
    expectation: NormalizeExpectation    # 锁内重验用（见 4.4.1）
    discarded_content: list[str]         # 整体重写将丢弃的注释/未知字段摘要（见第 7 节）
```

```python
# core/services/platform_normalize_service.py
def preview_platform_normalize(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
) -> ServiceResult: ...

def normalize_platform_config(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    chassis_type: str,
    module_choices: dict[str, str] | None = None,
    *,
    expected: NormalizeExpectation | None = None,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult: ...
```

```python
# core/types.py
class NormalizeExpectation(TypedDict):
    """用户确认时所见配置的最小摘要（普通值对象，**不是**签发 token）。"""
    block_names: list[str]                 # 预览时的块名，按 block_index 顺序
    module_values: dict[str, list[str]]    # canonical 键 → 该键的候选取值（排序去重）
```

`PlatformNormalizePreview` 增一个字段 `expectation: NormalizeExpectation`，由 `preview_*` 直接产出，调用方原样回传。

- `preview_*` **只读、不持锁**（D8：纯预览不持锁）。`payload["preview"]` 为 `PlatformNormalizePreview`。
- `normalize_*` 持写锁，锁内重验全部条件。`module_choices` 的键是 canonical `module_key`，值是用户选定的变体名；只对 `needs_module_choice` 的键**必须**给出，其余键给出即 `invalid_args`（防止用户选择被静默丢弃）。
- 用户选择（机芯类型、逐模块取值）发生在**锁外**（预览时），锁内只重验——父规格 D8 硬性要求。

### 4.2 `follow_default` 迁移前置（D5.5 第一条）

预览与执行都按同一判据计算「目标型号相关的残留 `follow_default`」：

1. 对 `model_root` 调 `find_references_to(configured_root, ws, model_root, "model")`；
2. 阻断级 issue（`is_blocking_issue`）非空 → 直接 `reference_incomplete`，**预览也不发布可执行内容**（`follow_default_blocked = True`）；
3. 命中中 `kind == "shared_follow_default"` 的条目即残留。**归属口径：只算入向**——「别的型号跟随**本型号**的默认」。这正是 `find_references_to(..., model_root, "model")` 已经返回的集合：`_collect_shared_hits` 对 `follow_default` + `target_kind == "model"` 的判据是「`_ref_source_root(ref)` 与目标型号根同身份 **且** `ref.source_model_id` 等于目标 `model_id`」，即来源侧必须是 `model_root`。不需要也不得再加 `owner_root` 过滤。

   > r1 曾写「双向」（再算上「本型号跟随别人的默认」）。那个口径**无法由本查询落地**——借入方条目的来源是别的型号，`find_references_to(model_root, "model")` 结构上不会返回它；要拿到只能额外全量枚举，而那会用与本次归一无因果关系的条目阻断用户，也比 5a `set_asset_default` 的判据更严。因果关系只有一条：**归一删除本型号的 legacy 块 → 指向本型号块的 `follow_default` 断链**。本型号作为借入方的条目指向别人的块，本次写入完全不碰，不构成阻断理由；它会在**那个来源型号**自己归一时被拦住。

   与 5a 的关系：5a `set_asset_default` 用 `find_references_to(..., module_dir, "module")`（模块级、同一 `shared_follow_default` 命中语义），本轮是型号级同一函数的更宽范围。两者同向、同判据，只是粒度不同——归一影响整个型号的全部块，所以按型号查。
4. 残留非空 → `follow_default_migration_required`，payload 带命中清单（`owner_root` / `raw_key` / `source_model_id` / `source_relative_path`），UI 据此引导用户先跑迁移。

**本服务不自动调用 `migrate_follow_default_refs`。** 迁移是全工作区级、可能改写多个型号配置的独立写操作；归一是单型号操作。自动串联会让一次「归一 L36」悄悄改写别的型号的 `型号配置.toml`，超出用户确认范围。UI（子任务 8）展示阻断原因并提供「先执行迁移」按钮，由用户显式触发。

为让 UI 能在同一页面完成这件事，本轮**再导出**一个薄封装：

```python
def migrate_follow_defaults_for_normalize(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult: ...
```

它只是 `migrate_follow_default_refs` 的直通转发（不改参数、不改 payload），存在的意义是让子任务 8 从同一模块取到归一流程的两个步骤，而不是把 R8 服务散播到 UI。**不得**在其中加入额外写入。

### 4.2a 配置的单次读取与未知内容派生

`load_platform_config_with_status` 只返回 `(platforms, status, error)`，**不暴露已解析的 `data`**；扩展它的返回值会波及全部既有调用方，不做。同时它会**静默跳过缺 `name` 的 `[[platform]]` 块**（`platform_config.py:95-97`：`name` 为空即 `continue`），那些块的 `defaults` 不会出现在 `platforms` 里——但归一是整体重写，它们会被**无声删除**。

定稿：本服务对 `平台配置.toml` 只做**一次**读取，自己持有字节与解析结果。

```python
# platform_normalize_service 内部（私有 helper，不进 __all__）
def _read_platform_source(model_root: Path) -> tuple[
    bytes,                       # preimage：CAS 与注释识别的同一份字节
    dict[str, Any],              # data：解析后的原始 TOML（未知字段检出用）
    list[PlatformDefaults],      # platforms：按 data 派生，与 data 同源
    PlatformConfigStatus,
    str,                         # detail
]: ...
```

- `preimage = path.read_bytes()`；`data = tomllib.loads(preimage.decode("utf-8"))`。解析失败 → `parse_error`；`tomllib` 不可用 → `parser_missing`；文件不存在 → `missing`（`preimage = b""`）。
- `platforms` 由 `data` 派生：结构判定（`platform` 必须是 array of tables、每项是 table、`defaults` 是 table）与 `load_platform_config_with_status` 一致；`name` 取值后 trim。
- **但类型要求更严：`name` 与每个 `defaults` 值必须本来就是 TOML 字符串。** 遇到数值、布尔、数组或 table → `config_parse_error`，payload `detail` 指出具体键，零写盘。

  既有的 `load_platform_config_with_status` 对它们做 `str(...)` 强转（`platform_config.py:95/98`）——那是只读路径的容错，在这里不能沿用：归一会把强转结果当成候选值展示给用户、再整体重写回磁盘，`3` 变 `"3"`、`["a","b"]` 变 `"['a', 'b']"` 都会**无声改变 TOML 语义**，而且这类内容既不进 `discarded_content`（它没被丢弃，是被改写了）、也无法在冲突选择里以原类型呈现。凡是「归一会重写、用户又看不出变了什么」的内容，一律拒绝而不是静默处理——与无名块同一理由。
- 这条只约束**本服务**。`load_platform_config_with_status` 的强转行为不改（既有只读调用方依赖），因此存在「只读路径能显示、归一拒绝」的差异——这是有意的：只读显示不会破坏磁盘，归一会。
- **缺 `name` 的块按 `parse_error` 拒绝**，与 `load_platform_config_strict` 同判据（该函数正是为「严格读取，无名块暴露为 `parse_error`」而存在）。理由：归一必须「展示现有块与冲突 → 用户选定 → 合并」（D5.5），一个用户看不见、系统也说不出名字的块无法纳入这个流程；静默删除它的 `defaults` 违反「逐模块处理 defaults 冲突」与第 7 节的丢弃告知。返回 `config_parse_error`，payload 带 `detail` 指出存在无名块，零写盘——用户先手工补 `name` 或删除该块，再归一。
- 一次读取同时供给四处：CAS preimage、注释识别、未知字段检出、块与冲突分类。不重复读盘，也就不存在「预览读到 A、CAS 读到 B」的内部不一致。

> 不改 `platform_config.py`：`load_platform_config_with_status` 的宽松语义有既有只读调用方依赖，本轮不动；`load_platform_config_strict` 的判据被本服务**复用为规则**（无名块 = `parse_error`），实现上由 `_read_platform_source` 自行落地，避免为拿 `data` 而读两次盘。

### 4.3 冲突分类与合并规则

对全部块的 `defaults` 按 canonical 键聚合（`canonical_module_dir`）。对每个 canonical 键收集：`raw_keys` = 全部块中映射到该键的原始键（**去重后的集合**，同名键在不同块里算一个）、`values` = 这些条目的取值去重集合。

分类**按下列顺序判定，先命中先返回，互斥**：

| 序 | 判据 | `kind` | 合并 |
| --- | --- | --- | --- |
| 1 | `len(values) >= 2` | `value_conflict` | 必须由 `module_choices` 指定；未指定 → `module_choice_required` |
| 2 | `len(values) == 1` 且 `len(raw_keys) >= 2` | `alias_duplicate` | 归并为一个 canonical 键，取该唯一值 |
| 3 | 其余（`len(values) == 1` 且 `len(raw_keys) == 1`） | `unique` | 直接取该值，不需用户选择 |

- **顺序是规范的一部分**：r1 的表格把「去重后只剩一个值」写在前面，导致同块 `机芯版`/`机芯板` 同值、以及跨块别名同值这两种情形都会先命中 `unique`，别名键永远不会被归并，`already_normalized` 还会据此误判为已归一并直接 `unchanged`。
- `alias_duplicate` **不限于同一块内**：跨块的别名键（A 块写 `机芯版`、B 块写 `机芯板`、取值相同）同样归为 `alias_duplicate`，合并后只留 canonical 键。
- `alias_duplicate` **不需要用户选择**（取值唯一），但它**使型号不算已归一**（见 4.5），归一执行时必须实际写入归并结果。

- 空串取值**参与去重**且是合法取值（legacy 数据里空串表示「该模块唯一」，父规格 D0.2 保留给 legacy）。`{"", "甲"}` 是 `value_conflict`，不得把空串当「无值」丢弃。
- `module_choices` 给出的值必须出现在该键的 `values` 候选中，否则 `invalid_choice`。**不允许**借归一写入任意新变体名——设默认有 5a 的专用入口，归一只做收敛。
- 合并后的单块 `defaults` 键**一律 canonical 化**（与 5a `set_asset_default` 写入口径一致），原始别名键不保留。
- 不调用 `_check_canonical_conflicts`：那个 helper 服务于「命中范围内是否该阻止级联改写」，返回单个 `LookupIssue`；归一需要的是**逐模块完整分类**给用户选。判据本身（同 canonical 键异值 = 冲突）与它一致，只是产物形状不同。

### 4.4 机芯类型选择

- `chassis_type` 必须属于 `ChassisType` 枚举，否则 `invalid_chassis_type`。
- 与现有块名无关：`chassis_candidates` 只是**建议**（现有块名恰好命中枚举时列出），用户可选任意枚举值。legacy 块名（`默认`、`标准单机芯3D`）不是候选。
- 归一后的唯一块 `name = chassis_type`。
- **机芯类型锁（D5.2）与 `already_normalized` 解耦**，判据只有一条：

  > 当前**恰好一个块**且该块名**属 `ChassisType` 枚举** → 该块名就是本型号的现行机芯类型；此时 `chassis_type` 与之不同即 `chassis_type_locked`，零写盘。

  这条**先于**别名/冲突处理判定。理由：D0.1a 已把「恰好一个枚举块」定为 `chassis_type` 的生产判据，也就是说这种型号**已经有生效的机芯类型**了（索引里已写入、UI 已展示），而 D5.2 规定型号建成后机芯类型不可改。若把锁绑在 `already_normalized` 上，「单个 `单3D` 块 + 一对同值别名键」这种型号就会因 `already_normalized == False` 而放行，用户借「清理别名」这一步把机芯类型顺手改成 `双2D`——归一变成了改类型入口。
- 反过来，`chassis_type` 与现块名**相同**时正常继续：此时仍可能有别名/冲突要清理（即 `already_normalized == False`），归一照常执行。

### 4.4.1 锁内重验输入 `expected`

归一不可逆且会丢弃块名与落选取值，因此「用户确认的内容」必须能在锁内被比对。父规格工程尺度裁决的是**不做受签发入口防伪造、不做细粒度 TOCTOU 重验**——它禁止的是签名 token 与逐字段时点重验，不是禁止调用方把「我看到的是什么」作为普通参数传进来。`expected` 就是这样一个普通值对象：

- `preview_*` 产出 `expectation`；调用方原样回传给 `normalize_*`。
- 锁内按当前磁盘重新计算同结构的摘要，与 `expected` **逐字段相等**才继续；否则 `stale_plan`，零写盘。
- `expected is None` 时**跳过该项比对**（其余重验照常）。这条是给测试与脚本用的显式退路，不是产品路径；子任务 8 的 UI **必须**回传。
- 比较范围只有两项：`block_names`（按顺序）与 `module_values`（键集合 + 每键的候选取值集合）。它们恰好覆盖「用户看着做决定」的全部内容——选机芯类型看的是块名，选模块取值看的是候选值。`defaults` 之外的内容（如注释、键顺序）不进比较，避免把无关改动误报为冲突。
- `follow_default` 残留状态**不进** `expected`：它是独立的阻断条件，锁内按当前磁盘重判即可，与「用户看到什么」无关（它变严了本就该拦，变松了本就该放行）。
- 与 CAS 的分工，按**时序**分清，不要互相顶替：

  | 窗口 | 谁负责 |
  | --- | --- |
  | 预览发布 → 锁内读取 preimage | `expected`（锁内重算摘要与 `expected` 比对） |
  | 锁内读取 preimage → 写入 | CAS（写前再比字节） |

  CAS 的 preimage 是**锁内读的**，所以它看不到「预览后、锁内读 preimage 前」的任何改动——包括只改注释这种 `expected` 也看不见的改动。这个窗口内的纯注释/未知字段改动**本轮不拦**：它不改变任何用户确认过的语义内容，而归一本就整体规范化重写该文件、不保留注释与未知字段（第 7 节不可逆点已明示并在预览中列出）。拦它没有收益。
- 因此**不存在**「`expected` 相等但被 CAS 拒绝」这条路径（r1/r2 曾如此描述，是时序错误）。CAS 在本服务里只覆盖「锁内读 preimage 之后仍被外部改写」这一个窗口——持写锁时这基本只会来自锁外的第三方进程。

> 这不是 r1 被否掉的「预览 token」：没有签发、没有过期、没有服务端存储，伪造它也只能让用户自己跳过一次自己的确认——与工程尺度裁决「不做受签发入口防伪造」一致。

### 4.5 `already_normalized`

「已归一」= 恰好一个块 **且** 块名属 `ChassisType` **且** `conflicts` 中**全部为 `unique`**（即无 `alias_duplicate`、无 `value_conflict`）。与 5a `_normalized_platform_for_default` 的三条件同口径，但后者按单模块判、这里按整文件判。

按 4.3 的互斥顺序，`alias_duplicate` 必须先于 `unique` 判定——否则同值别名会让型号被误判为已归一而直接 `unchanged`，别名键永远留在磁盘上，5a `set_asset_default` 随后仍会以 `canonical_duplicate` 拒绝，用户陷入「归一说已完成、设默认说未归一」的死循环。

- 预览：`already_normalized = True`，`conflicts` 仍返回（全为 `unique`）供展示。
- 执行：**`already_normalized == True` 且** `chassis_type` 与现块名相同 → `ok=True, code="unchanged"`，**零写盘、不递增 generation**（与 5a `unchanged` 同语义）。
- **`already_normalized == False` 时不走 `unchanged`**，即使 `chassis_type` 与现块名相同：单枚举块 + `alias_duplicate` / `value_conflict` 的型号正是要靠这一步收敛别名与冲突（见 4.4 末条与 9.3 对应断言）。把 `unchanged` 只绑在「同名」上会让这类型号永远归一不了。
- `chassis_type_locked` **不由本节判定**——它按 4.4 的独立判据（恰好一个枚举块 + 请求值不同）先行拦截，与 `already_normalized` 无关。

### 4.6 缺配置 / 零块

| 现状 | 行为 |
| --- | --- |
| `平台配置.toml` 不存在（`missing`） | `platform_config_missing`，拒绝。归一是「合并存量块」，凭空建块属新建型号（子任务 4）职责 |
| 存在但零 `[[platform]]` 块（`ok` 且 `platforms == []`） | 同上 `platform_config_missing`（payload 区分 `status`）。无块可合并，且没有 defaults 可继承 |
| `parse_error` / `parser_missing` | `config_parse_error` / `parser_missing`，**保留原文件**，零写盘 |
| 存在缺 `name` 的 `[[platform]]` 块 | `config_parse_error`（4.2a），零写盘。**不**静默跳过、**不**静默删除 |
| `name` 或 `defaults` 值不是 TOML 字符串 | `config_parse_error`（4.2a），零写盘。**不**静默 `str()` 强转 |

### 4.7 执行顺序（锁内）

全程持写锁。用户选择已在锁外完成。

1. `check_reference_gate` → 失败即返回（锁外先做一次，锁内不重复 gate，但重验其余全部）。
2. `assert_within_workspace(model_root, ws)`；`model_root` 必须是 `enumerate_model_roots(ws)` 中的一员（身份比较），否则 `invalid_model_root`。
3. 进入 `WorkspaceTransaction(ws, operation="normalize_platform_config")`。
4. 锁内重新 `_read_platform_source`（4.2a，一次读取供 CAS preimage / 冲突分类 / 未知内容）+ 重算冲突分类 + 重算 `follow_default` 残留。三类拒绝：
   - 残留 `follow_default` 非空 → `follow_default_migration_required`；反查阻断 → `reference_incomplete`；配置读取失败 → `config_parse_error` / `parser_missing`。这些**按当前磁盘重新独立判定**，不依赖预览。
   - 按 4.4.1 的 `expected` 重验不符 → `stale_plan`。
   - 全部零写盘，`commit()` 收敛为 `clean`。
5. 用步骤 4 已持有的 preimage 构造单块 → `serialize_platform_config`。**不重复读盘**（重读会让 CAS 比较的是比分类更晚的字节，白白缩短 CAS 的覆盖窗口）。
6. `begin_product_write()` → CAS 写（当前字节 != preimage → `stale_plan`）。
7. `commit()`。

**索引不动**：`平台配置.toml` 不是资产，`defaults` 不进索引列（`chassis_type` 来自块名——见 4.8）。不调 `reconcile_subtree`。

### 4.8 与 `chassis_type` 的关系

D0.1a：`chassis_type` 仅在「恰好一个块且 name 属枚举」时写入，否则 `""`。归一正是把型号从 `""` 变成有值的操作，因此**归一成功后既有索引行的 `chassis_type` 会过期**。

处理：归一服务**不**触发重扫（它不持有资产集合，且重扫是长操作，不应在写锁内做）。返回 payload 带 `chassis_type_changed: bool` 与 `chassis_type: str`，`message` 明确提示「已归一，请重新读取程序列表以更新机芯类型」。子任务 8 据此提示用户重扫。

> 不在锁内重扫：父规格 D8 要求长扫描不封锁 CRUD；且 `bulk_reindex_subtree` 要求调用方传完整 assets，归一服务没有。

### 4.9 失败契约

| 阶段 | 返回 | 磁盘 / 状态 |
| --- | --- | --- |
| gate / 路径 / 参数校验 | `not_configured` / `root_changed` / `out_of_workspace` / `invalid_model_root` / `invalid_chassis_type` / `invalid_args` / `invalid_choice` | 零写盘，未进事务 |
| 配置读取失败 | `config_parse_error` / `parser_missing` / `platform_config_missing` | 保留原文件，零写盘 |
| 残留 `follow_default` | `follow_default_migration_required` | 零写盘 |
| 反查不完整 | `reference_incomplete` | 零写盘 |
| 缺模块选择 | `module_choice_required` | 零写盘 |
| 锁内重验不符 / CAS 失败 | `stale_plan` | 零写盘，`commit()` → `clean` |
| 已归一且 `chassis_type` 同名 | `ok=True, code="unchanged"` | 零写盘，不递增 generation |
| 恰好一个枚举块且 `chassis_type` 异名（不论是否已归一） | `chassis_type_locked` | 零写盘 |
| 锁不可得 / 待恢复 | `workspace_busy` / `recovery_required` | 零写盘 |
| 写入本身抛异常 | `write_failed`，事务未 commit → `__exit__` 置 `recovery_required` | 单文件 CAS 原子替换，要么旧要么新 |

**锁内尚未写盘的拒绝一律 `transaction.commit()`**（5a / 4 已定：零产物空提交是受支持的正常退出路径），不得靠 `__exit__` 落到 `recovery_required`。

### 4.10 崩溃

唯一的产品数据变更是一次 `atomic_write_text`（同目录临时文件 + `os.replace`）。崩溃点只有两种结果：旧文件完整、或新文件完整。

- 崩在 `begin_product_write` 之后、`commit` 之前 → 启动时 `recover_interrupted_workspace` 发现 `operation_in_progress` / 奇数 generation → `recovery_required`，阻止后续写。用户或恢复流程确认后切回 `clean`。
- **无需续跑**：归一幂等——重新预览会看到当前磁盘状态，已归一则 `unchanged`，未归一则重新走一遍。不注册 `resume` 路径。

## 5. D5.5③ 低层 API 收紧

现状：`set_default_variant` 找不到块时 `platforms.append(PlatformDefaults(platform_name=platform))`；`set_module_default_for_model` 先 `ensure_platform_blocks` 从方案 `platform` 补块。两者都会在已归一型号上重新制造多块。

**改法**（保持现有签名与既有成功路径的 payload 形状，不做「顺手重构」）：

1. `ensure_platform_blocks(model_root, platforms)`：
   - `platforms` 恰好一个块且块名属 `ChassisType` → **原样返回**，不再从方案 `platform` 补块（已归一型号封口）；
   - 否则保持现有行为（legacy 型号不受影响，subtask 8 之前的旧 UI 路径不被本轮打断）。
2. `set_default_variant(model_root, platform_name, module_dir, variant_name, workspace_root, log_fn)`：
   - 读取后若**已归一**（恰好一块 + 枚举名），且 `platform_name` 与该块名不同 → 返回 `{"ok": False, "code": "platform_normalized_locked", "message": "该型号平台配置已归一，不能新建配置块"}`，零写盘；
   - 已归一且 `platform_name` 等于该块名 → 正常写入该块；
   - 未归一 → 保持现有行为（含找不到块时新建），legacy 型号继续可用。
3. `bootstrap_platform_blocks` 不改（它只在无配置时初始化，归一要求已有块，两者不重叠）。

> 为什么不直接禁掉旧入口：父规格 D1.7 说的是「改造或降为内部 helper，正式产品只走新的单块受管服务」，替换 UI 调用点属子任务 8。本轮只封住「在已归一型号上制造新块」这一条会破坏 D5.5 成果的路径，不动 legacy 型号的既有行为，也不改 `scheme_workbench_model`。

## 6. D4.3③ legacy `"旧"` 退役

### 6.1 前置核对（不回退）

① D3.5「禁止创建会被扫描器排除的路径」已由 `admission._assert_not_excluded` 落地（对 configured root 与最终完整路径各判一次，走 `file_scan._is_excluded_dir`）。本轮**只加回归断言**，不改其实现。

### 6.2 报告入口（②）

```python
# core/legacy_exclusions.py
LEGACY_GENERIC_KEYWORD = "旧"

class LegacyExcludedDir(TypedDict):
    path: str
    matched_name: str     # 命中关键词的那一段目录名
    is_retired_versions: bool   # 该目录是否本就属受管 旧版本/（退役后仍排除）

def scan_legacy_excluded_dirs(
    workspace_root: str | Path,
    *,
    keyword: str = LEGACY_GENERIC_KEYWORD,
) -> list[LegacyExcludedDir]: ...
```

- 只读遍历工作区，返回**因泛化 `"旧"` 子串命中而被整枝排除**的目录。
- 已被 `managed_path_reason` 命中（`旧版本/`、`.fwasset/`、隔离区）的目录：`旧版本` 段标 `is_retired_versions=True` 一并列出（让用户知道它是**有意**排除的，不是误伤）；其余受管类别不列入（与 `"旧"` 无关）。
- 遍历到命中目录即**不再下钻**（与 scanner 的整枝语义一致），避免在被排除的大目录里重复报告。
- `OSError` 跳过该目录并继续，不抛。
- 纯只读，不持锁，不写盘。

### 6.3 退役（③）

`settings.SCAN_EXCLUDE_DIR_KEYWORDS` 删除 `"旧"`，保留 `["CH341SER", "接线图", "新建文件夹", "照片"]`。

**受影响的消费者**（全部核对，不遗漏）：

| 消费者 | 影响 |
| --- | --- |
| `file_scan._is_excluded_dir` | `旧款L36` 等目录不再被整枝排除，会被扫描成资产所在路径。**这是本轮的目的**。`旧版本/` 仍由 `should_exclude_managed_path` 精确段排除 |
| `managed_paths._is_ignorable_entry` → `detect_workspace_layout` | 工作区根下名含 `旧` 的一级目录不再「可忽略」：带型号标志则计为型号，否则计为未知内容（可能把 `empty`/`multi_model` 判成 `invalid`）。**这是正确行为**——用户目录不该被静默忽略 |
| `reference_lookup.enumerate_model_roots` | 同上：名含 `旧` 的型号目录开始被枚举，反查覆盖面扩大 |
| `admission._assert_not_excluded` | 用户可以新建名含 `旧` 的目录（除 `旧版本`），不再被 `path_excluded` 拒绝 |
| `usb_ops` | 经 `should_exclude_managed_path`，不受本项影响 |
| `scheme_config._is_excluded_dir(name)` | **独立硬编码** `("backup", "-back", "旧", "temp", "tmp")`，不读 `settings`。**本轮必须一并删除其中的 `"旧"`**——见下 |

**`scheme_config` 是本轮的明确改动，不是「实现阶段再看」。** 该函数在 `discover_schemes` 里有两处作用：

1. 按目录名跳过**型号级子目录**（`scheme_config.py:84`，`model_root/型号名/定制/` 这一层级）——名含 `旧` 的型号目录下的**全部方案**会整片消失；
2. 方案目录自身的跳过。

只删 `settings.SCAN_EXCLUDE_DIR_KEYWORDS` 的 `"旧"` 而留下这一份，结果是「`旧款L36` 的通用区程序回来了，定制区方案还是不见」——直接违背 D4.3③ 的退役目标，且症状比退役前更费解。

处理：删除 `"旧"`，保留 `("backup", "-back", "temp", "tmp")`。这四项与 `"旧"` 不同——它们不会命中中文业务目录名，没有同类误伤，父规格也未要求退役，本轮不动。

**新增迁移说明条目**：名含「旧」的型号目录下的定制方案会重新出现在方案列表中。

**迁移说明（用户可见）**：

1. 升级后首次打开，名字里带「旧」的型号/方案/程序目录会重新出现在列表中（此前被静默跳过），**包括名含「旧」的型号目录下的全部定制方案**；`旧版本/` 备用副本仍然不显示，这是有意的。
2. 需要用户**执行一次全量重扫**才能看到这些目录的程序（扫描缓存不会自动发现新增可见路径）。
3. 若工作区根下存在名含「旧」且不是型号的散落目录，布局可能从 `multi_model` 变成 `invalid`，应用会提示整理。处理方式：把该目录挪出工作区，或给它补上型号配置。
4. **不可逆点**：无。本项只放宽排除，不改磁盘内容；如需恢复旧行为可把关键词加回配置——但父规格已裁决退役，不提供开关。

## 7. 迁移说明（D5.5，用户可见步骤与不可逆点）

归一的用户可见流程（服务层顺序，UI 呈现归子任务 8）：

1. 「软件修复」列出平台配置未归一的型号（多块 / legacy 块名 / canonical 冲突）。
2. 用户选一个型号 → `preview_platform_normalize` 展示：现有块与各自 `defaults`、逐模块冲突、待选机芯类型、以及 `discarded_content`（将被丢弃的注释与未知字段）。
3. 若有残留 `follow_default` → 展示阻断原因与命中清单，用户点「先执行迁移」→ `migrate_follow_defaults_for_normalize` → 重新预览。
4. 用户选定机芯类型 + 逐个冲突模块的取值 → 确认 → `normalize_platform_config`，**回传步骤 2 预览里的 `expectation`**。若期间配置被改动 → `stale_plan`，回到步骤 2 重新展示。
5. 成功后提示「请重新读取程序列表」以刷新 `chassis_type`。

**不可逆点**：

- 步骤 3 的 `follow_default` → `follow_asset` 迁移**不可撤销**（`migrate_follow_default_refs` 无 undo，且它是 R8 既有语义）。迁移后借用从「跟随来源型号默认」变成「跟随指定程序」，来源型号改默认不再影响借入方——这是语义变化，必须在确认前告知。
- 步骤 4 的归一**不可撤销**（D10.2 同类）：被丢弃的 legacy 块名与落选的 `defaults` 取值不保留。确认对话框须列出「将被删除的块」与「每个模块最终取值」。
- 归一后该型号的机芯类型**不可再改**（D5.2）；改机芯类型 = 建新型号。确认前必须明示。
- **`平台配置.toml` 会被整体规范化重写**：文件内的自定义注释、以及当前模型不消费的未知 TOML 字段（`[[platform]]` 的 `name` / `defaults` 之外的键、顶层其他 table）**不保留**。确认对话框必须明示这一条。

  > 这不是归一新引入的行为——`save_platform_config` / `serialize_platform_config` 是应用对该文件的既有托管方式（文件头注释已写明「本文件由 fwasset 管理」），5a `set_asset_default` 与 R8 级联改写同样整体重写。归一只是把它放进了一次**不可逆且用户显式确认**的操作里，所以必须在确认范围内告知；不为此改 `serialize_platform_config`（那会波及 5a / R8 的既有写入口，超出本轮范围）。
  >
  > 也不因此拒绝归一：`平台配置.toml` 是应用托管文件，存在未知字段是历史遗留而非用户资产，拒绝会让这些型号永远无法归一、永远用不了「设为默认」。

  **预览须可见**：`PlatformNormalizePreview` 增字段 `discarded_content: list[str]`，列出将被丢弃的内容摘要——检出即列出，供确认对话框展示：

  | 来源 | 摘要形如 |
  | --- | --- |
  | 自定义注释（见下「注释识别」） | `注释 N 处` |
  | 顶层非 `platform` 的键或 table | `顶层字段「<键名>」` |
  | `[[platform]]` 块内 `name` / `defaults` 之外的键 | `块「<块名>」的字段「<键名>」` |

  **检出方式见 4.2a 的单次读取**：未知字段来自那一次解析产出的 `data`，注释来自同一份 `preimage` 文本。检出**不影响**归一能否执行，只影响告知。

  **注释识别规则**（`#` 在 TOML 里不止出现在行首，也可能在字符串里）：逐行扫描 `preimage` 文本，维护一个「是否在字符串内」的状态，按 TOML 词法跳过 `"..."` / `'...'` / `"""..."""` / `'''...'''` 中的内容；字符串外遇到的第一个 `#` 起至行尾即一处注释。覆盖三种形态：行首注释、缩进后注释、行尾注释。

  **排除应用自己的文件头**：`serialize_platform_config` 输出的固定两行——

  ```
  # 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。
  # defaults 键 = 通用区模块目录名，值 = 默认变体子目录名（空串表示该模块唯一）。
  ```

  排除条件三条**同时**成立：① 是文件中的**第 1、2 处注释**（按出现顺序）；② 与上面两行**按该顺序**整行精确相等（去尾随空白）；③ 该处注释独占整行（不是行尾注释）。其余位置出现相同文本一律计入——用户把这两行复制到文件中段，那就是用户的注释，会被丢弃，必须告知。不做前缀或模糊匹配。

  计数单位是「处」不是「行」：一行里最多算一处。

**可恢复性**（父规格工程尺度「删改必须能恢复」）：归一只改一个 TOML 文件，不动任何固件目录。用户若选错，可用 5a `set_asset_default` 逐模块重设默认；机芯类型选错则需按 D5.2 建新型号。确认对话框须写明这一点（子任务 8）。

## 8. 错误码汇总

| 码 | 入口 | 含义 |
| --- | --- | --- |
| `not_configured` / `root_changed` | 两者 | configured-root gate |
| `out_of_workspace` | 两者 | `model_root` 越界 |
| `invalid_model_root` | 两者 | 不是本工作区枚举出的型号根 |
| `platform_config_missing` | 两者 | 配置缺失或零块，无可合并内容 |
| `config_parse_error` / `parser_missing` | 两者 | 严格读取失败（含缺 `name` 的块，4.2a），保留原文件 |
| `reference_incomplete` | 两者 | 反查存在阻断级 issue |
| `follow_default_migration_required` | 两者 | 目标型号相关的残留 `follow_default` |
| `invalid_chassis_type` | 执行 | 不属 `ChassisType` 枚举 |
| `chassis_type_locked` | 执行 | 恰好一个枚举块且请求的机芯类型与现块名不同（D5.2，与 `already_normalized` 无关） |
| `module_choice_required` | 执行 | `value_conflict` 模块未给出选择 |
| `invalid_choice` | 执行 | 选择值不在候选中 |
| `invalid_args` | 执行 | `module_choices` 含无需选择的键 |
| `stale_plan` | 执行 | `expected` 重验不符（4.4.1）或 CAS preimage 变化 |
| `workspace_busy` / `recovery_required` | 执行 | 事务不可得 |
| `write_failed` | 执行 | 写入异常 |
| `platform_normalized_locked` | `set_default_variant` | 已归一型号上请求新建配置块 |
| `unchanged` (`ok=True`) | 执行 | 已归一，零写盘 |

## 9. 验收清单

### 9.1 归一预览

- [ ] 多 legacy 块（`默认` + `标准单机芯3D`）→ `blocks` 两条、`is_chassis_type` 均 False、`chassis_candidates` 为空。
- [ ] 块名恰为 `单3D` 时进入 `chassis_candidates`。
- [ ] 同 canonical 键跨块同值 → `kind == "unique"`，不进 `needs_module_choice`。
- [ ] 同 canonical 键跨块异值 → `kind == "value_conflict"`，进 `needs_module_choice`，`values` 排序去重。
- [ ] 同块内 `机芯版` / `机芯板` 同值 → `kind == "alias_duplicate"`（**不是** `unique`），`raw_keys` 两条，`already_normalized == False`。
- [ ] **跨块**别名同值（A 块 `机芯版`、B 块 `机芯板`、取值相同）→ `alias_duplicate`。
- [ ] 别名键 + 异值 → `value_conflict`（序 1 先于序 2）。
- [ ] 空串与非空串并存 → `value_conflict`，`values` 含 `""`。
- [ ] 已归一（单枚举块 + 全 `unique`）→ `already_normalized == True`。
- [ ] 单枚举块但含 `alias_duplicate` → `already_normalized == False`，执行后别名被归并为 canonical 键。
- [ ] `discarded_content`：行首、缩进后、行尾三种注释各 1 处 → 计 3 处。
- [ ] `discarded_content`：应用的两行文件头位于文件开头、按序精确相等时**不计**；手写的相似但不相等注释**计入**。
- [ ] `discarded_content`：同样两行文本出现在文件**中段**（前面已有别的注释）→ **计入**。
- [ ] `discarded_content`：`name = "a#b"` 这类**字符串内的 `#`** 不计为注释（含 `'...'` 与三引号形态）。
- [ ] `discarded_content`：顶层未知键 / 块内 `name`·`defaults` 之外的键 → 各自列出；无此类内容时为空列表。
- [ ] `discarded_content` 非空**不阻止**归一：执行成功，重写后文件确实不含这些内容。
- [ ] 预览不持锁：预览期间另一 `WorkspaceTransaction` 可正常获取锁（或断言 `workspace_lock_is_held` 为 False）。
- [ ] 预览不写盘：调用前后 `平台配置.toml` 字节与 generation 均不变。

### 9.2 `follow_default` 前置

- [ ] 另一型号有指向本型号的 `follow_default`（入向）→ 预览 `follow_default_blocked == True`；执行返回 `follow_default_migration_required`，零写盘。
- [ ] 本型号**作为借入方**跟随**别的型号**的 `follow_default` → **不阻止**本型号归一（出向不算；该条目会在来源型号归一时被拦）。
- [ ] 跑 `migrate_follow_default_refs` 成功后重新预览 → `follow_default_blocked == False`，执行成功。
- [ ] `unresolved`（来源程序已删除，迁移后仍是 `follow_default`）→ 仍阻止。
- [ ] 与本型号无关的第三、四型号之间的 `follow_default` → **不阻止**本型号归一。
- [ ] 阻断级 issue（如某型号 `型号配置.toml` 语法坏）→ `reference_incomplete`，零写盘。
- [ ] `migrate_follow_defaults_for_normalize` 的返回与直接调 `migrate_follow_default_refs` 逐字段相等。

### 9.3 归一执行

- [ ] 成功后文件恰好一个 `[[platform]]`，`name == chassis_type`，`defaults` 键全部 canonical。
- [ ] `value_conflict` 按 `module_choices` 取值；未给 → `module_choice_required`；给了非候选值 → `invalid_choice`；给了 `unique` 键 → `invalid_args`。
- [ ] 枚举外 `chassis_type` → `invalid_chassis_type`。
- [ ] 已归一 + 同名 → `unchanged`，字节与 generation 不变。
- [ ] 已归一 + 异名 → `chassis_type_locked`，零写盘。
- [ ] **单枚举块 + `alias_duplicate`** + 异名 → `chassis_type_locked`，零写盘（不得借清理别名改机芯类型）。
- [ ] **单枚举块 + `value_conflict`** + 异名 → `chassis_type_locked`，零写盘。
- [ ] 单枚举块 + `alias_duplicate` + **同名** → 成功，别名归并为 canonical 键，块名不变。
- [ ] 多 legacy 块（无枚举名块）+ 任意枚举 `chassis_type` → **不**触发 `chassis_type_locked`，正常归一。
- [ ] 多块且其中恰有一块是枚举名 → 不满足「恰好一个块」，**不**触发锁，正常归一（用户可选任意枚举值）。
- [ ] `missing` / 零块 → `platform_config_missing`。
- [ ] `parse_error` → `config_parse_error` 且**文件字节不变**。
- [ ] **含缺 `name` 的 `[[platform]]` 块** → 预览与执行均 `config_parse_error`，零写盘，该块的 `defaults` 不被静默删除（文件字节不变）。
- [ ] **非字符串 `name`**（数值 / 布尔）→ `config_parse_error`，零写盘，`detail` 指出该键。
- [ ] **非字符串 `defaults` 值**（数值 / 布尔 / 数组 / table）→ 各自 `config_parse_error`，零写盘，文件字节不变。
- [ ] `_read_platform_source` 对**全字符串**配置派生的 `platforms` 与 `load_platform_config_with_status` 的返回逐字段相等（无名块与非字符串值场景除外：后者容忍、前者拒绝）。
- [ ] 锁内 preimage 被改（monkeypatch 在 `begin_product_write` 后改文件）→ `stale_plan`，工作区状态回到 `clean`、generation 为偶数。
- [ ] `expected` 重验：预览后在磁盘上加一个新块 → 回传原 `expectation` 执行 → `stale_plan`，零写盘。
- [ ] `expected` 重验：预览后把某模块的取值改成第三个值 → `stale_plan`，零写盘。
- [ ] `expected is None` → 跳过该项比对，其余重验照常（`follow_default` 残留仍阻断）。
- [ ] CAS 覆盖的窗口：在锁内读取 preimage **之后**改写文件（monkeypatch `_cas_write` 前的钩子或注入写入）→ `stale_plan`，零写盘。
- [ ] 预览后仅改注释、块与取值不变 → 归一**成功**（该窗口不拦；归一整体重写该文件，自定义注释本就不保留）。
- [ ] 全部零写盘失败分支后 `load_workspace_status(ws).state == "clean"` 且 generation 为偶数。
- [ ] 成功后 `state == "clean"`、generation 偶数、操作日志已清除。
- [ ] `payload["chassis_type_changed"]` 在 legacy → 枚举名时为 True。
- [ ] `model_root` 不在工作区 → `out_of_workspace`；在工作区但无型号标志 → `invalid_model_root`。
- [ ] 归一不改动任何固件目录（前后目录树快照相等）、不改 `型号配置.toml`、不改任一 `方案配置.toml`（含其 `platform` 字段原样保留）。

### 9.4 与 5a 的衔接（不得改坏）

- [ ] 归一前 `set_asset_default` 返回 `platform_not_normalized`；归一后同一调用成功。
- [ ] 残留 `follow_default` 时 `set_asset_default` 仍返回 `follow_default_migration_required`（5a 行为不变）。
- [ ] 归一后仍存在同值别名时 `set_asset_default` 的 `canonical_duplicate` 不被触发（归一已消除别名）。

### 9.5 D5.4 边界（不得放宽/收紧）

- [ ] 多块型号上 `build_clear_defaults_plan` 仍按 `block_index` 命中签发计划（6a/6b 既有用例保持绿）。
- [ ] **端到端**：多块型号上对通用区程序做 R8 `rename`，`build_rewrite_plan` + `apply_rewrite_plan` 成功，且只改写命中 `block_index` 的那一块——其余块的 `defaults` 字节级不变。
- [ ] **端到端**：多块型号上走 6a `update_asset`（普通 update）成功，`平台配置.toml` 的非命中块不变。
- [ ] 上两条在本轮改动**前后**均通过（证明第 5 节的低层 API 收紧没有间接收紧 R8 路径）。
- [ ] 多块型号上 `update_asset_vendor`（不碰平台配置）仍可成功。
- [ ] 已归一型号上 `set_default_variant` 用别的块名 → `platform_normalized_locked`，文件不变。
- [ ] 已归一型号上 `set_default_variant` 用该块名 → 成功写入，仍是一个块。
- [ ] 未归一型号上 `set_default_variant` / `set_module_default_for_model` 既有用例全部保持绿。
- [ ] 已归一型号上 `ensure_platform_blocks` 不因方案 `platform` 追加块。

### 9.6 legacy `"旧"` 退役

- [ ] `SCAN_EXCLUDE_DIR_KEYWORDS` 不含 `"旧"`，仍含其余四项。
- [ ] `旧款L36/通用/<模块>/<程序>` 能被 `scan_firmware_assets` 扫描成资产。
- [ ] `<程序>/旧版本/<副本>` 仍被排除（`managed_paths` 精确段）。
- [ ] `旧版本说明/` 仍**不**被排除（既有断言保持绿，且不再需要 monkeypatch 移除 `"旧"`）。
- [ ] `usb_ops` 复制仍排除 `旧版本/` 与 `程序信息.toml`（既有用例保持绿）。
- [ ] `admission.validate_new_path` 允许新建 `旧款L36`，仍拒绝 `旧版本`。
- [ ] `detect_workspace_layout`：工作区根下 `旧款L36/` 带型号标志 → 计入型号；不带标志 → `invalid`。
- [ ] `enumerate_model_roots` 枚举出 `旧款L36`。
- [ ] `scan_legacy_excluded_dirs` 在**仍含**关键词的前提下（测试内 monkeypatch 或直接传 `keyword`）能列出命中目录、标记 `is_retired_versions`、命中后不下钻、`OSError` 不抛。
- [ ] `scheme_config._is_excluded_dir("旧款L36")` 为 False；`("backup"/"temp"/"tmp"/"x-back")` 仍为 True。
- [ ] `discover_schemes` 在 `旧款L36/定制/<方案>/` 布局下能发现方案（型号级子目录跳过那一处，`scheme_config.py:84`）。
- [ ] `discover_schemes` 在 `<型号>/定制/旧方案/` 下能发现该方案（方案目录自身跳过那一处）。

### 9.7 通用

- [ ] `uv run ruff check src scripts` 通过。
- [ ] `uv run mypy` 通过。
- [ ] `uv run python -m pytest -q` 通过，覆盖率 ≥ 80%。
- [ ] 不改 `ui_qt/` / `ui_common/`；不改 5a/6a/6b 公共契约；不改 `_infer_asset_context`。

## 10. 人工验证判据

按父规格「自动化能全断言就直接提交」：本子任务**全部为服务层与纯函数**，无 UI、无真实设备、无用户操作界面依赖，磁盘状态与返回码均可在 `tmp_path` 完整树上断言。

但有一处**必须人工确认**：`"旧"` 关键词退役会改变用户真实工作区的可见目录集合与布局判定。该影响不在本轮代码验证范围（不读真实工作区数据），由用户在子任务 8 的实机验收中确认；本轮在提交说明与迁移说明中写明需重扫。

据此：**自动化通过即可提交**，无需停在 staged 等人工验证。

## 11. Task DoD

- [ ] 规格经独立审查 PASS
- [ ] TDD：先 RED 后 GREEN
- [ ] 第 9 节验收清单全部落为断言并通过
- [ ] 第 6.3 / 第 7 节迁移说明已写入提交说明
- [ ] 父规格状态表更新为「7 已完成、8 待实施」（提交时一并）
- [ ] 中文 Conventional Commit，只暂存本任务文件，不 push
