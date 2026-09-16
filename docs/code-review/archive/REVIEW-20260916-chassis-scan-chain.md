# REVIEW-20260916：机芯类型字段与扫描诊断分级

候选：r2（复审 r1 → r2）
结论：**通过**。

## 首轮问题关闭

### CSC-001（P1，已关闭）
- 证据/影响：原 `owner_model_root_for` 选择最浅祖先，可能错读 chassis 配置并影响引用反查。
- r2：`reference_lookup.py:889-902` 改为最小 `rel.parts`；`_owner_root_for` 仍只委托公共 helper。
- 回归：覆盖嵌套根、两种迭代顺序、外层兄弟路径和无归属；受影响测试 57 passed。

### CSC-002（P1，已关闭）
- 证据/影响：缓存扫描三个 payload 缺少 `warnings`，服务契约不稳定。
- r2：`scan_service.py:85-117` 三个分支均补 `warnings: []`；即时扫描分级与 message 未改变。
- 回归：三个缓存分支均断言空 warnings。

## 剩余风险

- 嵌套型号根不属于当前 `enumerate_model_roots` 可生成的真实布局；修复用于契约对齐。
- 仍需按 TASK 在独立临时目录完成人工场景验证；CHANGELOG 不适用。
