from __future__ import annotations

from qfluentwidgets import BodyLabel

from fwasset.ui_qt.operation_panels.base import BaseOperationPanel
from fwasset.ui_qt.operation_panels.registry import register


@register("disabled")
class DisabledPanel(BaseOperationPanel):
    """flash_mode 为空或未知时的默认操作面板；目录类操作在详情底部。"""

    def build(self):
        label = BodyLabel(
            f"{self.asset.get('firmware_label', '该类型')}当前不可自动化操作，"
            "可在下方打开程序目录手动处理。",
            self,
        )
        label.setWordWrap(True)
        self.body.addWidget(label)
