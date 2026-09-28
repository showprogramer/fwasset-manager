# 项目架构与领域约束

本文档保存低频但重要的架构事实。日常任务先看 `AGENTS.md`；只有涉及对应模块时，再读取本文件和源码中的 canonical owner。

## 项目定位

`fwasset` 是基于 Python、PySide6 和 QFluentWidgets 的固件资产管理桌面应用，负责固件目录扫描、分类、索引、检索、工具启动和部分 USB 操作。

详细产品功能和用户操作说明见根目录 `README.md`。

## 代码分层

```text
src/fwasset/
├── core/          # 与 UI 无关的领域逻辑、扫描、索引、配置和服务
├── ui_common/     # 框架无关的 ViewModel 与辅助逻辑
├── ui_qt/         # PySide6 + QFluentWidgets 界面
└── tests/         # 测试代码
```

- `core/` 不依赖 UI 框架。
- `ui_common/` 保存可测试的 ViewModel 和纯辅助逻辑。
- `ui_qt/` 是唯一支持的界面实现，不新增第二套 UI 框架。
- 入口为 `fwasset.app:main`；打包入口保留 `fwasset.spec`。

## 类型与服务契约

- `core/types.py` 是 `TypedDict` 和 `Literal` 的唯一类型真源。新增字段时必须同步更新定义、生产者、消费者和测试。
- 服务位于 `core/services/`，返回 `ServiceResult` 形状：

  ```python
  {"ok": bool, "code": str, "message": str, "payload": dict}
  ```

- 用户可见服务消息使用中文，不使用裸异常代替服务错误码。
- 服务错误码的完整列表以各 service 实现和相关测试为准；当前主要服务包括扫描、固件烧录、USB 修复和音乐文件烧录。

## 固件目录与分类

- `firmware_catalog.toml` 是固件类型注册表，匹配采用条目顺序的 first-match 语义。
- `handcontrol_ui` 的 `.rom` 与 `.pkg` 成对扫描规则不用于导入准入。明确位于 `手控UI` 模块下的 ROM+PKG 与 TXT+IMG 都识别为资产并显示在列表中。ROM+PKG 的版本仍来自 `.rom` 文件名；TXT+IMG 的版本来自程序目录名（厂商文件夹或压缩包名），不读取 `.img` 文件名。操作是格式化 U 盘、把这两个固件文件复制到 U 盘根目录（不含 `程序信息.toml`）后弹出，不称为自动烧录。
- 固件根目录中的 `平台配置.toml` 保留平台元数据和旧版默认程序条目；默认条目不再影响工作台展示。
- `定制/方案名/方案配置.toml` 保存定制方案的名称和平台元数据。
- 资产分类来自整理后的目录树和 TOML：SQLite 只作为搜索缓存，不是最终真源。
- `通用/` 是共享模块，`定制/` 是方案专属模块；方案页仅展示本方案实际存放的程序，不自动补入通用程序。
- UI 对外显示的来源标签使用 `定制专属` 或 `通用`；内部术语不得直接显示给用户。
- 跨型号使用他人程序（代码与规格里称「借用」，`shared_*` / `borrow_*`）在界面和服务消息中一律称「关联」：动作为「关联其他型号的程序 / 更换关联 / 解除关联」，归属标签为「来自 源型号」。不用「共享」，以免与 `通用/` 混淆并丢失归属方向。

## SQLite 索引语义

- `core/asset_index.py` 当前 `SCHEMA_VERSION = 3`。
- 索引采用单工作区语义：一次只服务一个固件根目录，换根扫描就是切换工作区，不保留旧根资产。
- `assets` 是当前工作区的搜索缓存；`hidden_items` 保存隐藏项；`scan_meta` 保存当前根目录元数据。
- 应用内 CRUD 若改变索引写入方式，必须同时检查单工作区语义、TOML 真源和 schema 迁移。

## Qt UI 约束

- 操作面板通过 `ui_qt/operation_panels/registry.py` 的 `@register` 和 `get_panel(flash_mode)` 注册。
- 面板依赖 `PanelHost` 协议，不直接复制主窗口逻辑。
- ViewModel 是独立组合对象，面板组合 ViewModel，不继承 ViewModel。
- 耗时任务使用后台线程和结果队列，UI 线程只负责状态更新；取消使用 `threading.Event`。
- 设计 token 和主题值集中于 `ui_qt/design_tokens.py` 及 QFluentWidgets 主题 API。
- 整机模块按烧录习惯展示：主板、手控 UI、蓝牙、语音、快捷键、3D 机芯板、2D 机芯板、腿部；缺失模块不显示，单变体折叠，多变体展开。

## 配置与运行时数据

- `config.example.toml` 是配置说明模板；本地 `config.toml` 不提交。
- `firmware_catalog.toml` 随代码版本管理。
- 开发运行时数据默认在 `.runtime/`，打包运行时默认在 `runtime/`，可由 `FWASSET_RUNTIME_DIR` 覆盖。
- 数据库、日志和运行缓存属于本地运行数据，不进入版本库。
