# TASK-20260917-admission-import-primitives：新路径准入与导入原语

状态：已完成

父规格：`specs/active/TASK-20260903-crud-write-semantics.md` 实现子任务 3（D3、D0.2、D7.1–D7.4）。规则以父规格为准，本文只写实现裁定；服务层包装与 UI 编排归子任务 5/8。

## 目标与规则

新增两个 core 原语模块。

### 1. `core/admission.py` — 新路径准入（D3）+ 目录层级规范（D0.2）

`validate_new_path(target, *, kind, configured_root, workspace_root) -> None`，异常式 `AdmissionError(code, message, payload)`；锁外预览与锁内复验（D3.7）共用同一入口。`AdmissionKind = Literal["model", "scheme", "asset"]` 定义进 `core/types.py`。

检查按序执行，首个失败即抛：

| # | 检查 | 错误码 |
| --- | --- | --- |
| 1 | `assert_within_workspace` | `out_of_workspace` |
| 2 | 目标不存在：父目录 normcase 枚举 + resolved 身份双重比较（junction 等价命中，与 `same_path_identity` 同语义） | `path_exists` |
| 3 | 领域归属（见下表） | `domain_violation` / `migration_required` / `layout_invalid` |
| 4 | Windows 文件名，对**每个新建段**：非法字符 `<>:"/\|?*`、控制字符、保留名 CON/PRN/AUX/NUL/COM1-9/LPT1-9（含带扩展名形态、大小写不敏感）、尾随点/空格、空段 | `invalid_name` |
| 5 | 与 scanner 完全相同的排除判定（复用 `file_scan._is_excluded_dir`，传 workspace_root，私有跨模块引用有先例）作用于**最终完整绝对路径**；configured root 自身命中关键词 | `path_excluded` / `workspace_excluded` |
| 6 | `find_dangling_anchors(configured_root, workspace_root, target)`：hits 非空或 issues 含阻断级（`is_blocking_issue`） | `path_identity_conflict` |

领域归属（D0.2）：

| kind | 规则 |
| --- | --- |
| model | parent == workspace_root；`detect_workspace_layout` ∈ {multi_model, empty}；single_model → `migration_required`，invalid → `layout_invalid` |
| scheme | parent 名为 `定制` 且 parent.parent 具型号标志（single_model 时即工作区根） |
| asset | 向上回溯具型号标志的根（与 `managed_paths._has_model_marker` 同规则）；相对该根恰为 `通用/<模块>/<程序名>`（3 段）或 `定制/<方案>/<模块>/<程序名>`（4 段）。模块叶子（2 段）与更深/更浅一律拒绝；定制区 `<方案>` 须已存在为目录；`<模块>` 段允许由导入提升时自动创建 |

- 不提供强制覆盖开关；原身份延续例外（备用副本恢复、删除撤销、D0.3 归一）由各调用方在自己 plan 范围内窄化，本模块不加旁路。
- asset 新建段 = `<程序名>` 与（新建时的）`<模块>` 段；model/scheme 新建段 = 末段。

### 2. `core/import_io.py` — 导入成形与边界防护（D7.1–D7.4）

`AssetImportError(code, message, payload)`。staging 会话经 `allocate_staging_area` 分配（须持锁 + active 事务，同 `staging_io` 要求）；成形后会话根 = 未来资产目录内容。

`stage_import_files(transaction, workspace_root, files) / stage_import_directory(..., source) / stage_import_archive(..., archive) -> StagedImport`（TypedDict 定义进 `core/types.py`：`session` 会话路径 + `skipped_metadata` 被忽略的来源元数据文件清单；只过滤会落到资产目录根层的 `程序信息.toml`）：

- **来源安全（D7.3a，三入口共用，最小防呆）**：
  - 来源与 staging 会话同一身份或互相包含 → `source_overlap`；
  - 来源位于受管区域（`managed_path_reason` 任一命中）→ `source_managed`；
  - 名为 `程序信息.toml` 的来源文件不复制、在 payload 单独报告（D4.3：导入不信任来源元数据）。
- **边界防护（D7.3，来源可信裁剪）**：压缩包与文件夹来源均为厂商提供的可信内容，不做逐条目恶意路径校验、重名预检与限额（zip-bomb 防护不适用），路径清洗交给标准库默认行为：
  - 压缩包**仅支持 `.zip`**（标准库 `zipfile`；其余后缀 → `archive_unsupported`，rar/7z/tar 等出现真实需求再扩展）；
  - 来源不存在或类型不符 → `source_unreadable`；解压/复制失败统一 → `archive_extract_failed`（含加密、损坏、非法条目名、重名落盘冲突），消息附简短原因。
- **成形（D7.2，最多剥一层，绝不递归扁平化）**：
  - files → 直接放会话根；
  - directory → 会话根替代来源目录层（内容平移、内部结构原样保留）；
  - archive → 解压进会话内临时子目录；仅当根下恰一个目录且无同级文件时剥掉该层（空单一顶层目录同规则）；临时子目录在校验后清出，提升前会话根只含资产内容。
- **重名（D7.4）**：`files` 入口做 normcase 重名检查 → `duplicate_name`（payload 冲突清单）；压缩包/文件夹内部重名不预检，由 Windows 落盘冲突自然报错（`archive_extract_failed`）。
- 文件夹/压缩包解包后无任何文件 → `empty_source`。
- 任一失败 → `cleanup_staging_area`，不留半成品。

`promote_import(transaction, workspace_root, configured_root, staged_session, target) -> Path`（持锁事务内）：

1. `validate_new_path(target, kind="asset", ...)` 全套复验（D3.7）；
2. `begin_product_write`；
3. 自动创建缺失模块容器段：先 `record_product`（空目录 manifest）后 `mkdir`（与提升原语「先记录后落盘」同序）；
4. `promote_staging` 提升会话到 target；
5. 返回最终路径。

## 边界与非目标

- 不做 catalog 完整性分流与候选区（D7.5 归子任务 5）：`stage_*` 不判定完整性，子任务 5 在 stage 与 promote 之间分流 incomplete 去向。
- 不实现型号/方案/程序操作服务与 UI（子任务 4/5/8）；介绍文件随内容原样复制，无特殊处理（D7.6）。
- 导入不代建方案（缺失由准入拒绝）；索引对账由调用方负责；不改 `file_scan` / `asset_index` / R8 语义与索引 schema。
- 压缩包仅支持 `.zip`，其余后缀（rar/7z/tar 等）→ `archive_unsupported`；出现真实需求再扩展格式支持。
- 错误消息中文；core 原语抛异常，ServiceResult 映射归服务层。

## 验收清单

- [x] admission 六项检查按序生效，错误码与 payload 可观察；无强制覆盖旁路
- [x] D0.2：asset 恰 3/4 段；模块叶子/2 段/更深拒绝；定制区方案缺失拒绝；single_model 拒新建型号（migration_required）但放行 asset/scheme；invalid 布局 → layout_invalid
- [x] 大小写等价已存在、junction 等价、保留名（含扩展名形态）、尾随点/空格、整路径排除关键词、configured root 命中排除 → 各自错误码
- [x] dangling anchors hits 与阻断级 issues → path_identity_conflict
- [x] 三种来源成形规则（含仅剥一层、空单一顶层目录、内部结构保留）
- [x] zip 解压与剥一层；损坏/加密/非法条目统一 archive_extract_failed；其余后缀 → archive_unsupported
- [x] 来源重叠/受管来源拒绝；程序信息.toml 不复制且报告；files 入口 normcase 重名 → duplicate_name
- [x] stage 失败清理 staging；promote 前复验准入；容器段自动创建并记录产物，`delete_recorded_product` 可清理
- [x] 现有测试全绿；ruff / mypy / pytest 全量通过（覆盖率 ≥80%）
- [x] Review 留档（core 公共 API）；人工验证结论

## 验证

- 自动化（候选 r2，2026-09-18）：`uv run ruff check src scripts` → 通过；`uv run mypy` → 通过（47 文件）；`uv run python -m pytest -q` → 826 passed, 1 skipped，覆盖率 94.78%（门槛 80%；admission 97% / import_io 92%）。
- 场景：`C:\Users\fanzehao\AppData\Local\Temp\opencode\scenario_admission_import.py`（临时目录模拟工作区，不触碰真实数据）→ 24/24 通过：准入 11 项（成功、模块叶子、深层、保留名、尾随点、排除关键词、大小写等价占用、悬空锚点、方案缺失、旧布局拒新建型号、scheme 成功），导入 13 项（files 元数据跳过与提升落盘、文件夹替代来源层、zip 剥包装、重名拒绝、失败无 staging 残留、空 zip、非 zip 后缀、来源含暂存区、zip 元数据跳过、占用目标拒绝、会话保留与复用）。
- 人工：2026-09-18 用户确认验证通过（无 UI 入口，以场景脚本 24/24 为验收）。
- 文档：Review 见 `docs/code-review/REVIEW-20260917-admission-import-primitives.md`（3 项发现均不阻断）；CHANGELOG 不适用（无用户可感知变化，UI 消费归子任务 5/8）；迁移说明不适用（不改路径、配置与索引 schema）。
