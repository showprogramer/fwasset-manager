from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QMenu,
    QStyleOptionViewItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    TreeItemDelegate,
    TreeWidget,
    getFont,
    isDarkTheme,
    themeColor,
)

from fwasset.core.asset_helpers import open_path_in_explorer
from fwasset.ui_common.view_models.scheme_workbench_model import (
    ModuleCardData,
    ModuleRow,
    ModuleVariant,
)
from fwasset.ui_common.workbench_helpers import source_tag
from fwasset.ui_qt.design_tokens import (
    GRID_BORDER_RADIUS,
    GRID_COL_WIDTHS,
    GRID_ROW_HEIGHT,
    STATUS_COLORS,
    TAG_RADIUS,
)

_VARIANT_ROLE = Qt.ItemDataRole.UserRole
_TAG_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
TAG_COLUMN = 3
COLUMNS = ["类型", "程序名称", "版本", "归属"]


def tag_colors(kind: str) -> tuple[QColor, QColor]:
    """标签 (底色, 文字色)：accent 跟随主题色，其余取状态色。"""
    if kind == "accent":
        fg = QColor(themeColor())
        bg = QColor(fg)
        bg.setAlpha(46 if isDarkTheme() else 30)
        return bg, fg
    light, dark = STATUS_COLORS.get(kind, STATUS_COLORS["info"])
    bg_hex, fg_hex = dark if isDarkTheme() else light
    return QColor(bg_hex), QColor(fg_hex)


class _TagDelegate(TreeItemDelegate):
    """「归属」列把文字画成圆角标签；其余列沿用 Fluent 树的绘制。"""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), GRID_ROW_HEIGHT)

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().initStyleOption(option, index)
        if index.column() == TAG_COLUMN and index.data(_TAG_KIND_ROLE):
            option.text = ""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        kind = index.data(_TAG_KIND_ROLE)
        text = str(index.data() or "")
        if index.column() != TAG_COLUMN or not kind or not text:
            return
        font = getFont(12)
        metrics = QFontMetrics(font)
        width = min(metrics.horizontalAdvance(text) + 14, option.rect.width() - 8)
        height = metrics.height() + 4
        rect = QRectF(
            option.rect.x() + 4,
            option.rect.y() + (option.rect.height() - height) / 2,
            width,
            height,
        )
        bg, fg = tag_colors(str(kind))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, TAG_RADIUS, TAG_RADIUS)
        painter.setFont(font)
        painter.setPen(fg)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width) - 14)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), elided)
        painter.restore()


class DataGrid(QWidget):
    """整机模块固定层级的可展开树（QTreeWidget 版，语义对齐 ui/panels/data_grid_panel.py）。

    顶层 = 模块类型行（ModuleRow），多变体收在该行子节点下，绝不在顶层铺平。
    程序归属列只显示「定制专属 / 通用」，绝不出现"回源"。
    选中变体子节点才发 ModuleVariant；选中多变体父行发 None（等用户展开）。
    """

    selection_changed = Signal(object)  # ModuleVariant | None
    variant_right_clicked = Signal(object, object)  # (ModuleVariant, QPoint global)

    def __init__(self, on_log, parent=None):
        super().__init__(parent)
        self._on_log = on_log

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tree = TreeWidget(self)
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setItemDelegate(_TagDelegate(self.tree))
        header = self.tree.header()
        for col, width in enumerate(GRID_COL_WIDTHS):
            self.tree.setColumnWidth(col, width)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # 行高由委托给出；不覆盖 Fluent 样式表，否则选中行退回系统默认外观。
        self.tree.setBorderVisible(False)
        self.tree.setBorderRadius(GRID_BORDER_RADIUS)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree)

        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

    # --- 渲染 ---
    @staticmethod
    def _variant_text(variant: ModuleVariant, label: str) -> str:
        # Issue 20-A：始终用变体目录名；空名才退回「默认」
        text = variant.name or ("关联" if variant.borrowed_only else "默认")
        if variant.default_badge:
            text = f"{text}  {variant.default_badge}"
        # 借用命中的来源画在归属标签里，名称列只标缺失。
        if variant.shared_state == "shared_missing":
            if variant.shared_reason == "no_source_default":
                text = f"{text}  源型号未设默认"
            else:
                text = f"{text}  关联来源缺失"
        return text

    @staticmethod
    def _source_text(label: str, source_kind: str) -> str:
        return source_tag(label, source_kind)[0]

    @staticmethod
    def _variant_item(first: str, name: str, variant: ModuleVariant) -> QTreeWidgetItem:
        text, kind = source_tag(
            variant.source_label,
            variant.source_kind,
            variant.shared_state,
            variant.shared_source_label,
        )
        item = QTreeWidgetItem([first, name, variant.version or "-", text])
        item.setData(TAG_COLUMN, _TAG_KIND_ROLE, kind)
        item.setData(0, _VARIANT_ROLE, variant)
        if variant.shared_source_label:
            item.setToolTip(TAG_COLUMN, variant.shared_source_label)
        item.setToolTip(1, str((variant.effective_asset or variant.asset).get("path", "")))
        return item

    def populate_tree(self, rows: list[ModuleRow]) -> None:
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            for row in rows:
                if len(row.variants) == 1:
                    variant = row.variants[0]
                    item = self._variant_item(
                        row.label, self._variant_text(variant, row.label), variant
                    )
                    self.tree.addTopLevelItem(item)
                    continue

                parent = QTreeWidgetItem(
                    [
                        row.label,
                        f"{len(row.variants)} 个变体",
                        "",
                        self._source_text(row.source_label, row.source_kind),
                    ]
                )
                parent.setData(TAG_COLUMN, _TAG_KIND_ROLE, "accent")
                self.tree.addTopLevelItem(parent)
                for variant in row.variants:
                    name = (variant.name or "默认") + (
                        f"  {variant.default_badge}" if variant.default_badge else ""
                    )
                    parent.addChild(self._variant_item("", name, variant))
                parent.setExpanded(True)
        finally:
            self.tree.blockSignals(False)

    def populate(self, data_list: list[ModuleCardData]) -> None:
        """兼容旧调用（通用/全部视图）：按模块类型归组后走 populate_tree。"""
        grouped: dict[str, list[ModuleVariant]] = {}
        for data in data_list:
            asset = data.asset
            label = str(asset.get("firmware_label", "")) or str(
                asset.get("firmware_type", "")
            )
            kind = (
                data.source_kind
                if data.source_kind
                else ("common" if data.is_fallback else "custom")
            )
            raw = (data.source_label or "").strip()
            if raw and "回源" not in raw:
                source_label = self._source_text(raw, kind)
            else:
                source_label = "通用" if kind == "common" else "定制专属"
            variant = ModuleVariant(
                asset=asset,
                name=str(asset.get("directory_name", "")),
                version=str(asset.get("version", "")),
                source_kind=kind,
                source_label=source_label,
                default_badge=data.default_badge,
                shared_state=data.shared_state,
                shared_source_label=data.shared_source_label,
                shared_reason=data.shared_reason,
                effective_asset=data.effective_asset,
                borrowed_only=data.borrowed_only,
            )
            grouped.setdefault(label, []).append(variant)

        rows: list[ModuleRow] = []
        for label, variants in grouped.items():
            row_kind = (
                "custom"
                if any(v.source_kind == "custom" for v in variants)
                else "common"
            )
            rows.append(
                ModuleRow(
                    label=label,
                    source_kind=row_kind,
                    source_label="定制专属" if row_kind == "custom" else "通用",
                    variants=variants,
                )
            )
        self.populate_tree(rows)

    def variant_count(self) -> int:
        """表格中可选变体行数（状态栏「N 个程序」）。"""
        count = 0
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            count += top.childCount() or (1 if top.data(0, _VARIANT_ROLE) else 0)
        return count

    # --- 选择 ---
    def select_first_variant(self) -> bool:
        """选中第一个变体行（自动化验证/回归钩子用），无变体返回 False。"""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, _VARIANT_ROLE) is not None:
                self.tree.setCurrentItem(top)
                return True
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, _VARIANT_ROLE) is not None:
                    self.tree.setCurrentItem(child)
                    return True
        return False

    def get_selected_variant(self) -> ModuleVariant | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, _VARIANT_ROLE)

    # 旧名兼容（PanelHost 走 get_selected_data().asset）
    def get_selected_data(self) -> ModuleVariant | None:
        return self.get_selected_variant()

    def _on_selection(self) -> None:
        self.selection_changed.emit(self.get_selected_variant())

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        variant = item.data(0, _VARIANT_ROLE)
        if variant is None:
            return
        if variant.shared_state == "shared_missing":
            self._on_log("关联来源缺失，无法打开")
            return
        asset = variant.effective_asset or variant.asset
        open_path_in_explorer(str(asset.get("path", "")), self._on_log)

    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        variant = item.data(0, _VARIANT_ROLE)
        if variant is not None:
            self.variant_right_clicked.emit(
                variant, self.tree.viewport().mapToGlobal(pos)
            )
            return
        # 多变体父行：右键给「展开/收起」轻量菜单，避免用户觉得右键无响应
        menu = QMenu(self)
        toggle = QAction("收起" if item.isExpanded() else "展开", menu)
        toggle.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.addAction(toggle)
        menu.exec(self.tree.viewport().mapToGlobal(pos))
