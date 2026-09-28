"""随明暗主题切换的自定义样式。

QFluentWidgets 的 ``setCustomStyleSheet`` 只作用于其自带控件；普通 QFrame
的衬底、状态条等用这里的 ``bind_qss`` 跟随主题。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget
from qfluentwidgets import isDarkTheme, qconfig


def pick(pair: tuple[str, str]) -> str:
    """按当前主题从 (light, dark) 中取值。"""
    return pair[1] if isDarkTheme() else pair[0]


def pick_color(pair: tuple[str, str]) -> QColor:
    return QColor(pick(pair))


def bind_qss(widget: QWidget, builder: Callable[[], str]) -> None:
    """立即应用样式，并在主题切换时按 ``builder`` 重算。"""
    widget.setStyleSheet(builder())

    def _refresh(*_args: object) -> None:
        try:
            widget.setStyleSheet(builder())
        except RuntimeError:  # 控件已销毁
            pass

    qconfig.themeChanged.connect(_refresh)
    widget.destroyed.connect(lambda *_a: _disconnect(_refresh))


def _disconnect(slot: Callable[..., None]) -> None:
    try:
        qconfig.themeChanged.disconnect(slot)
    except (RuntimeError, TypeError):
        pass
