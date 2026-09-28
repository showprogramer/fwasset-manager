"""运行日志：内存缓冲 + 状态栏弹出查看。

日志不再常驻工作台；状态栏「活动」显示自上次查看以来的错误数，点击弹出
``LogView``。文件日志仍由 ``FileLogger`` 负责。
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FlyoutViewBase,
    StrongBodyLabel,
    TransparentToolButton,
)

from fwasset.ui_qt.design_tokens import LOG_VIEW_SIZE, SPACE_SM

LOG_LIMIT = 2000
_ERROR_MARKERS = ("失败", "错误", "异常", "✗")


def is_error_line(message: str) -> bool:
    return any(marker in message for marker in _ERROR_MARKERS)


class ActivityLog(QObject):
    """日志缓冲；``changed`` 在新增或已读时发出，供状态栏刷新。"""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lines: deque[str] = deque(maxlen=LOG_LIMIT)
        self._unread_errors = 0

    def write(self, message: str) -> None:
        text = (message or "").rstrip("\n")
        if not text:
            return
        self._lines.append(text)
        if is_error_line(text):
            self._unread_errors += 1
        self.changed.emit()

    def lines(self) -> list[str]:
        return list(self._lines)

    def text(self) -> str:
        return "\n".join(self._lines)

    @property
    def unread_errors(self) -> int:
        return self._unread_errors

    def mark_read(self) -> None:
        if self._unread_errors:
            self._unread_errors = 0
            self.changed.emit()

    def clear(self) -> None:
        self._lines.clear()
        self._unread_errors = 0
        self.changed.emit()


class LogView(FlyoutViewBase):
    """弹出层内的只读日志视图；打开时滚到末尾并跟随新日志。"""

    def __init__(self, log: ActivityLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log = log
        self.setFixedSize(*LOG_VIEW_SIZE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_SM, SPACE_SM, SPACE_SM, SPACE_SM)
        layout.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("运行日志", self))
        header.addStretch(1)
        self.copy_btn = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_btn.setToolTip("复制全部")
        self.copy_btn.clicked.connect(self._copy)
        header.addWidget(self.copy_btn)
        self.clear_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.clear_btn.setToolTip("清空")
        self.clear_btn.clicked.connect(log.clear)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self.log_text = QPlainTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(LOG_LIMIT)
        layout.addWidget(self.log_text, stretch=1)

        self._reload()
        log.changed.connect(self._reload)
        log.mark_read()

    def _reload(self) -> None:
        self.log_text.setPlainText(self._log.text())
        bar = self.log_text.verticalScrollBar()
        bar.setValue(bar.maximum())
        if self.isVisible():
            self._log.mark_read()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._log.text())
