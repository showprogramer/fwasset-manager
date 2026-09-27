# TASK-20260927-handcontrol-usb-copy：手控复制到 U 盘

状态：已完成

## 目标与规则

手控 UI 不是自动烧录。ROM+PKG 与 TXT+IMG 都显示在工作台列表中，并执行同一操作：格式化所选 U 盘，把程序目录里的固件文件复制到 U 盘根目录，然后弹出。不复制 `程序信息.toml`，也不在 U 盘上创建程序文件夹。ROM+PKG 的版本仍来自 `.rom` 文件名；TXT+IMG 的版本来自程序目录名，也就是厂商文件夹或压缩包名。音乐目录复制保持原样。

扫描识别见 `src/fwasset/core/file_scan.py`；复制见 `src/fwasset/core/services/flash_service.py` 的 `run_handcontrol_copy`。

## 验收清单

- [x] 重新读取程序文件夹后，`通用/手控UI` 下列出 TXT+IMG 程序；版本是目录名里的 `V21.07`，不是 `.img` 文件名里的版本；程序文件列显示 `.img`
- [x] 选中该程序后按钮为「复制到 U 盘」；确认后格式化 U 盘、只复制固件文件到根目录并弹出
- [x] ROM+PKG 手控走同一按钮和流程
- [x] 音乐文件仍是目录复制，可选择是否格式化和弹出

## 验证

用户于 2026-09-27 确认验收通过。`ruff`、`mypy` 通过；全量 pytest 1308 passed、1 skipped，覆盖率 92.69%。独立审查无阻断问题，见 [REVIEW-20260927-handcontrol-usb-copy](../../docs/code-review/archive/REVIEW-20260927-handcontrol-usb-copy.md)。
