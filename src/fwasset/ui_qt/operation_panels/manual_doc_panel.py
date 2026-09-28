from __future__ import annotations

from PySide6.QtWidgets import QMessageBox
from qfluentwidgets import BodyLabel, FluentIcon, PushButton

from fwasset.ui_qt.operation_panels.base import BaseOperationPanel
from fwasset.ui_qt.operation_panels.registry import register


@register("manual_doc")
class ManualDocPanel(BaseOperationPanel):
    """manual_doc 类型操作面板：显示说明提示。"""

    def build(self):
        label = BodyLabel("该类型需要按说明人工处理。", self)
        label.setWordWrap(True)
        self.body.addWidget(label)
        doc_btn = PushButton(FluentIcon.DOCUMENT, "查看说明", self)
        doc_btn.clicked.connect(lambda: self._show_manual_doc(self.asset))
        self.body.addWidget(doc_btn)

    def _show_manual_doc(self, asset):
        message = (
            f"固件类型: {asset.get('firmware_label', '-')}\n"
            f"型号: {asset.get('model', '-')}\n"
            f"版本: {asset.get('version', '-')}\n"
            f"目录: {asset.get('path', '-')}\n\n"
            "当前类型暂未接入自动烧录工具，请按对应工艺说明处理。"
        )
        QMessageBox.information(self, "操作说明", message)
