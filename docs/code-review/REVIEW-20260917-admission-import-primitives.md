# REVIEW-20260917-admission-import-primitives：新路径准入与导入原语审查

任务：`specs/active/TASK-20260917-admission-import-primitives.md`（父规格实现子任务 3：D3 / D0.2 / D7.1–D7.4）

说明：flash-coder 子代理三次派单（整单/拆单）均空返回无产物，通道探针确认会话与文件操作正常但模型承载不了从零建模块的多步任务，实现由统筹在主会话完成。本 Review 为统筹自查 + 全量测试证据；独立复审并入子任务 5（首个消费方）的审查轮。

## 审查范围

- `core/admission.py`（新）：D3 六项检查序列、D0.2 领域归属、错误码与 payload
- `core/import_io.py`（新）：来源防呆、zip 成形（仅剥一层）、提升编排（准入复验 → 容器创建 → 原子提升）
- `core/types.py`：`AdmissionKind`、`StagedImport`
- `tests/test_admission.py`（34 用例）、`tests/test_import_io.py`（22 用例）
- 父规格 D7.3 / D7.3a / D7.4 按用户裁决同步简化（来源可信、zip-only、无逐条目校验与限额）

## 证据（候选 r2）

- `uv run ruff check src scripts` → 通过；`uv run mypy` → 通过（47 文件）
- `uv run python -m pytest -q` → 826 passed, 1 skipped，覆盖率 94.78%（门槛 80%；admission 97% / import_io 92%）
- 场景脚本 `C:\Users\fanzehao\AppData\Local\Temp\opencode\scenario_admission_import.py` → 24/24 通过（准入 11 项、导入 13 项，含失败清理、会话保留与复用、junction 占用）

## 发现

| ID | 严重度 | 位置 | 触发条件 | 影响 | 结论 |
| --- | --- | --- | --- | --- | --- |
| ISS-20260917-A1 | 低 | `admission._validate_asset_domain` | 提升目标的父链某段已存在但为文件（如 `通用/主板程序` 是文件） | 准入层放行，到 `promote_staging` 才以 StagingError 拒绝；行为正确，报错层级较深 | 不阻断；子任务 5 服务层统一映射错误码 |
| ISS-20260917-A2 | 低 | `admission._validate_scheme_domain` | 「定制」段的大小写/变形体目录名 | 精确比较与 scanner / reference_lookup 现有语义一致；变形体由文件名校验或落盘层拒绝 | 不阻断；保持与现有语义一致 |
| ISS-20260917-A3 | 信息 | `import_io.stage_import_archive` | zip 条目名无 UTF-8 旗标（历史工具生成） | 按 zipfile 默认 cp437 解码，不做编码猜测修复（规格定稿）；乱码名落盘失败时报 `archive_extract_failed` | 记录边界；出现真实案例再立项 |

## 结论

- 验收清单代码相关项全部满足；检查顺序、错误码、成形规则与 Task 规格一致，无强制覆盖旁路。
- 复用既有原语（`assert_within_workspace` / `managed_path_reason` / `find_dangling_anchors` / `staging_io` / `manifest_hash`），未复制路径规则；`file_scan._is_excluded_dir` 私有跨模块引用沿用 reference_lookup 先例。
- 无阻断问题。
