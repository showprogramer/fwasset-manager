"""工作台左栏分类列表：分节标题 + 右侧数量。"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QStyleOptionViewItem
from qfluentwidgets import ListItemDelegate, getFont

from fwasset.ui_qt.design_tokens import MUTED_TEXT, NAV_ITEM_HEIGHT, NAV_SECTION_HEIGHT
from fwasset.ui_qt.theming import pick_color

COUNT_ROLE = Qt.ItemDataRole.UserRole + 1
SECTION_ROLE = Qt.ItemDataRole.UserRole + 2
_TEXT_INDENT = 12


class SidebarNavDelegate(ListItemDelegate):
    """分节行只画小号灰字（无悬停底色）；普通行在右侧补画数量。"""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        height = NAV_SECTION_HEIGHT if index.data(SECTION_ROLE) else NAV_ITEM_HEIGHT
        return QSize(size.width(), height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if index.data(SECTION_ROLE):
            painter.save()
            painter.setFont(getFont(12))
            painter.setPen(pick_color(MUTED_TEXT))
            rect = option.rect.adjusted(_TEXT_INDENT, 0, -_TEXT_INDENT, 0)
            flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
            painter.drawText(rect.adjusted(0, 0, 0, -6), int(flags), str(index.data() or ""))
            painter.restore()
            return

        super().paint(painter, option, index)
        count = index.data(COUNT_ROLE)
        if count in (None, ""):
            return
        painter.save()
        painter.setFont(getFont(12))
        painter.setPen(pick_color(MUTED_TEXT))
        rect = QRect(option.rect).adjusted(0, 0, -_TEXT_INDENT, 0)
        flags = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(rect, int(flags), str(count))
        painter.restore()


def add_nav_entry(nav: QListWidget, text: str, count: int | None = None) -> QListWidgetItem:
    item = QListWidgetItem(text)
    if count is not None:
        item.setData(COUNT_ROLE, str(count))
    nav.addItem(item)
    return item


def add_nav_section(nav: QListWidget, text: str) -> QListWidgetItem:
    item = QListWidgetItem(text)
    item.setFlags(Qt.ItemFlag.NoItemFlags)
    item.setData(SECTION_ROLE, True)
    nav.addItem(item)
    return item
