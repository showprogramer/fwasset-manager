"""表格上方的内联提示条（撤销、恢复、未配置等）。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, FluentIcon, HyperlinkButton, IconWidget

from fwasset.ui_qt.design_tokens import SPACE_SM, SPACE_XS, STATUS_COLORS
from fwasset.ui_qt.theming import bind_qss, pick

_ICONS = {
    "info": FluentIcon.INFO,
    "success": FluentIcon.COMPLETED,
    "warning": FluentIcon.INFO,
    "error": FluentIcon.CANCEL,
}


class InlineBanner(QFrame):
    """带语义底色、可选操作链接的单行提示。默认隐藏，``show_message`` 显示。"""

    def __init__(self, kind: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inlineBanner")
        self._kind = kind
        self._on_action: Callable[[], None] | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM + SPACE_XS, SPACE_XS + 2, SPACE_SM, SPACE_XS + 2)
        row.setSpacing(SPACE_SM)
        self.icon = IconWidget(_ICONS.get(kind, FluentIcon.INFO), self)
        self.icon.setFixedSize(14, 14)
        row.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.label = BodyLabel("", self)
        self.label.setWordWrap(True)
        row.addWidget(self.label, stretch=1)
        self.action = HyperlinkButton(self)
        self.action.clicked.connect(self._fire)
        self.action.hide()
        row.addWidget(self.action)
        bind_qss(self, self._qss)
        self.hide()

    def _qss(self) -> str:
        light, dark = STATUS_COLORS.get(self._kind, STATUS_COLORS["info"])
        self.label.setTextColor(QColor(light[1]), QColor(dark[1]))
        bg = pick((light[0], dark[0]))
        return f"#inlineBanner {{ background: {bg}; border-radius: 6px; }}"

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.icon.setIcon(_ICONS.get(kind, FluentIcon.INFO))
        self.setStyleSheet(self._qss())

    def show_message(
        self,
        text: str,
        action_text: str = "",
        on_action: Callable[[], None] | None = None,
    ) -> None:
        self.label.setText(text)
        self._on_action = on_action
        self.action.setText(action_text)
        self.action.setVisible(bool(action_text and on_action))
        self.show()

    def text(self) -> str:
        return self.label.text()

    def _fire(self) -> None:
        if self._on_action is not None:
            self._on_action()
