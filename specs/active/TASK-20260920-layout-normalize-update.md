# TASK-20260920 子任务 6a：legacy 布局归一（D0.3）+ 普通 update 事务（D1.4a）+ build_clear_defaults_plan（D1.3）

- 状态：已实现，等待审查（规格 r1 + 实现候选；全量 Python 检查通过）
- 日期：2026-09-20
- 分支/HEAD：main @ cde649f
- 父规格：specs/active/TASK-20260903-crud-write-semantics.md
- 关联：TASK-20260919-default-vendor-shared-edit.md（5a，已收口模式参照，其文件本轮不得修改）

## 1. 背景与目标

父规格子任务 6 含五项能力，本轮取 6a 三项：D0.3 legacy 模块叶子归一、D1.4a 普通 update 事务、D1.3 `build_clear_defaults_plan`。

取这三项的依据是依赖方向：D1.1 改类型/改范围要消费 D1.3 签发的冻结计划，D4 恢复交换要消费 update 产生的 `旧版本/` 副本，二者均在 6b。D0.3 是 D1.4 的显式前置（未归一的模块叶子不允许直接 update），故与 D1.4a 同轮交付才能让 update 路径完整可测。

三个入口都是 D8.3 协调器清单内的复合写，共用持久化操作日志、阶段标记与启动恢复。

## 2. 范围与非目标

本轮交付三个公共入口及其测试。落点：D0.3 与 D1.4a 入新建的 `core/services/layout_update_service.py`；D1.3 `build_clear_defaults_plan` 入既有 `core/services/reference_service.py`（复用 `RewritePlan` / `_collect_defaults_hits` / `_plan_token`，理由见第 8 节）。

非目标：D1.1 改类型/改范围、D4 `旧版本/` 与恢复交换、D5.5 存量 platform 归一、全部 UI 接入（归子任务 8）。旧 UI 入口不得直接写 TOML。

D1.3 的计划入口本轮由 D1.4a 之外的调用方零消费——`build_clear_defaults_plan` 在本轮只被测试直接调用并交付为公共 API，6b 的 D1.1 步骤 2「构建并冻结 defaults 清理计划」直接复用同一函数与同一 `ClearDefaultsPlan` 结构，不得在 6b 另起一套。为此本轮必须把 `change_kind` 四种取值的行为全部定死（含本轮 update 不会用到的三种），否则 6b 会改签名。

## 3. 工程尺度

按父规格第 19 行：不做受签发入口防伪造（普通函数 + 配置根校验）、不做逐行崩溃注入测试（只测主要阶段）、不做细粒度 TOCTOU 重验（保留写锁即可）。不加码。

## 4. 既有事实与术语

- 事务：`WorkspaceTransaction(ws, operation=...)`，方法 `set_phase(phase, details=...)`、`record_product(path, manifest)`、`begin_product_write()`、`commit()`；状态 `WorkspaceState = "clean" | "operation_in_progress" | "recovery_required"`。
- 日志：`OperationLog{operation, phase, started_at, products, details}`，`OperationProduct{path, manifest}`。
- 改写：`build_rewrite_plan(configured_root, workspace_root, request: RewriteRequest, log_fn)`、`apply_rewrite_plan(plan, configured_root, log_fn)`。
- 反查与 gate：`check_reference_gate`、`find_references_to`。
- 其他：`validate_new_path`（D3 准入）、`reconcile_subtree`、`allocate_staging_area`、`register_retire`。
- 「模块叶子」= 模块目录直接含固件文件且构成唯一程序；「模块容器」= 模块目录下只有变体子目录。

## 5. 锁外/锁内边界总则

用户选择一律发生在锁外，锁内只做重验（父规格 D1.1 / D1.4a / D8）。三个入口统一：

1. 锁外：gate、冷读预览、影响展示、用户输入程序名或退位方式。等待用户确认期间**不得持锁**。
2. 锁内：重验 generation、路径、引用与目标状态；重验不通过即返回，不改盘。
3. 首次可能改动产品数据前调用 `begin_product_write()`。
4. **锁内尚未发生产品写入的拒绝分支必须 `commit()`**，以 clean 偶数 generation 收束（子任务 4 定稿、5/5a 沿用）。
5. 已落盘半成品的失败路径**不 `commit()` 成 clean**，保持 `recovery_required`，返回恢复材料。
6. 所有入口透传统一 gate 的 `workspace_not_configured` / `workspace_mismatch`、路径守卫的 `out_of_workspace`、事务的 `workspace_busy` / `recovery_required`。

## 6. D0.3 legacy 布局归一

```python
def normalize_module_leaf(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    module_path: str | Path,
    asset_name: str,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult
```

`asset_name` **必须由调用方（最终是用户）传入**，经 D3 名称校验。实现**不得**从版本号、固件文件名或类型名派生或兜底猜测——legacy 模块叶子的目录名通常就是类型名，没有独立程序名可派生。缺失或非法名一律 `invalid_asset_name`，不猜。

准入：`module_path` 必须是当前工作区内的 legacy 模块叶子。不是模块叶子（已是容器、或不含固件文件）→ `not_module_leaf`。叶子内不构成唯一完整合法资产 → `not_single_asset`。

归一属**身份延续**，不是新身份占用：目标路径命中的历史锚点是预期结果，走 D3 窄化例外；仅当发现 plan 之外的重叠锚点才返回 `path_identity_conflict`，**不得**泛化为用户可选的强制覆盖。

状态机（不能把目录 `M` 直接移进自身后代 `M/V`，必须借道临时目录），全程持写锁并写操作日志：

1. 构建 asset rename `M → M/V` 的 R8 plan；
2. `M` → staging/temp；
3. 创建新的模块容器 `M`；
4. staging/temp → `M/V`；
5. `apply_rewrite_plan`（同步 `static`、`follow_asset` 与 `defaults`）；
6. `reconcile_subtree`（`M` 的父目录）。

`begin_product_write()` 在步骤 2 之前调用。每步进入前 `set_phase`。步骤 4 完成后 `record_product(M/V, manifest)`。

失败补偿：

| 失败点 | 处理 | 返回 |
| --- | --- | --- |
| 步骤 1（build） | 原状不变，未写盘 | 透传 build 错误码 |
| 步骤 2（`M` → temp） | 原状不变 | `normalize_failed` |
| 步骤 3（创建新 `M`） | temp → `M` | `normalize_failed` |
| 步骤 4（temp → `M/V`） | **仅当新 `M` 仍为空**时 `rmdir M`，再 temp → `M` | `normalize_failed` |
| 步骤 5 apply 完整回滚成功 | 反向恢复目录 | `normalize_failed` |
| 步骤 5 `rollback_conflict` | **停止自动移动** | `layout_inconsistent` |
| 任一恢复动作失败 | 停止 | `layout_inconsistent` |
| 步骤 6 对账失败 | 磁盘为准，不回滚 | `reindex_failed`，提示重扫 |

步骤 4 的「仅当新 `M` 仍为空」是硬条件：非空说明有外部写入，**禁止递归清理未知内容**，转 `layout_inconsistent`。

`layout_inconsistent` 分支保持 `recovery_required`，不 commit 成 clean。归一不可撤销（D10.2），操作前需用户确认。

## 7. D1.4a 普通 update 事务

```python
def update_asset(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_asset: FirmwareAsset,
    source: str | Path,
    *,
    retire_mode: Literal["retire_to_trash", "retire_to_backup"],
    log_fn: Callable[[str], None] = print,
) -> ServiceResult
```

锁外：来源选择、影响预览、退位方式选择（D1.4b）。**用户确认后才取写锁。**

落点矩阵（D1.4）：模块叶子唯一程序 → **先按 D0.3 归一**，本入口遇未归一叶子直接返回 `normalize_required`，不隐式归一；模块下变体之一 → staging 提升为同模块下的**兄弟变体目录**。不得原位覆盖，不得把旧资产变成 replacement 的父容器。replacement 与任一 static 锚点不得父子重叠（R8 已强制）。

锁内：

1. 重验用户选择、generation、路径、引用与目标状态；
2. 创建操作日志，随后 staging 导入并**验证完整性**（D1.5：必须是完整合法资产）；
3. 提升 replacement 到最终路径，`record_product` 记录提升时的 manifest hash；
4. 此时 old 与 replacement **同为现存合法资产** → `build_rewrite_plan`（R8 update）；
5. `apply_rewrite_plan`；
6. 旧程序退位（按 `retire_mode` 分支）；
7. 对共同祖先 `reconcile_subtree`；清理日志。

`begin_product_write()` 在步骤 2 首次 staging 写入之前调用。步骤 1 的重验失败分支尚未写盘，必须 `commit()` 收成 clean。

失败契约：

| 失败点 | 处理 | 返回 |
| --- | --- | --- |
| build 失败 | 按 D1.4c 清理本次 replacement | `plan_build_failed` |
| apply **完整回滚成功** | 按 D1.4c 清理本次 replacement，恢复原状 | `config_rewrite_failed` |
| apply **rollback conflict** | **保留现场**，不自动删除 replacement | `update_inconsistent` |
| apply 成功、步骤 6 退位失败 | 保留两份；引用与 defaults 已指向新程序 | `retire_failed` |
| 步骤 7 对账失败 | 磁盘为准，不回滚 | `reindex_failed`，提示重扫 |

**禁止出现「旧已退位但新程序不可用」的结果。**

崩溃恢复：

| 崩溃点 | 恢复 |
| --- | --- |
| replacement 已提升、尚未 build | 按 D1.4c 清理 replacement，回到原状 |
| apply 成功、旧程序尚未退位 | 继续退位 |
| 旧程序已退位、尚未对账 | 只做 `reconcile_subtree` |
| `retire_to_trash` 已完成、尚未清理日志 | 清理日志即可 |

检测到第三方修改 → 停止并返回恢复材料，不自动续跑。

### D1.4b 退位两分支（不得合并为一种记录）

| 分支 | 落点 | 生命周期 |
| --- | --- | --- |
| `retire_to_trash` | `register_retire` 写 `transactional_retire` 隔离记录 | 提交成功后异步送系统回收站，**不展示撤销** |
| `retire_to_backup` | `<replacement>/旧版本/<旧名>-<时间戳>/`（目录名用 `managed_paths.RETIRED_VERSIONS_DIRNAME`，不写字面量），写副本元数据（D4.2） | **不进隔离区、不送回收站**，用户长期保留 |

两者落点、生命周期与恢复完全不同，实现必须是两条独立分支与两种记录。`retire_to_backup` 写入的副本元数据形状本轮只写最小字段（原路径、退位时间戳、来源 update 操作 id），D4 恢复交换在 6b 消费。

### D1.4c 「删除本次产物」统一原语

清理前必须**同时**满足三条：① 路径仍是本操作创建的身份；② 当前 manifest hash 等于提升时 `record_product` 的值；③ 操作日志状态允许清理。

任一不满足 → **保留现场**，返回 `update_inconsistent` 及恢复材料，**禁止递归删除**。理由：应用锁挡不住资源管理器或其他程序往 replacement 里加文件。

该原语由 D0.3、D1.4a 共用，6b 的 D1.1、D2.1 继续复用；复用的是身份、manifest 与日志状态校验。

## 8. D1.3 build_clear_defaults_plan

```python
def build_clear_defaults_plan(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_path: str | Path,
    change_kind: Literal["change_type", "general_to_custom", "custom_to_general", "custom_scheme_move"],
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult
```

R8 现有 `build_rewrite_plan` 遇语义变化即 `unsupported_semantic_change`，且 plan 不可手工构造，故需本入口。

**builder 内部冷读、重新反查并派生完整命中集，再签发 plan——不接受调用方传入的 `hits`。** 传入不完整列表会漏清 defaults，传入过期或伪造 hit 会清错块。这条是硬约束，实现签名里不得出现 hits 参数。

清除范围限定为反查命中的 `block_index` 对应条目。按变化种类：

| change_kind | defaults 处理 |
| --- | --- |
| `change_type` | 清除旧 canonical 键的命中 defaults |
| `general_to_custom` | **同样清除**精确命中的 defaults（否则复合操作结束后留下已知悬空 default） |
| `custom_to_general` | **不自动设为默认**，空计划，由用户显式「设为默认」 |
| `custom_scheme_move` | 无平台 default 改写（defaults 只指向通用区），空计划 |

**落点为 `core/services/reference_service.py`，不另起模块。** 计划复用既有 `RewritePlan` 结构、`_collect_defaults_hits`（`reference_service.py:232`）派生命中集，并以 `_plan_token`（`:116`）签发 token——`apply_rewrite_plan` 在 `:778` 会校验 `plan.token != _plan_token(plan)`，另造一套结构产出的计划会被直接拒绝。不得复制一份平行的 token/preimage 机制（5a 的 WES-001 即「另起一套字符串比较而非复用既有判定」，本轮不重犯）。

计划持有 TOML preimage 与新内容，经 `apply_rewrite_plan` CAS 落盘、可回滚。不猜测替代者。本入口**纯函数、不改盘、不持锁**，签发后由调用方在锁内 apply。UI 必须先展示影响（子任务 8）。

与「删除零改写」的关系：删除程序不动 defaults（R8 规则 5 不变，D1.2 借用引用沿用删除零改写）；改类型是另一种操作，明确要求 default 失效。实现不得混为一谈。

builder 不信任调用方状态：即便 D1.1 已在其步骤 1 校验过，本入口仍自行冷扫确认 `old_path` 为现存完整合法资产（否则 `invalid_target`）。反查返回阻断级 issues 时命中集不完整，直接返回 `reference_incomplete`，**不得**以不完整命中集签发计划（漏清即留悬空 default）。

错误：严格读取失败 → `config_parse_error`；平台结构不合法 → `platform_not_normalized`；`old_path` 不是现存合法资产 → `invalid_target`；反查不完整 → `reference_incomplete`。

## 9. 崩溃恢复与半成品

三个入口全部接入 D8.3 同一协调器（持久化操作日志 + 阶段标记 + 启动恢复），不得只给其中一个写恢复逻辑。

D8.1 seqlock：第一次产品数据变更前递增为奇数，成功/失败/inconsistent 收尾时再次递增为偶数。**任何已改变磁盘或 TOML 的操作，即使最终返回 inconsistent，也必须完成递增**。

D8.4：`*_inconsistent` 现场（`layout_inconsistent` / `update_inconsistent`）收尾同样推进为偶数以结束 seqlock，但 `WorkspaceState` 必须留在 `recovery_required`，**所有新写操作一律阻止**；预览只读放行但带警告。不得把不一致现场当正常状态放行后续写入。

两条不变量：① 产品数据变更前必须先进入非 `clean` 状态；② 不一致现场不得重新开放写入。

## 10. 错误码全集

| 入口 | 码 | 含义 |
| --- | --- | --- |
| D0.3 | `invalid_asset_name` | 程序名缺失或未过 D3 校验（不猜测） |
| D0.3 | `not_module_leaf` | 目标不是 legacy 模块叶子 |
| D0.3 | `not_single_asset` | 叶子内不构成唯一完整合法资产 |
| D0.3 | `path_identity_conflict` | 发现 plan 之外的重叠历史锚点 |
| D0.3 | `normalize_failed` | 状态机失败且补偿成功，已回到原状 |
| D0.3 | `layout_inconsistent` | 补偿失败或 rollback conflict，保留现场 |
| D1.4a | `normalize_required` | 旧资产仍是未归一模块叶子，须先走 D0.3 |
| D1.4a | `incomplete_replacement` | staging 内容不是完整合法资产（D1.5） |
| D1.4a | `plan_build_failed` | 步骤 4 build 失败，已清理 replacement |
| D1.4a | `config_rewrite_failed` | apply 失败且完整回滚成功，已清理 replacement |
| D1.4a | `update_inconsistent` | rollback conflict 或 D1.4c 校验不通过，保留现场 |
| D1.4a | `retire_failed` | apply 成功但退位失败，两份并存 |
| D1.3 | `config_parse_error` | 平台配置严格读取失败 |
| D1.3 | `platform_not_normalized` | 平台结构不合法 |
| D1.3 | `invalid_target` | `old_path` 不是现存合法资产 |
| D1.3 | `reference_incomplete` | 反查返回阻断级 issues，命中集不完整 |
| 共用 | `stale_plan` | 锁内重验发现 generation 或 preimage 变化 |
| 共用 | `reindex_failed` | 索引对账失败，磁盘为准，提示重扫 |

用户消息一律中文，不以裸异常代替服务错误码。

## 11. 验收清单（含父规格点名必测断言）

- [x] D0.3 程序名由调用方传入；缺失/非法返回 `invalid_asset_name`，实现内无任何从版本号、固件文件名或类型名派生的兜底路径。
- [x] D0.3 步骤 4 失败且新 `M` 非空时不 `rmdir`、不递归删除，返回 `layout_inconsistent` 且状态为 `recovery_required`。
- [x] D0.3 成功后模块目录成为容器，`static` / `follow_asset` / `defaults` 三类引用同步，`reconcile_subtree` 后索引无幽灵行。
- [x] **未归一的模块叶子不允许直接 update**：`update_asset` 返回 `normalize_required`，零写盘、generation 不变。
- [x] D1.4a apply rollback conflict 保留现场返回 `update_inconsistent`，**不自动删除 replacement**。
- [x] D1.4a apply 成功、退位失败返回 `retire_failed`，两份并存，引用与 defaults 已指向新程序；不出现「旧已退位但新程序不可用」。
- [x] D1.4b 两分支各自可测：`retire_to_trash` 产生 `transactional_retire` 隔离记录且不展示撤销；`retire_to_backup` 落 `旧版本/<旧名>-<时间戳>/` 且不进隔离区。
- [x] D1.4c manifest 与提升时记录不一致（模拟外部写入 replacement）时保留现场返回 `update_inconsistent`，目标目录内容未被删除。
- [x] D1.3 签名内无 hits 参数；builder 内部反查派生命中集；`custom_to_general` 与 `custom_scheme_move` 返回空计划。
- [x] D1.3 产出的计划能直接通过 `apply_rewrite_plan` 的 `_plan_token` 校验（断言非 `invalid_plan`），证明复用了既有结构而非平行实现。
- [x] D1.3 `change_type` 与 `general_to_custom` 均清除精确命中的 `block_index` 条目，不误清同模块其他块。
- [x] 锁内未写入的拒绝分支全部以 `load_workspace_status(ws).state == "clean"` 且 generation 为偶数收束（MSC-001 同构回归）。
- [x] 已落盘半成品的 inconsistent 分支状态为 `recovery_required`，后续写操作被阻止、预览带警告放行。
- [x] 每个新增行为先有失败测试并观察到预期 RED，再写最小实现；全量 Python 检查通过。

## 12. TDD 计划（每条行为先 RED 后最小实现）

按此顺序逐条落地，每条先写失败测试并实际观察 RED：

1. D1.3 四种 `change_kind`（含两种空计划）与三个错误码 → 纯函数最易先绿，且 D1.4a/6b 都依赖其形状。
2. D0.3 准入三码（`invalid_asset_name` / `not_module_leaf` / `not_single_asset`）。
3. D0.3 成功路径六步 + 引用同步 + 对账。
4. D0.3 步骤 4 非空保护与 `layout_inconsistent` 状态断言。
5. D1.4a `normalize_required` 零写盘断言。
6. D1.4a 成功路径（两个 `retire_mode` 各一条）。
7. D1.4c manifest 不符保留现场。
8. D1.4a apply 失败三分支（`config_rewrite_failed` / `update_inconsistent` / `retire_failed`）。
9. 收敛判据回归：每个锁内拒绝分支断言 `state == "clean"` 且 generation 偶数。

## 13. 验证命令

先取 RED，逐项实现后取 GREEN：

```powershell
uv run python -m pytest src/fwasset/tests/test_layout_update_service.py src/fwasset/tests/test_reference_service.py -q --no-cov
```

（D1.3 的新增用例落在既有 `test_reference_service.py`，与其实现落点一致。）

候选稳定后跑全量：

```powershell
uv run ruff check src scripts
uv run mypy
uv run python -m pytest -q
```

人工验证：不适用；本轮验收点均由服务级自动化断言覆盖（AGENTS.md「能被测试完全断言、无需用户操作界面的改动，自动化通过即可直接提交」）。

## 14. 已决问题

1. `retire_to_backup` 的副本元数据字段全集由 D4.2 定义，本轮只写最小字段（`原路径`、`退位时间戳`，落在副本内 `退位信息.toml`）。**按倾向决定：接受 6b 的一次性迁移**（自用场景、副本量小）。
2. D0.3 归一与 D1.4a update 在 UI 上是两次确认还是一次连续流程，归子任务 8；本轮服务层按两个独立入口交付，不提供合并入口。
3. `reindex_failed`：**按倾向决定——不置 `recovery_required`**。磁盘是真源、SQLite 只是搜索缓存，重扫即可恢复，阻写代价过高。实现上该分支返回 **`ok: True` + `code="reindex_failed"`**：产品操作确已成功，只是缓存过期，UI 据此提示「请重新扫描程序列表」。与既有 `model_scheme_service.rename_*`（对账失败只记日志后照常成功提交）同构。

## 15. 实现记录

落点：`core/services/layout_update_service.py`（D0.3 + D1.4a，新建）、`core/services/reference_service.py`（D1.3 `build_clear_defaults_plan`，复用 `RewritePlan` / `_plan_token`）；测试 `tests/test_layout_update_service.py`（17 例）与 `tests/test_reference_service.py`（新增 9 例）。

类型新增：`types.py` 的 `ReferenceOperation` 增加 `"clear_defaults"`，新增 `ClearDefaultsKind`。

**与第 7 节签名的已知偏差（保留待审查裁定，未回改规格）**：第 7 节写的是 `old_asset: FirmwareAsset`，实现收的是 `old_asset: str | Path`。理由是服务内部无论如何都要冷扫重读（5a 定下的「不信任调用方字段」），传入完整资产字典纯属多余，调用方反而要先扫一遍才能构造它。此处刻意不把规格改成与实现一致——签名属审查面，抹平偏差等于消掉审查线索。由审查者裁定改规格还是改实现。

两处**复用既有实现而非另起一套**（沿用 5a 的 WES-001 教训）：

- 语义快照复用 `reference_service._derive_semantics`——`build_rewrite_plan` 会把请求快照与它内部派生的结果逐字段比对，手工从 `FirmwareAsset` 拼字段必然对不上（模块键要 canonical，`source_group` 取型号 id）。
- D1.3 计划复用 `RewritePlan` + `_plan_token`，`apply_rewrite_plan` 的 token 校验直接放行。

`config_parse_error` 与 `reference_incomplete` 的分流：`_scan_workspace` 把平台配置解析失败报成阻断级 `platform_parse_error`，本入口单独摘出给 `config_parse_error`——否则用户只看到「反查不完整」，不知道该修哪个文件。

### 实现中发现、未在本轮修复的既有缺陷

`reference_lookup._default_program_dir` 只按 `canonical_module_dir(child.name) == module_key` 匹配模块目录，**不走 catalog 的 `dir_keywords`**。因此当模块目录用的是关键词名（如 `主板`，catalog 中 `主板程序` 的 `dir_keywords` 之一）而非 canonical 名（`主板程序`）时，指向它的 `defaults` 条目**收集不到 hit**——rename / update / 归一都不会同步这条 default，留下悬空默认值。

真实工作区按 `dir_keywords` 命名目录是合法且常见的，所以这条影响面真实存在。本轮未修：它属 `find_references_to` 的公共反查语义，改动会同时影响 R8 的 rename、delete 预检与级联改写全部路径，超出 6a 范围。本轮测试统一用 canonical 模块目录名规避。**建议列入 6b 或单独子任务**，修复时需同步补 rename / delete 侧的回归。
