# REVIEW-20260919-default-vendor-shared-edit：设为默认 / 仅改厂商 / 借用编辑

状态：**实现审查通过**（r1 FAIL；r2 闭合 WES-001～009）

范围：子任务 5a（D1.6 / D1.7 / D1.8 / D10.1a）。实现 `src/fwasset/core/services/write_edit_service.py`，测试 `src/fwasset/tests/test_write_edit_service.py`。UI 接入归子任务 8。

验证：`ruff` / `mypy` 通过；`pytest -q` 1096 passed, 1 skipped，覆盖率 92.74%。

## 闭合项

| ID | 结论 |
| --- | --- |
| WES-000 | 已补规格 r2 |
| WES-001 | 异值 `canonical_conflict`、同值 `canonical_duplicate`，复用 `_check_canonical_conflicts` |
| WES-002 | 覆盖 token 绑定锁外 preview generation + fingerprint |
| WES-003 | 目标不存在 `target_model_missing`，无合法 id `invalid_target_model` |
| WES-004 | `rescan_failed` / `index_update_failed` 分流，不回滚已写 vendor |
| WES-005 | 父规格点名必测与关键失败分支已补 |
| WES-006 | `clear_shared_module` / `undo_clear_shared_module` 纳入本子任务 |
| WES-007 | 锁内重验来源资产、型号 id、self-reference 与其他型号 |
| WES-008 | 锁内反查阻断 issue → `reference_incomplete` |
| WES-009 | 登记/解除借用从同一份 preimage 字节解析并 CAS |
