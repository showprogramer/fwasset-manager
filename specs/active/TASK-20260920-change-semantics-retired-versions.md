# TASK-20260920 子任务 6b：改类型/改范围复合操作（D1.1）+ 旧版本备用副本与恢复交换（D4）

- 状态：实现完成，审查通过（规格 r6；6B-IMP-013 已闭合）
- 日期：2026-09-20
- 父规格：specs/active/TASK-20260903-crud-write-semantics.md（D1.1 / D1.3 / D1.4b-c / D4 / D8 / D10；子任务 6b 条目）
- 上游：TASK-20260920-layout-normalize-update.md（6a，已实现待审查）。本轮只消费其**规格已写出的公开契约**与父规格已冻结规则；6a 审查裁定前，不把实现与规格的签名偏差、私有 helper 当作契约（6B-001）。
- r1 → r2：`_default_program_dir` 的 `dir_keywords` 回退改为必做。
- r2 → r3：闭合 6B-001 / 6B-002 / 6B-003。
- r3 → r4：闭合 6B-004 / 6B-005。
- r4 → r5：闭合 6B-007；6B-006 部分闭合。
- r5 → r6：6B-006 补全——`intended_firmware_type` 无法判定时仍须写入空字符串键，不得省略。
- 实现：`change_asset_semantics` / `restore_retired_version` / `retire_asset_to_backup` 落在 `layout_update_service`；6a `update_asset` 的 backup 分支改走 D4.2 原语。
- 关联但本轮不得修改：6a Task / 5a Task / 父 Task / 任何既有 Review。

## 1. 目标与非目标

### 目标

交付两个 D8.3 协调器清单内的复合写入口，以及它们共用的 D4.2 退位原语：

1. **D1.1** `change_asset_semantics`：改类型 / 改范围。不走 R8 `update` 级联（`unsupported_semantic_change` 保持阻断）。借用条目按 D1.2 零改写保留；平台 default 只通过 6a 已导出的 `build_clear_defaults_plan` + `apply_rewrite_plan` 清除。
2. **D4.2 / D4.4** `restore_retired_version`：把 `旧版本/` 副本恢复到 `retired_from`，并与当前程序做 R8 update 交换。
3. **D4.2 原子退位原语** `retire_asset_to_backup`：`retire_to_backup` 的唯一写入口（D1.1、D4.4 步骤 3、以及 6a `update_asset` 的 backup 分支都必须走它）。「旧程序内容 + 副本元数据」是一个提交单元。
4. **D4.3 消费、不重做**：扫描/USB 排除沿用 `managed_path_reason` / `should_exclude_managed_path`；恢复路径按 reason=`retired_versions` **允许读取** `退位信息.toml`。
5. **D4.4 `retired_anchor` issue**：借用锚点落在 `旧版本/` 段内时发出专用 issue。**不**加入全局 `BLOCKING_ISSUE_CATEGORIES`；查询目标与 `anchor_path` / `source_root` **有边界关系时本次阻断**，无关目标只 warning（6B-003）。
6. **`_default_program_dir` 的 catalog `dir_keywords` 回退**（父任务 6b 原文必做，属 `find_references_to` 公共反查语义）：canonical 名未命中模块目录时，才用 scanner/catalog **现有**匹配语义确定目录；catalog 不可用或无**唯一**匹配时保留原合成路径 `通用/<module_key>/[<variant>]`。须同步补 rename、delete 预检与 D1.1 clear-defaults 的关键词目录回归。不把 `主板` 写进 `canonical_module_dir` 别名表。

工程尺度沿用父规格 2026-09-18 裁决（适用于子任务 4–8）：不做受签发入口防伪造、不做逐行崩溃注入（只测主要阶段）、不做细粒度 TOCTOU（写锁 + 锁内重验即可）。

### 非目标

- 不实施 UI（子任务 8）。旧 UI 不得直接写 TOML。
- 不改 R8 删除零改写、`follow_asset` 锚定、`unsupported_semantic_change` 阻断。
- 不重做 D0.3 / D1.4a / D1.3（6a）；不另起平行的 clear-defaults 计划结构或 token。
- 不重做 D4.3 helper 与 D4.5 USB `ignore`（子任务 1 已落地）；不退役 legacy `"旧"` 关键词（D4.3③，子任务 7）。
- 不实施「删除备用副本」及其 D10.1 撤销（删除类，归删除入口 / 子任务 8）。
- 候选项不持久化改类型/改范围意图、旧资产路径或冻结计划（D7.5；补齐后用户经 `promote_candidate` 重新选择落点）。
- 不把 `主板` 等 catalog `dir_keywords` 扩进 `canonical_module_dir`（`_MODULE_KEY_ALIASES` 仍只覆盖快捷键三词 + 机芯版→机芯板）。关键词目录命中只发生在 `_default_program_dir` 的回退分支。
- 本轮不实施 UI（子任务 8）。

## 2. 依赖的 6a 公共入口（仅 API，不当作未审查内部行为）

| 符号 | 模块 | 本轮用法 |
| --- | --- | --- |
| `build_clear_defaults_plan(configured_root, workspace_root, old_path, change_kind) -> ServiceResult` | `reference_service` | D1.1 锁内步骤 2 唯一 defaults 计划入口。签名无 `hits`。成功时 `payload["plan"]` 为既有 `RewritePlan`（`_plan_token` 签发），空计划也是成功。D5.4：多 `[[platform]]` 块只要命中带明确 `block_index`，必须签发并清理这些命中，**不得**因「多于一块」本身返回 `platform_not_normalized`。仅配置损坏（`config_parse_error`）或反查不完整（`reference_incomplete`）才拒绝。 |
| `ClearDefaultsKind` | `types.py` | `"change_type" \| "general_to_custom" \| "custom_to_general" \| "custom_scheme_move"`。四种行为已由 6a 定死：前两种清命中 `block_index`，后两种空计划。 |
| `apply_rewrite_plan(plan, configured_root, log_fn)` | `reference_service` | D1.1 清 defaults；D4.4 的 R8 update。成功 `ok`；整批失败且回滚干净 `rolled_back`；回滚冲突 `rollback_conflict`；preimage 变化 `stale_plan`。 |
| `update_asset`（6a 规格 §7） | `layout_update_service` | 公开契约以 6a **规格**为准：`old_asset: FirmwareAsset`。实现目前收路径是 6a 自留的审查线索，**不是** 6b 可依赖的冻结 API（6B-001）。本轮不重开 D1.4a 状态机；6a 审查冻结后，仅要求其 `retire_to_backup` 改走第 6 节原语。 |
| `normalize_module_leaf` / `normalize_required` | 同上 | D1.1 遇未归一模块叶子直接 `normalize_required`，不隐式归一。 |
| `delete_recorded_product` | `staging_io` | D1.4c 身份 + manifest + 日志状态三条件；冲突抛 `ProductCleanupConflict`，禁止递归删除。 |
| `register_retire` | `quarantine` | 仅 `retire_to_trash`。`expires_at=0`，不展示撤销。 |
| `WorkspaceTransaction` | `workspace_transaction` | `set_phase` / `record_product` / `begin_product_write` / `commit`；未 commit 的 `__exit__` → generation 偶数 + `recovery_required`。 |
| `promote_import` / `stage_import_directory` | `import_io` | D1.1 的 staging 与容器创建。`promote_import` 会再调 `begin_product_write`（奇数 generation 时是 no-op）。 |
| `RETIRED_VERSIONS_DIRNAME` / `managed_path_reason` / `should_exclude_managed_path` | `managed_paths` | 目录名与排除。元数据文件名本轮新增常量，禁止再写字面量 `"旧版本"` / `"退位信息.toml"`。 |
| `directory_manifest` / `directory_manifest_hash(..., exclude_names=)` | `manifest` | D4.2 content hash：相对路径 + 长度 + 内容 SHA-256，**排除元数据文件、不含 mtime**。`exclude_names` 正是为此预留。 |
| `validate_new_path` / `find_references_to` / `check_reference_gate` / `reconcile_subtree` / `load_workspace_status` | 既有 | 准入、反查、gate、对账、收敛断言。 |

6a 私有 `_retire_to_backup` **不是**本轮契约：6a 规格只承诺最小字段，且实现顺序未满足 D4.2 原子边界。本轮以第 6 节原语作为此后唯一 backup 写入口；6a 审查冻结后的 `update_asset` 公开签名（无论裁定为 `FirmwareAsset` 还是路径）都只是调用方形状，不改变原语参数。

6a 规格已决、可沿用：`reindex_failed` 为 `ok: True` + `commit()` 成 `clean`（6a §14.3）；锁内尚未写盘的拒绝必须 `commit()` 成 clean 偶数 generation。`normalize_required` 的语义以 6a 规格为准。

## 3. 公共入口与类型 / 服务契约

落点：`core/services/layout_update_service.py`（与 6a 同模块，不另起平行服务）；D4.2 元数据与 `RetireMode` 升到 `core/types.py`；元数据文件名进 `managed_paths.py`；`retired_anchor` 与 `_default_program_dir` 回退均在 `reference_lookup.py`（不改 `canonical_module_dir`、不改 scanner）。测试新建 `tests/test_change_semantics.py` 与 `tests/test_retired_versions.py`；关键词目录回归落在既有 `tests/test_reference_lookup.py`，rename 计划回归可落 `tests/test_reference_service.py`。不改写 6a 已有用例正文（若替换 `update_asset` 的 backup 实现，既有「落在 `旧版本/`、不进隔离区」断言必须仍绿）。

```python
RetireMode = Literal["retire_to_trash", "retire_to_backup"]  # types.py 真源

class RetiredVersionMetadata(TypedDict):
    retired_from: str       # 退位时的原始身份路径（绝对路径字符串）
    content_hash: str       # 排除 退位信息.toml 的 directory_manifest_hash
    retired_at: str         # UTC `YYYY-MM-DDTHH:MM:SSZ`
    retired_by: Literal["update_asset", "change_asset_semantics", "restore_retired_version"]

RETIRED_METADATA_FILENAME = "退位信息.toml"  # managed_paths.py

def change_asset_semantics(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_asset: str | Path,
    source: str | Path,
    new_path: str | Path,
    change_kind: ClearDefaultsKind,
    *,
    retire_mode: RetireMode,
    vendor: str = "",
    log_fn: Callable[..., None] = print,
) -> ServiceResult: ...

def restore_retired_version(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    backup_path: str | Path,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult: ...

def retire_asset_to_backup(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    old_path: str | Path,
    current_asset_path: str | Path,
    *,
    retired_by: Literal["update_asset", "change_asset_semantics", "restore_retired_version"],
) -> Path:
    """D4.2 唯一 backup 写入口。返回正式副本目录。须已持写锁且已 begin_product_write。"""
```

D1.1 的 `old_asset` 收路径是**本入口自己的契约**（锁内冷扫，不信任调用方资产字典），不声称与 6a `update_asset` 签名同构，也不把 6a 实现偏差写成已决依赖（6B-001）。`backup_path` 同理。

`vendor` 仅在落入待补齐区时写入候选元数据（完整成功路径不改旧程序 `程序信息.toml`）。取值：调用方传入非空则用传入值；否则冷读旧程序 `程序信息.toml` 的 `vendor`；再缺则 `""`。不得猜厂商名单。

成功 payload：

- D1.1：`old`、`replacement`（即 `new_path`）、以及 `quarantine_id` **或** `backup`（互斥，见第 5 节）。
- D4.4：`restored`（`retired_from`）、`retired`（当前程序的新副本路径）。

用户消息一律中文。不以裸异常代替服务错误码。两个入口均不可撤销（D10.2）；`retire_to_trash` 不展示撤销；恢复的替代路径是再交换一次。

### 3.1 `change_kind` 与路径的对应（锁内外同一规则）

由旧路径与 `new_path` 派生语义（复用 `reference_service._derive_semantics`，不另造字段规则）。必须同一 `model_id`。调用方声明的 `change_kind` 必须与派生结果**精确匹配**，否则 `change_kind_mismatch`（零写盘）。

| `change_kind` | 派生条件 | defaults（交给 D1.3，本入口不重写规则） |
| --- | --- | --- |
| `change_type` | `module_key` 变化；category / scheme **允许同时变** | 清除旧 canonical 键命中 |
| `general_to_custom` | 旧在 `通用/`、新在 `定制/<方案>/`，且 `module_key` **不变** | 同样清除命中 |
| `custom_to_general` | 旧在定制、新在通用，且 `module_key` 不变 | 空计划，不自动设默认 |
| `custom_scheme_move` | 同型号两定制方案之间，`module_key` 不变 | 空计划 |

**D5.4（6B-007）**：型号根有多个 `[[platform]]` 时，D1.1 **不得**整型号封锁。`build_clear_defaults_plan` / apply 必须清掉反查给出的每一个精确 `block_index` 命中（可跨块），不误清未命中键。多块本身不是 `platform_not_normalized`。该码仅保留给 6a 规格已定义的「平台结构不合法」（例如无法解析为合法块列表）；命中集不完整才 `reference_incomplete`。D1.1 不得在 D1.3 外包一层「见多块就拒」。

`new_path` 必须是 D0.2 资产路径：`通用/<模块>/<程序名>` 或 `定制/<方案>/<模块>/<程序名>`。方案目录必须已存在。模块容器可缺，由 `promote_import` 创建；回滚时仅 `rmdir` 空目录。`new_path` 与 `old_asset` 不得同一身份。来源是目录；完整性按**最终落点**的 catalog 语境判定（D1.5）。

未归一模块叶子 → `normalize_required`。`build_rewrite_plan(operation="update")` 不得出现在 D1.1 主路径。

### 3.2 D4.4 恢复的语义前提

恢复只允许在「当前程序」与 `retired_from` **R8 语义相同**（同一 `model_id` / `module_key` / `scheme_name`）时走 R8 update。语义不同（例如 D1.1 改类型后的 backup）→ 把副本移回 `旧版本/`，返回 `unsupported_semantic_change`。逆向改类型/改范围走再一次 D1.1，不塞进 D4.4。

### 3.3 `_default_program_dir`：canonical 未命中才回退 catalog（必做）

公开行为仍通过既有入口消费，不新导出写入口：

| 入口 | 本轮要求 |
| --- | --- |
| `reference_lookup._default_program_dir(model_root, module_key, raw_value)` | 唯一改动落点。`module_key` 已是 canonical（如 `主板程序`）。 |
| `find_references_to` / `_collect_defaults_hits` | rename 预览与 `delete_asset` 预检共用；关键词模块目录上的 `platform_default` 必须命中。 |
| `build_rewrite_plan(operation="rename")` | 级联 defaults 改写必须包含该 hit（rename 回归）。 |
| `build_clear_defaults_plan`（6a 已导出） | D1.1 `change_type` / `general_to_custom` 必须清到该 hit，不得空计划。 |

匹配顺序（硬约束）：

1. 在 `model_root/通用/` 下，先找 `canonical_module_dir(子目录名) == module_key` 的目录。命中即用，**不再**看 `dir_keywords`（canonical 与关键词目录并存时以 canonical 为准）。
2. **仅当步骤 1 未命中**：用 scanner/catalog **现有**语义在通用区子目录中找模块目录——复用 `_catalog_context` + `_is_catalog_asset_dir` / `file_scan._match_catalog_type`（catalog 顺序、`dir_keywords` 在路径段内、`handcontrol_ui` 的 `.rom+.pkg`）。匹配条件：命中条目的 `canonical_module_dir(label) == module_key`。
3. catalog 不可用（`_catalog_context()` 为 `None`）、或步骤 2 命中 **0 个或 ≥2 个** 子目录 → **不**把关键词目录静默当命中，返回原合成路径 `model_root/通用/<module_key>`，有 `raw_value` 再拼变体段。
4. 禁止另造一套字符串包含规则，禁止把 `主板` 写入 `_MODULE_KEY_ALIASES`（`canonical_module_dir("主板")` 仍是 `"主板"`）。

例：仅有 `通用/主板/v1`、defaults `主板程序 = "v1"`、catalog 可用且该目录唯一匹配 mainboard → `_default_program_dir(..., "主板程序", "v1")` 指向 `通用/主板/v1`。同一工作区若同时存在 `通用/主板程序/v1`，步骤 1 已命中 canonical，忽略 `主板`。

## 4. 锁外预览与锁内重验

用户选择一律在锁外完成；等待确认期间**不得**持 `WorkspaceTransaction` / 写锁（D8）。锁内只重验，不沿用锁外计划对象（`RewritePlan.token` 含进程盐，跨调用本来就不能重放）。

### 4.1 D1.1

锁外（只读，可供子任务 8 直接组合，本轮不另做 preview 写入口）：

1. `check_reference_gate`。
2. 冷扫 `old_asset` 为唯一完整合法资产；`source` 为目录且不在受管排除区。
3. `find_references_to(old, "asset")`：展示借用命中（D1.2：提交时不改这些条目）与 defaults 命中。若返回 `retired_anchor` 且与 `old` 有边界关系（第 7.2 节）→ 锁外即可报 `retired_anchor`，锁内同样拒绝。
4. `build_clear_defaults_plan(...)`：展示将清除的 `block_index`；空计划也要展示「无需清理」。
5. 用户提供 `new_path`、`change_kind`、`retire_mode`（不预选）。
6. 磁盘空间：工作区所在卷可用空间 < 来源树字节数 → 预览即可报 `insufficient_space`；锁内再测一次。

锁内：

1. 事务 `operation="change_asset_semantics"`。状态不是 `clean` 或 generation 为奇数 → 既有 `recovery_required` / `workspace_busy`。
2. 重验 gate、路径守卫、旧资产仍唯一完整、`source` 仍可读、`change_kind` 与派生语义仍匹配、`new_path` 仍通过 D3（`validate_new_path`）、磁盘空间仍足够；再次 `find_references_to(old)`，有边界关系的 `retired_anchor` → `commit()` 后返回该码。
3. **全部只读通过后**才 `begin_product_write()`，然后 staging。重验失败必须 `commit()` 成 clean 偶数 generation（MSC-001 同构）。
4. 步骤 2 的 defaults 计划在锁内**重新** `build_clear_defaults_plan`（builder 自己冷读，不接受 hits、不接受锁外 plan）。把 `change_kind`、新旧路径、`retire_mode`、计划文件的 preimage sha256 写入 `details`（冻结的是摘要，不是带盐 token）。
5. `stale_plan`：锁内 rebuild 失败（配置已变、反查不完整等）按对应错误码返回，尚未提升则 `commit()` 清理 staging。

### 4.2 D4.4

锁外：读 `退位信息.toml`。必须已有非空 `retired_from`（或可映射的 `original_path`）**以及**非空 `content_hash`；缺任一字段 → `retired_metadata_invalid`，不进入确认。展示 `retired_from`、当前程序、`find_references_to(current, "asset")`；确认恢复不可撤销。

锁内（6B-005，身份字段与目标路径分开说）：

1. 重读 toml：`retired_from`（或已映射的 `original_path`）仍非空，且与锁外预览的身份字符串相同；缺或变化 → `retired_metadata_invalid` / `stale_plan`。
2. **禁止**用当前目录内容补写或补全缺失的 `content_hash`。有完整 hash 时按 `exclude_names={RETIRED_METADATA_FILENAME}` 重算，不一致 → `content_hash_mismatch`。
3. 副本仍在 `backup_path`。
4. **`retired_from` 命名的目标路径仍为空**（不存在、也无大小写等价占用）。命中该原路径的历史锚点走 D3 窄化例外（预期恢复）；仅 plan 之外的重叠锚点才 `path_identity_conflict`。
5. 当前程序仍是唯一完整资产，且与 `retired_from` R8 语义相同。

失败且未搬副本 → `commit()` 成 clean。

## 5. `retire_to_trash` 与 `retire_to_backup`（不得合并为一种记录）

| | `retire_to_trash` | `retire_to_backup` |
| --- | --- | --- |
| 调用 | `register_retire(ws, old)` | `retire_asset_to_backup(...)`（第 6 节） |
| 落点 | 隔离区 `transactional_retire` | `<当前程序>/旧版本/<旧名>-<时间戳>/` |
| 元数据 | 隔离清单（既有 `QuarantineRecord`） | 副本内 `退位信息.toml`（D4.2） |
| 生命周期 | 提交成功后异步送系统回收站；`expires_at=0` | **不进隔离区、不送回收站**，用户长期保留 |
| 撤销 | 不展示、不承诺（D10.2） | 不展示；用户替代路径是 D4.4 再交换 |
| 失败 | 与 backup 相同：退位失败不得撤销已成功的引用/defaults 改写（D1.1 步骤 6 另有 CAS 恢复 defaults） | 同左的「禁止旧已退位但新不可用」 |

`<当前程序>` 是退位完成后仍在业务树上的那一个：D1.1 / D1.4a 是 replacement；D4.4 是刚恢复出来的程序。**禁止把旧程序放进它自己的 `旧版本/`**（自包含移动）。实现必须是两条独立分支与两种记录；测试分别断言隔离清单与 `旧版本/` 目录，互斥。

D1.1 在清 defaults **成功之后**才退位。D4.4 在 R8 apply **成功之后**才把当前程序退入恢复后程序的 `旧版本/`（固定 backup，无 trash 选项——父规格步骤 3）。

## 6. D4.2 元数据与原子边界

不变量：**不得产生没有合法 `retired_from` 的正式副本。** 正式路径 = 当前程序目录下 `旧版本/<旧名>-<时间戳>/`（重名加 `-2` 递增）。

写入顺序（持写锁、已 `begin_product_write`）：

1. `allocate_staging_area` 得到 temp。
2. 将 `old_path` **整树移入** temp（同卷 `os.replace` / move；失败则 old 仍在原处）。
3. 在 temp 内按 `exclude_names={RETIRED_METADATA_FILENAME}` 计算 `content_hash`；写入 `退位信息.toml`（UTF-8，仅第 3 节四个字段）；再读回校验：能解析、`retired_from` 非空且等于本次 old 身份、`content_hash` 与刚算的一致。
4. 校验失败：temp → 原 `old_path`（仅当原处仍空），返回 `retire_failed`；原处已被占用 → `retire_failed` + 保留 temp 路径于 payload，**禁止递归删 temp 里未知内容**。
5. 校验通过：`os.replace(temp, 正式副本路径)`。这是唯一使副本出现在 `旧版本/` 下的步骤。
6. 不得先出现正式目录再补写 toml；不得在正式目录上二次追加字段。

`retired_from` 存绝对路径字符串（与隔离记录 `original_path` 同风格）。`retired_at` 用 UTC。本轮原语写出的正式副本必须四字段齐全；**不得产生没有 `content_hash` 的正式副本**。

**6a 最小字段副本不可经 D4.4 恢复（6B-002）**：6a 规格只写 `原路径` + `退位时间戳`，没有可信的内容 hash。父规格 D4.4「元数据不符仍阻止」。缺 `content_hash` 时：

- **不得**按当前文件补算 hash 再放行（退位后被第三方改写将无法识别）。
- **不得**把补算结果写回旧 toml 当作迁移成功。
- 有 `original_path` 无 `retired_from` 时，身份字段可映射，但只要缺 hash 仍返回 `retired_metadata_invalid`，零搬动。
- 无身份字段（既无 `retired_from` 也无 `original_path`）同样 `retired_metadata_invalid`。
- 完整四字段且 hash 与当前文件一致才允许提升；hash 在但内容已变 → `content_hash_mismatch`。

受控迁移范围仅限：本轮 `retire_asset_to_backup` 新写出的副本。不遍历、不升级历史最小字段副本。那些目录仍可被扫描排除、可被删除入口处理（子任务 8 / D10.1），但不能走恢复交换。

## 7. D4.3 排除与 D4.4 恢复交换

### 7.1 D4.3 消费

- scanner / USB / `FirmwareAsset.files`：`should_exclude_managed_path` 为真则跳过。`旧版本` 是精确目录段（大小写不敏感），同名**文件**不误排，`旧版本说明` 前缀不误排（既有测试，本轮不复制规则）。
- 本轮恢复：`managed_path_reason(..., is_dir=True) == "retired_versions"` 时**允许**打开 `退位信息.toml`。排除 ≠ 写授权，写 `旧版本/` 只能通过第 6 节原语。
- 提升到 `retired_from` 之后该目录不再含 `旧版本` 段，扫描应重新得到完整合法资产。

### 7.2 `retired_anchor` issue（6B-003）

在 `find_references_to` / 共享引用解析中：若解析出的锚点路径任一目录段为 `旧版本`，追加 `LookupIssue(category="retired_anchor", ...)`，固化 `anchor_path`、`source_root`、引用方 `config_path`/`owner_root`、`raw_key`（可用现有字段 + `detail`，或给 `LookupIssue` 加默认空串的可选字段，保持旧构造可用）。

**不**把 `retired_anchor` 加入全局 `BLOCKING_ISSUE_CATEGORIES`（否则不相关型号的全工作区反查会一起失败）。阻断按**本次查询目标**判定：

- **有边界关系 → 本次阻断**：查询 `target` 与该 issue 的 `anchor_path` 或 `source_root` 满足 `same_path_identity`，或一方位于另一方子树内（`is_within_boundary` / 祖先-后代）。`find_references_to` 返回 `ok: False`、`code="retired_anchor"`。`delete_asset`、rename 预检、`change_asset_semantics`、以及 6a 冻结后的 `update_asset` 预检必须拒绝，不得只看 `BLOCKING_ISSUE_CATEGORIES`。
- **无关目标 → 只 warning**：无上述路径关系时 `ok: True`（除非另有真正的阻断级 issue），issue 仍出现在 `issues` 里供 UI 展示。
- **新建借用**不得选该来源：5a `register_shared_module` 已对受管排除返回 `retired_anchor`；本轮不改 5a 文件。
- **D4.4 恢复例外**：正在恢复的那份副本上的 `retired_anchor` 是身份延续的预期态，不阻断本次 `restore_retired_version`。恢复完成后锚点应落回 `retired_from`，不再带 `旧版本` 段。

### 7.3 恢复状态机（`operation="restore_retired_version"`）

全程写锁。`begin_product_write()` 在第一次搬动副本之前。

1. 锁内重验（第 4.2 节）。
2. 把副本从 `当前/旧版本/<名-戳>/` 经 staging **提升到 `retired_from`**（不能把目录移进自身后代，必须借道 temp）。`record_product(retired_from, manifest)`。此时当前程序与恢复程序同为现存合法资产。
3. `build_rewrite_plan(operation="update")` + `apply_rewrite_plan`。
4. apply 成功后，把**当前程序**经第 6 节原语退入 `retired_from/旧版本/`。
5. 两路径最小共同现存祖先 `reconcile_subtree`。

失败补偿（父规格表，禁止「apply 成功后把副本移回」造成断链）：

| 失败点 | 处理 | 返回 | 收敛 |
| --- | --- | --- | --- |
| 步骤 2 之前（含重验） | 零搬动 | 对应错误码 | `commit()` clean |
| 步骤 2 之后、步骤 3 apply 之前 | 副本移回原 `旧版本/` 路径（目标须仍空；否则保留现场） | `plan_build_failed` 或移回失败 `restore_inconsistent` | 移回成功则 `commit()`；否则 `recovery_required` |
| apply **完整回滚**（`rolled_back`） | 副本移回 `旧版本/` | `config_rewrite_failed` | 同上 |
| apply `rollback_conflict` | **停止移动**，不删恢复出的目录 | `restore_inconsistent` | `recovery_required` |
| apply 成功、步骤 4 退位失败 | 保留两个活动程序，**不得**把 restored 移回 | `retire_failed` | `recovery_required`（与 6a `update_asset` 退位失败同构） |
| 步骤 5 对账失败 | 磁盘为准，不回滚 | `reindex_failed`（`ok: True`） | `commit()` clean |

禁止结果：「当前已退位但 restored 不可用」。

## 8. D1.1 事务阶段与失败 / 补偿

`operation="change_asset_semantics"`。阶段名（`set_phase`）：`revalidate` → `freeze_clear_plan` → `stage_import` → `promote_replacement` → `apply_clear_defaults` → `retire_old` → `reconcile`。

`begin_product_write()`：第一次 staging 写入之前（与 6a `update_asset` 对齐）。步骤 2a：gate 全过之后、staging 之前，日志已由事务 `__enter__` 创建；再把冻结摘要写入 `details`。日志创建失败（进事务失败）则无产品变化。

锁内顺序：

1. 只读重验（第 4.1 节）。
2. 锁内 `build_clear_defaults_plan`；失败则 `commit()`，透传 D1.3 错误码。
3. staging 导入；按 `new_path` catalog 校验完整性（D1.5）。**不完整不得清掉用户内容**：把 staging 会话按子任务 5 的候选区路径提升到 `.fwasset/incomplete/`，调用既有 `save_candidate_metadata(candidate, vendor=..., intended_firmware_type=...)`，落盘后 toml **必须同时具备三键**：`vendor`、`intended_firmware_type`、`import_state="incomplete"`。`intended_firmware_type` 复用子任务 5 对 staging 文件名的判定（能判定为手控则 `"handcontrol_ui"`，否则 `""`）。**无法判定时仍写入 `intended_firmware_type = ""`，不得省略该键。** 若 `save_candidate_metadata` 对空串省略该键，D1.1 须随即补写空键，不另起平行元数据格式。`scan_incomplete_imports` 仍以 `import_state` 识别候选。`vendor` 见第 3 节。返回 `ok: True`、`code="created_incomplete"`，payload 含 `candidate`。旧程序、引用、defaults **均不变**；不提升到 `new_path`、不 apply 清默认计划、不退位。候选项**不**记录 `change_kind` / 旧路径 / 冻结计划（D7.5）。用户补齐后走既有 `promote_candidate` 重新选落点，不自动续跑本次复合操作。提升失败且会话仍在 staging → 清会话，`promote_failed`，`commit()` clean；会话已离开 staging → `recovery_required`。
4. `promote_import` 到 `new_path`（D3 + 缺模块容器 + `record_product`）。
5. `apply_rewrite_plan(clear_plan)`。
6. 仅当步骤 5 成功：按 `retire_mode` 退位。
7. 旧+新最小共同现存祖先 `reconcile_subtree`。

失败契约：

| 阶段 | 补偿 | 返回 | 收敛 |
| --- | --- | --- | --- |
| 1–2 与 staging 导入失败 | 清 staging；旧不动 | 常规码 / `insufficient_space` / `change_kind_mismatch` / `normalize_required` / `retired_anchor` | `commit()` clean |
| 3 catalog 不完整 | staging → 待补齐候选区；旧不动 | `created_incomplete`（`ok: True`） | `commit()` clean |
| 3 候选提升失败、会话仍在 | 清 staging | `promote_failed` | `commit()` clean |
| 3 候选提升失败、会话已走 | 保留现场 | `promote_failed` | `recovery_required` |
| 4 提升到 `new_path` 失败 | 清 staging；已建空模块容器仅 `rmdir` | 准入码 | 无产品残留则 `commit()`；容器非空 → `change_inconsistent` + `recovery_required` |
| 5 `rolled_back` 或 `stale_plan`（未写出） | **保留**已提升的新程序，旧不退位，defaults 仍原状 | `config_rewrite_failed` | `recovery_required`（复合操作未完成且两份并存，避免被当成成功 update） |
| 5 `rollback_conflict` | 停止，不删新程序 | `change_inconsistent` | `recovery_required` |
| 6 退位失败 | **CAS 恢复 defaults**：仅当平台文件当前字节仍等于 clear plan 的 postimage，才写回冻结的 preimage；成功则 defaults 回到旧程序 | `retire_failed` | `recovery_required` |
| 6 CAS 恢复冲突 | 不覆盖第三方改写 | `retire_failed_with_config_conflict` | `recovery_required` |
| 7 | 不回滚 | `reindex_failed`（`ok: True`） | `commit()` clean |

步骤 1–4 的「删除本次产物」复用 `delete_recorded_product`（D1.4c）。步骤 5 之后**禁止**再删新程序：引用/defaults 可能已指向它，或按父规格「新程序已就位」。

`apply_rewrite_plan` 需要进程内 `RewritePlan` 才能 CAS 回滚步骤 6 的 defaults。因此 `details` 必须另存平台文件的 `path + original_sha256 + original_text`（冻结摘要），供步骤 6 补偿与崩溃恢复在**没有**带盐 token 的情况下做字节级 CAS。不得把 `RewritePlan` 对象当跨进程恢复材料。

## 9. 崩溃恢复

`recover_interrupted_workspace` 现状：把非干净现场收敛为 `recovery_required` 并结束 seqlock，**不**自动续跑（编排归子任务 8）。本轮为两个 `operation` 各提供启动后续跑函数，由同一协调器日志驱动；第三方修改（产物 manifest ≠ 日志、`retired_from` 被占用、平台字节对不上冻结 preimage）→ 停止，返回恢复材料，不自动续跑。

检测到奇数 generation 时仍先走既有「恢复完成前预览与写一律 `workspace_recovery_required`」。本轮续跑在写锁内把 generation 收为偶数，再按阶段前进。

### 9.1 D1.1（日志 `operation="change_asset_semantics"`）

| 崩溃点 | 续跑 |
| --- | --- |
| 尚未 `begin_product_write` / 仅有 staging | 清本操作 staging；`commit()` clean |
| 新程序已提升、尚未 apply defaults | 锁内重建 `build_clear_defaults_plan` 并 apply，然后退位 |
| apply defaults 成功、旧仍在 | 继续退位（不把 defaults 撤回去，除非退位失败走第 8 节 CAS） |
| 旧已退位、尚未对账 | 只 `reconcile_subtree` |
| `retire_to_trash` 已完成、日志未清 | 对账（若需要）后清日志 |

「旧仍存在 → CAS 恢复 defaults，或继续退位」按阶段拆开：运行时步骤 6 失败才 CAS 撤 defaults；崩溃续跑在 defaults 已成功时**向前**退位（与 D1.4a「apply 成功则继续退位」同构）。

### 9.2 D4.4（`operation="restore_retired_version"`）

| 崩溃点 | 续跑 |
| --- | --- |
| 副本已离开 `旧版本/`、尚未 apply | 副本移回 `旧版本/`（目标空）；否则 `restore_inconsistent` |
| apply 成功、当前程序尚未退入新 `旧版本/` | 继续 `retire_asset_to_backup` |
| 当前已退位、尚未对账 | 只对账 |

主要阶段用临时目录夹具模拟「提升后中断 / apply 后中断」，不逐行注入。

D8.1 / D8.4：任何已改磁盘或 TOML 的收尾必须把 generation 推到偶数；`*_inconsistent` 仍留 `recovery_required`，新写全阻，只读预览带警告。产品数据变更前必须已非 `clean`。

## 10. 错误码

| 入口 | 码 | 含义 |
| --- | --- | --- |
| D1.1 | `normalize_required` | 旧资产仍是未归一模块叶子 |
| D1.1 | `change_kind_mismatch` | 声明的 `change_kind` 与新旧路径派生语义不符 |
| D1.1 | `insufficient_space` | 工作区卷可用空间不足 |
| D1.1 | `created_incomplete` | catalog 不完整，内容已落入待补齐区；旧程序未动（`ok: True`） |
| D1.1 | `promote_failed` | 候选区提升失败 |
| D1.1 | `config_rewrite_failed` | 清 defaults 失败且完整回滚，新程序已在、旧未退 |
| D1.1 | `change_inconsistent` | 回滚冲突或 D1.4c 不能安全清理，保留现场 |
| D1.1 / D4.4 | `retire_failed` | 新/恢复程序已可用，旧/当前退位失败，两份并存 |
| D1.1 | `retire_failed_with_config_conflict` | 退位失败且 defaults CAS 恢复冲突 |
| D1.3 透传 | `config_parse_error` / `invalid_target` / `reference_incomplete` | 锁内 rebuild 计划失败。多块本身不构成 `platform_not_normalized`（D5.4） |
| D4.2 / D4.4 | `retired_metadata_invalid` | 无身份字段，或缺 `content_hash`，或 toml 损坏无法解析；**不提升** |
| D4.4 | `content_hash_mismatch` | 元数据已有 hash，但与排除 toml 后的当前内容不一致 |
| 反查 / D1.1 / 删除预检 | `retired_anchor` | 查询目标与 `旧版本/` 锚点或来源有边界关系，本次阻断 |
| D4.4 | `restore_inconsistent` | apply rollback conflict，或补偿移回失败 |
| D4.4 | `unsupported_semantic_change` | 当前程序与 `retired_from` 语义不同（透传 R8 语义，副本已移回） |
| D4.4 | `path_identity_conflict` | 恢复目标命中 plan 之外的重叠锚点 |
| 共用 | `stale_plan` | 锁内重验发现配置/preimage 变化 |
| 共用 | `reindex_failed` | 对账失败，`ok: True`，提示重扫 |
| 共用 | `workspace_not_configured` / `workspace_mismatch` / `out_of_workspace` / `workspace_busy` / `recovery_required` | gate / 守卫 / 事务 |

准入失败透传 `validate_new_path` 的码（`path_exists` / `invalid_name` / `domain_violation` / `path_excluded` 等）。`retire_mode` 非法 → `invalid_args`。

## 11. 验收清单（均可自动断言）

- [x] D1.1 不调用 `build_rewrite_plan(operation="update")`；四种 `change_kind` 均走 `build_clear_defaults_plan`；`custom_to_general` / `custom_scheme_move` apply 后平台 defaults 字节不变。
- [x] `change_type` / `general_to_custom` apply 后仅命中 `block_index` 被清，同文件其他块/键保留；借用 `static`/`follow_asset` 条目仍在（D1.2）。
- [x] `change_kind_mismatch`、`normalize_required`、锁内重验失败：零写盘，`load_workspace_status.state == "clean"` 且 generation 偶数。
- [x] catalog 不完整：返回 `ok: True`、`code="created_incomplete"`；`scan_incomplete_imports` 能发现该候选；`程序信息.toml` **三键都在**：`import_state="incomplete"`、`vendor`（入参或旧程序，皆空则 `""`）、`intended_firmware_type`（手控缺文件为 `"handcontrol_ui"`，否则键存在且值为 `""`）。旧路径仍在；无隔离记录、无 `旧版本/`、平台 defaults 字节不变；候选项元数据不含 `change_kind` / 旧路径。不得把已导入内容随 staging 删掉。
- [x] 型号根两个 `[[platform]]` 均有 `主板程序=v1` 指向旧程序：`change_type` 成功后两块该键都清除，其它键保留，返回码不是 `platform_not_normalized`。
- [x] 步骤 5 `rollback_conflict` → `change_inconsistent` + `recovery_required`，新目录仍在；后续 `update_asset` / `change_asset_semantics` 得 `recovery_required`。
- [x] 步骤 6 退位失败 → `retire_failed`，两份并存，新程序可用；CAS 恢复冲突 → `retire_failed_with_config_conflict` 且平台文件未被本操作覆盖成第三份内容。
- [x] 禁止「旧已退位但新程序不可用」：任何失败分支若旧已不在业务树，则新路径必须仍是可冷扫的完整资产。
- [x] `retire_to_trash` 产生 `transactional_retire` 且成功 payload 无 `backup`；`retire_to_backup` 落 `当前/旧版本/<旧名>-…/`，隔离清单无 `transactional_retire`，正式副本 toml **同时含**可解析的 `retired_from` 与 `content_hash`。
- [x] D4.2：在「已移入 temp、尚未 `os.replace` 到 `旧版本/`」插入失败时，工作区 `旧版本/` 下不出现无 toml 的目录；old 仍在原处或 payload 给出 temp 路径。
- [x] D4.2 hash 不含 `退位信息.toml`、不含 mtime：只改元数据文件不改变 `content_hash`；改固件字节则变。
- [x] 扫描当前程序的 `FirmwareAsset.files` 不含 `旧版本/` 下文件；恢复服务能读同一路径的 toml。
- [x] `retired_anchor` 不在 `BLOCKING_ISSUE_CATEGORIES`。指向 `旧版本/` 的 shared 锚点必出现该 issue。查询目标与 `anchor_path`/`source_root` 有边界关系时 `find_references_to` 为 `ok: False` 且 `code="retired_anchor"`；`delete_asset` / D1.1 预检拒绝。无关目标 `ok: True`，issue 仅 warning。
- [x] 缺 `content_hash` 的副本（含仅有 `original_path`+`retired_at` 的 6a 最小字段）→ `retired_metadata_invalid`，`retired_from` 不出现新目录，旧副本仍在原 `旧版本/` 路径。不得把当前文件补算成 hash 后恢复。
- [x] 有完整 `content_hash` 但固件字节被改 → `content_hash_mismatch`，同样零提升。
- [x] D4.4 成功：副本在 `retired_from`，原当前程序在 `restored/旧版本/…`，引用/defaults 指向 restored，共同祖先对账后无旧路径幽灵行。
- [x] D4.4 apply 前失败：副本回到原 `旧版本/` 路径；apply `rollback_conflict` → `restore_inconsistent` 且 restored 目录仍在；apply 成功后退位失败 → `retire_failed`，两个活动程序，restored 不被移回。
- [x] 语义不同的 backup 恢复：`unsupported_semantic_change`，副本仍在（或已移回）`旧版本/`，当前程序未退位。
- [x] 恢复目标已存在非空内容 → 准入拒绝（`path_exists`），副本未提升。锁内重验：toml 里 `retired_from` 字段仍非空且与预览相同，同时该路径在磁盘上不存在；不得把「字段非空」写成「路径必须空」的反义混用（6B-005）。
- [x] 两个入口成功路径均不签发 undo token、不写 `undoable_delete`。
- [x] `_default_program_dir`：仅有关键词目录 `通用/主板`（无 `主板程序` 目录）、defaults `主板程序=v1` 时，解析到该 `主板/v1`；`canonical_module_dir("主板")` 仍为 `"主板"`。
- [x] 同上布局下 `find_references_to(asset)` 返回恰好一条 `platform_default`（`raw_key=="主板程序"`，`current_target` 为该变体）。这是 rename 预览与 `delete_asset` 预检的同一反查路径，两条消费链都必须覆盖。
- [x] 同上布局下 `build_rewrite_plan(operation="rename")` 的计划含该 defaults 条目（非空 `files`/`hits` 中有对应 `block_index`）；`delete_asset` 预检（其内部 `find_references_to`）同样带上该 hit，不得因漏匹配而显示「无人引用」。
- [x] 同上布局下 `build_clear_defaults_plan(..., change_kind="change_type")` 签发的计划含该 `block_index`；D1.1 成功路径 apply 后该键被清除（不得留下悬空 `主板程序=v1`）。
- [x] canonical 名已存在时不走关键词回退：`通用/主板程序` 与 `通用/主板` 并存时命中 `主板程序`。
- [x] catalog 不可用，或通用区有两个均可匹配同一 `module_key` 的关键词目录：返回合成路径 `通用/主板程序/<variant>`，不把任一关键词目录静默当命中；`find_references_to` 不因此崩溃。
- [x] 每个新增行为先有失败测试并观察到 RED，再写最小实现。

## 12. 测试计划（先 RED 后 GREEN）

顺序：每条先写失败测试并实际观察 RED，再最小实现。夹具可仿 6a `test_layout_update_service.py` 的型号/变体/`_source`，但 D1.1/D4 用例放在新文件以免与 6a 未审查用例缠在一起。关键词目录回归写在既有 `test_reference_lookup.py`（lookup / delete 预检）与 `test_reference_service.py`（rename 计划），D1.1 clear-defaults 写在 `test_change_semantics.py`。

0. `_default_program_dir` 回退（先于 D1.1，否则 clear-defaults 夹具只能用 canonical 名、盖不住缺陷）：
   - RED：仅 `通用/主板/v1` + defaults `主板程序=v1` 时，`find_references_to` 无 `platform_default` hit。
   - GREEN：命中该变体；再补 catalog 不可用 / 非唯一匹配仍合成 `通用/主板程序/v1`；canonical 目录优先。
   - rename：`build_rewrite_plan(operation="rename")` 含该 defaults 改写。
   - delete 预检：`delete_asset` 所用 `find_references_to` 带同一 hit。
   - D1.1：`build_clear_defaults_plan(..., "change_type")` 非空计划并清掉该键。
1. D4.2：`RetiredVersionMetadata` 读写；hash 排除元数据；原子边界（正式目录出现前 toml 已合法）。
2. D4.3 消费：扫描 files 不含 `旧版本/`；恢复仍能读 toml；同名文件 / 前缀目录不误排（可调既有 helper 断言，不改 D4.3 实现）。
3. `retired_anchor`：相关目标 `ok: False`；无关目标 `ok: True` + warning；不在 `BLOCKING_ISSUE_CATEGORIES`。D1.1 / `delete_asset` 对相关目标拒绝。
4. `retire_to_backup` / `retire_to_trash` 在 D1.1 各一条，互斥记录。
5. D1.1 四种 `change_kind` 成功路径 + defaults / 借用断言；另加多块精确清理（D5.4）一条。
6. D1.1 准入：`normalize_required`、`change_kind_mismatch`、`created_incomplete`（候选可发现、旧不动）、MSC-001 收敛。
7. D1.1 失败：`config_rewrite_failed`、`change_inconsistent`、`retire_failed`、`retire_failed_with_config_conflict`。
8. D4.4 成功交换（同语义）+ 对账。
9. D4.4 分阶段失败表 + 语义不匹配。
10. 6a 最小字段副本（无 `content_hash`）D4.4 返回 `retired_metadata_invalid`，零搬动；有 hash 但内容被改 → `content_hash_mismatch`。
11. 崩溃主要阶段：D1.1「已提升未清 defaults」、D4.4「已提升未 apply」——断言续跑或 `recovery_required` 材料，而非静默 clean。

验证（实现候选）：

```powershell
uv run ruff check src scripts
uv run mypy
uv run python -m pytest -q
```

最新结果：ruff 通过；mypy 通过；全量 pytest 1051 passed, 1 skipped，覆盖率 93.05%。人工验证不适用：验收点均可服务级断言。

## 13. 已决（本子任务冻结，不再问）

1. D1.1 入口名 `change_asset_semantics`；本入口 `old_asset` 收路径并冷扫。6a `update_asset` 的公开签名以 6a 规格 §7（`FirmwareAsset`）为准，待 6a 审查冻结后再对接其 backup 分支；6b 不把实现里的路径形参当契约（6B-001）。
2. 同时改模块键与范围时调用方必须传 `change_type`；范围三种要求 `module_key` 不变。
3. D4.4 只做同语义 R8 交换；改类型后的副本不能经 D4.4 回到旧类型。
4. D1.1 不完整 replacement 按 D1.5/D7.5 落入待补齐区（`created_incomplete`）。toml 必须有 `vendor`、`intended_firmware_type`、`import_state` 三键；无法判定类型时 `intended_firmware_type=""` 仍落键。`vendor` 入参优先，否则继承旧程序。不退位、不改引用/defaults；不保存本次操作意图。
5. D5.4：多块下 D1.1 清所有精确 `block_index` 命中，不以多块本身拒绝。
6. `reindex_failed` 同 6a：`ok: True` + clean。
7. 元数据文件名 `退位信息.toml`。`original_path` 只作身份别名；**缺 `content_hash` 一律不可恢复**（6B-002），不按当前内容补算。
8. 6a 审查冻结后，其 `update_asset` 的 backup 分支改为调用本轮 `retire_asset_to_backup`。这是 D4.2 不变量；不在 6a 审查完成前改写 6a 规格正文。
9. 崩溃续跑按第 9 节前进；运行时步骤 6 失败才 CAS 撤 defaults。
10. `_default_program_dir` 的 `dir_keywords` 回退为本轮必做；`canonical_module_dir` 别名表不扩 `主板`。canonical 未命中才回退；catalog 不可用或匹配不唯一则合成 `通用/<module_key>/…`。

## 14. 待决与风险（不阻塞本草案审查）

1. **`retire_failed` 置 `recovery_required` 会挡住后续删除/再更新**（D8.4）。与 6a `update_asset` 同构，但 D1.1 步骤 5 还保留新程序，现场更挤。若产品希望「两份并存但仍可删除」，需把 `retire_failed` / `config_rewrite_failed` 改为 `commit()` clean——那是产品决策，本草案未改。
2. **6a `update_asset` 签名未冻结**（规格 `FirmwareAsset` vs 实现路径）。6b 实现对接 backup 分支时以当时已冻结的 6a 公开签名为准；本草案不再预选。
3. **6a 最小字段 / 非原子副本**：缺 `content_hash` 或缺 toml 的目录不能经 D4.4 恢复。用户替代路径是删除副本或再导入，不在本轮做「补写 hash」迁移。
4. **步骤 5 保留新程序 + `recovery_required`**：用户不能经应用删除多余程序，只能按恢复材料手工处理后确认修复。与 D1.4a「apply 失败则 D1.4c 清 replacement」不对称，但是父规格 D1.1 表的字面要求。
5. **关键词回退的「唯一匹配」**：`_match_catalog_type` 按 catalog 顺序返回第一条类型；本轮「唯一」指通用区**子目录**中满足「该类型 label canonical == module_key」的目录个数为 1。若实现先写成「返回第一个子目录」，验收的非唯一用例会红。
6. **6a `build_clear_defaults_plan` 若对多块返回 `platform_not_normalized`**：与本草案 D5.4 冲突。D1.1 不得外包拒绝；实现时以父规格 D5.4 与本草案为准，必要时在 6a 冻结后要求其 builder 对齐。
)
