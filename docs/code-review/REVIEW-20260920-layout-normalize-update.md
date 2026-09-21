# REVIEW-20260920-layout-normalize-update：归一 / 普通更新 / defaults 清理计划

状态：**实现审查通过**（e3cbd17 初审 FAIL；后续未提交修复闭合 6A-IMP-001～011）

范围：子任务 6a（D0.3 / D1.4a / D1.3）。初审对象 `e3cbd17`；复审对象为叠在 `e104c03` 上的未提交修复。不含 5a、不改 6b 规格。

验证：`ruff` / `mypy` 通过；`pytest -q` 1083 passed, 1 skipped，覆盖率 92.52%。

## 闭合项

| ID | 结论 |
| --- | --- |
| 6A-IMP-001 | backup 走 `retire_asset_to_backup` 四字段元数据，覆盖 6a 最小字段 |
| 6A-IMP-002 | `resume_normalize_module_leaf` / `resume_update_asset`；staging 路径在 `os.replace` 前入日志 |
| 6A-IMP-003 | `update_asset` 接受 `FirmwareAsset`，只取其 `path` 再冷扫；规格第 7 节未改 |
| 6A-IMP-004 | 续跑按序列化计划判定 preimage / postimage / mixed，禁止把已 apply 当未 apply 回滚 |
| 6A-IMP-005 | apply 后记录 `old_manifest`；旧路径消失须有本次退位证据 |
| 6A-IMP-006 | 写入 staging 内容前持久化 session |
| 6A-IMP-007 | 退位证据绑定路径 + `retired_by` + hash，拒绝历史副本冒充 |
| 6A-IMP-008 | 传入 `session=` 须为本事务空 staging 会话 |
| 6A-IMP-009 | 正式 backup 已在时仍先校验 replacement manifest |
| 6A-IMP-010 | destination 存在不能单独当完成；temp 仍有内容则保留现场 |
| 6A-IMP-011 | 正式 backup 完成后旧路径再出现 → `retire_inconsistent` |

## 裁定

`update_asset` 公开参数保持规格第 7 节 `FirmwareAsset`；实现另接受 `str \| Path`。
