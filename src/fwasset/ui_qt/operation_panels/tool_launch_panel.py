from __future__ import annotations

from PySide6.QtWidgets import QMessageBox
from qfluentwidgets import FluentIcon, PrimaryPushButton, StrongBodyLabel

from fwasset.core.firmware_catalog import load_firmware_catalog
from fwasset.core.tool_discovery import discover_tool_path, launch_tool
from fwasset.ui_qt.operation_panels.base import BaseOperationPanel
from fwasset.ui_qt.operation_panels.registry import register


@register("tool_launch")
class ToolLaunchPanel(BaseOperationPanel):
    """tool_launch 类型操作面板：找到烧录工具时提供启动按钮。

    是否保留「打开烧录工具」尚未定，找不到工具时不展示路径与配置提示。
    """

    def build(self):
        fw_type = str(self.asset.get("firmware_type", ""))
        tool_name = str(self.asset.get("tool_name", "")) or "烧录工具"
        tool_dir = str(self.asset.get("tool_dir", ""))

        catalog = load_firmware_catalog()
        tool_path = ""
        dir_keywords: list[str] = []
        for item in catalog.get("firmware_types", []):
            if item.get("key") == fw_type:
                tool_path = item.get("tool_path", "")
                tool_dir = item.get("tool_dir", tool_dir)
                dir_keywords = item.get("dir_keywords", [])
                break

        if not tool_path:
            tool_path = discover_tool_path(
                fw_type,
                tool_name,
                tool_dir=tool_dir,
                dir_keywords=dir_keywords,
            )

        self._current_tool_path = tool_path
        if not tool_path:
            return

        self.body.addWidget(StrongBodyLabel(tool_name, self))
        self.launch_btn = PrimaryPushButton(FluentIcon.APPLICATION, "打开烧录工具", self)
        self.launch_btn.setToolTip(tool_path)
        self.launch_btn.clicked.connect(self._launch_current_tool)
        self.body.addWidget(self.launch_btn)

    def _launch_current_tool(self):
        """启动当前选中的工具。"""
        tool_path = getattr(self, "_current_tool_path", "")
        if not tool_path:
            return

        result = launch_tool(tool_path)
        if result.get("ok"):
            self._log(f"✓ {result.get('message')}")
        else:
            QMessageBox.critical(self, "启动失败", result.get("message", ""))
            self._log(f"✗ {result.get('message')}")
