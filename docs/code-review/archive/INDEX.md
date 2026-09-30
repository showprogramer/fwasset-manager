# 代码审查归档索引

以下文件已从 `docs/code-review/` **根目录移出**，议题均已闭环或被后续审查吸收。  
需要细节时打开对应文件；**不要**再当作未完成任务列表。

| 文件 | 日期 | 主题 | 处置 |
|------|------|------|------|
| [REVIEW-20260929-asset-in-place.md](./REVIEW-20260929-asset-in-place.md) | 2026-09-29 | 沿用原文件编辑程序 | 两条意见已修复；对话框人工验收通过 |
| [REVIEW-20260927-handcontrol-usb-copy.md](./REVIEW-20260927-handcontrol-usb-copy.md) | 2026-09-27 | 手控复制到 U 盘 | 无阻断问题；用户验收通过。残留 HC-USB-001–004 保持接受 |
| [REVIEW-20260927-retired-version-ui.md](./REVIEW-20260927-retired-version-ui.md) | 2026-09-27 | 备用版本列表与恢复入口 | 阻断问题已修复；自动化及用户人工验收通过 |
| [REVIEW-20260803-default-module-key-canonicalization.md](./REVIEW-20260803-default-module-key-canonicalization.md) | 2026-08-03 | 默认模块键一致性 | 人工验证通过；快捷键短键与规范键归一、默认键合并已闭环 |
| [REVIEW-20260803-shared-source-contract.md](./REVIEW-20260803-shared-source-contract.md) | 2026-08-03 | 共享来源模块一致性与登记对话框 | 人工验证通过；同模块约束、候选收敛和对话框优化已闭环 |
| [REVIEW-20260724-b4-shared-scheme-boundary.md](./REVIEW-20260724-b4-shared-scheme-boundary.md) | 2026-07-24 | B4 共享 / 方案边界硬化 | 回源不跳共享；本地副本覆盖；缺失不回落本地；Qt 人验 4/6/7/8 通过 |
| [REVIEW-20260724-b3-shared-display.md](./REVIEW-20260724-b3-shared-display.md) | 2026-07-24 | B3 共享模块展示与烧录候选 | Qt 角标 / 可烧 / 缺失禁用 / 列表只显共享来源；2026-07-24 人验通过 |
| [REVIEW-20260718-shared-module-schema.md](./REVIEW-20260718-shared-module-schema.md) | 2026-07-18 | 共享引用 schema + 解析器 | B1 数据层验收通过并提交（`4c8b6db` + 修复 `fafd65a`） |
| [REVIEW-20260717-model-persistent-id.md](./REVIEW-20260717-model-persistent-id.md) | 2026-07-17 | 型号持久 id（Phase B0） | 人验通过并提交（`1c287b8`）；B1 解锁 |
| [REVIEW-20260716-platform-config-safety.md](./REVIEW-20260716-platform-config-safety.md) | 2026-07-16 | 平台配置严格读 + 原子写盘 | 人验通过并提交（`72d1cca`）；B0 解锁 |
| [REVIEW-20260728-config-usb-simplification.md](./REVIEW-20260728-config-usb-simplification.md) | 2026-07-28 | 配置简化 + USB 烧录流程对齐 | Issue 1–9 已关闭；冻结 exe、真实 U 盘与自动化验证通过 |
| [REVIEW-20260709-branch-pyside6.md](./REVIEW-20260709-branch-pyside6.md) | 2026-07-09 | PySide6 迁移分支总审 | Phase 4 完成；exe 冷启动人验通过 2026-07-27 |
| [REVIEW-20260724-qt-only-ui-cleanup.md](./REVIEW-20260724-qt-only-ui-cleanup.md) | 2026-07-24 | Qt-only 界面收敛 | Qt 三场景人验通过；CTk 已移除；自动化回归通过 |
| [REVIEW-20260720-shared-module-registration.md](./REVIEW-20260720-shared-module-registration.md) | 2026-07-24 | B2 共享手动登记入口 | Qt 3 场景人验通过；CTk 随 B3；已闭环 |
| [REVIEW-20260720-shared-module-registration-independent.md](./REVIEW-20260720-shared-module-registration-independent.md) | 2026-07-24 | B2 独立审查 | B2 议题闭环；迁移议题移交后置 TASK |
| [REVIEW-20260514-scan-index-race.md](./REVIEW-20260514-scan-index-race.md) | 2026-05-14 | 扫描/索引竞态 | 已闭环；扫描互锁见分支总审 Issue 3 |
| [REVIEW-20260516-scan-state-model.md](./REVIEW-20260516-scan-state-model.md) | 2026-05-16 | ScanStateModel 抽取 | 已修复并合入 |
| [REVIEW-20260518-agents-and-doc-naming.md](./REVIEW-20260518-agents-and-doc-naming.md) | 2026-05-18 | AGENTS / 文档命名 | 规范已落地 AGENTS.md |
| [REVIEW-20260518-ios-ui-refactor.md](./REVIEW-20260518-ios-ui-refactor.md) | 2026-05-18 | 早期 iOS 风 UI | 界面已多次重写，仅历史参考 |
| [REVIEW-20260518-sidebar-panel.md](./REVIEW-20260518-sidebar-panel.md) | 2026-05-18 | 旧侧边栏面板 | 组件已替换为工作台 |
| [REVIEW-20260606-l36-directory-standard.md](./REVIEW-20260606-l36-directory-standard.md) | 2026-06-06 | L36 目录标准长文 | 目录约定以 `docs/migrations/MIGRATION-20260606-*` + 现行扫描为准 |
| [REVIEW-20260608-l36-scanner.md](./REVIEW-20260608-l36-scanner.md) | 2026-06-08 | L36 扫描器重构 | 已合入 |
| [REVIEW-20260609-workbench-ui.md](./REVIEW-20260609-workbench-ui.md) | 2026-06-09 | 方案工作台 UI | 已被后续 workbench / Qt 审查覆盖 |
| [REVIEW-20260702-workbench-step-d.md](./REVIEW-20260702-workbench-step-d.md) | 2026-07-02 | 工作台 Step D | 已闭环 |
| [REVIEW-20260706-ui-review-bugfix.md](./REVIEW-20260706-ui-review-bugfix.md) | 2026-07-06 | UI Review 六项修复 | 已合入（CHANGELOG 有链） |
| [REVIEW-20260708-platform-default.md](./REVIEW-20260708-platform-default.md) | 2026-07-08 | 设为平台默认 | 已合入 |
| [REVIEW-20260708-pyside6-p1-p2.md](./REVIEW-20260708-pyside6-p1-p2.md) | 2026-07-08 | PySide6 Phase 1+2 | 已吸收进分支总审 / Phase3 |
| [REVIEW-20260709-pyside6-phase3-core.md](./REVIEW-20260709-pyside6-phase3-core.md) | 2026-07-09 | Phase3 + 核心域 | 处置表已并入分支总审 |

## 归档策略（简）

- **不删 git 历史**：搬迁用 `git mv`，旧路径在历史中仍可查。  
- **根目录只留活文档**，避免「十几份 REVIEW 全像未完成任务」。  
- 目录标准、迁移说明仍以 `docs/migrations/` 为准；本目录长文仅作当时决策快照。

归档日期：2026-07-09  
