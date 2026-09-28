from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QVBoxLayout, QWidget

from fwasset.core.types import FirmwareAsset
from fwasset.ui_qt.design_tokens import SPACE_SM

if TYPE_CHECKING:
    from fwasset.ui_qt.operation_panels.host_types import PanelHost


class BaseOperationPanel(QWidget):
    """Qt 操作面板基类。子类在 build() 里向 self.body 追加内容。"""

    def __init__(
        self, *, asset: FirmwareAsset, log_fn, panel_host: PanelHost, parent=None
    ):
        super().__init__(parent)
        self.asset = asset
        self._log = log_fn
        self._panel_host: PanelHost = panel_host
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SPACE_SM)

    def build(self):
        """子类重写此方法构建 UI。"""
