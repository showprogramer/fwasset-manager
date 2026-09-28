"""工作台右侧详情：选中程序的属性、按 flash_mode 挂载的操作面板与目录类操作。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from fwasset.core.asset_helpers import (
    asset_primary_file_name,
    handcontrol_copy_filenames,
)
from fwasset.core.types import FirmwareAsset
from fwasset.ui_common.view_models.scheme_workbench_model import ModuleVariant
from fwasset.ui_common.workbench_helpers import (
    flash_mode_label,
    module_label_from_asset,
    source_tag,
)
from fwasset.ui_qt.data_grid import tag_colors
from fwasset.ui_qt.design_tokens import (
    DETAIL_PANE_WIDTH,
    PANE_BACKGROUND,
    PANE_BORDER,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    STATUS_COLORS,
    TAG_RADIUS,
)
from fwasset.ui_qt.theming import bind_qss, pick

_BREAK_AFTER = "_-.·"
USB_PLACEHOLDER = "未发现 U 盘"
_ZERO_WIDTH_SPACE = "\u200b"


def break_long_name(text: str) -> str:
    """在 _ - . 后插入零宽空格，让无空格的长文件名能在分隔处换行。"""
    return "".join(
        ch + _ZERO_WIDTH_SPACE if ch in _BREAK_AFTER else ch for ch in text
    )


def _rgba(color) -> str:  # noqa: ANN001 - QColor
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


class TagLabel(QLabel):
    """与表格「归属」列同色的小标签。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = "accent"
        bind_qss(self, self._qss)

    def _qss(self) -> str:
        bg, fg = tag_colors(self._kind)
        return (
            f"QLabel {{ background: {_rgba(bg)}; color: {_rgba(fg)};"
            f" border-radius: {TAG_RADIUS}px; padding: 1px 7px; font-size: 12px; }}"
        )

    def set_tag(self, text: str, kind: str) -> None:
        self._kind = kind
        self.setText(text)
        self.setStyleSheet(self._qss())
        self.setVisible(bool(text))


def _separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("detailSeparator")
    line.setFixedHeight(1)
    bind_qss(line, lambda: f"#detailSeparator {{ background: {pick(PANE_BORDER)}; }}")
    return line


class DetailPane(QFrame):
    """选中程序详情。宿主负责挂载操作面板（``ops_layout``）与响应信号。"""

    vendor_edit_requested = Signal()
    open_dir_requested = Signal()
    copy_dir_requested = Signal()
    copy_file_requested = Signal()
    borrow_requested = Signal(QPoint)
    delete_requested = Signal()
    usb_refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailPane")
        self.setFixedWidth(DETAIL_PANE_WIDTH)
        bind_qss(
            self,
            lambda: (
                f"#detailPane {{ background: {pick(PANE_BACKGROUND)};"
                f" border-left: 1px solid {pick(PANE_BORDER)}; }}"
            ),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_MD)
        root.setSpacing(SPACE_MD)

        self.caption = CaptionLabel("选中程序", self)
        root.addWidget(self.caption)

        self.empty = self._build_empty()
        root.addWidget(self.empty)

        self.content = QWidget(self)
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(SPACE_MD)

        # 长名称靠零宽空格换行；不开鼠标选取，免得复制出零宽字符（有复制按钮）。
        self.name_label = StrongBodyLabel("", self.content)
        self.name_label.setWordWrap(True)
        body.addWidget(self.name_label)

        tags = QHBoxLayout()
        tags.setSpacing(SPACE_XS)
        self.module_tag = TagLabel(self.content)
        self.source_tag = TagLabel(self.content)
        tags.addWidget(self.module_tag)
        tags.addWidget(self.source_tag)
        tags.addStretch(1)
        body.addLayout(tags)

        body.addLayout(self._build_properties())

        self.notice = BodyLabel("", self.content)
        self.notice.setWordWrap(True)
        self.notice.hide()
        body.addWidget(self.notice)

        body.addWidget(_separator(self.content))

        self.usb_row = QWidget(self.content)
        usb = QHBoxLayout(self.usb_row)
        usb.setContentsMargins(0, 0, 0, 0)
        usb.setSpacing(SPACE_XS)
        usb.addWidget(CaptionLabel("U 盘", self.usb_row))
        self.usb_combo = ComboBox(self.usb_row)
        self.usb_combo.setPlaceholderText(USB_PLACEHOLDER)
        usb.addWidget(self.usb_combo, stretch=1)
        self.usb_refresh = TransparentToolButton(FluentIcon.SYNC, self.usb_row)
        self.usb_refresh.setToolTip("刷新 U 盘列表")
        self.usb_refresh.clicked.connect(self.usb_refresh_requested)
        usb.addWidget(self.usb_refresh)
        body.addWidget(self.usb_row)

        self.ops_layout = QVBoxLayout()
        self.ops_layout.setContentsMargins(0, 0, 0, 0)
        self.ops_layout.setSpacing(SPACE_SM)
        body.addLayout(self.ops_layout)

        root.addWidget(self.content)
        root.addStretch(1)

        self.footer = self._build_footer()
        root.addWidget(self.footer)

        self.show_empty()

    # ------------------------------------------------------------------ 构建
    def _build_empty(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, SPACE_LG * 3, 0, 0)
        layout.setSpacing(SPACE_SM)
        icon = IconWidget(FluentIcon.DOCUMENT, box)
        icon.setFixedSize(32, 32)
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)
        title = BodyLabel("选择一个程序", box)
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)
        hint = CaptionLabel("在左侧列表中选中程序，查看属性并复制到 U 盘等操作。", box)
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(hint)
        return box

    def _build_properties(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE_MD)
        grid.setVerticalSpacing(SPACE_SM)
        grid.setColumnStretch(1, 1)

        def key(text: str, row: int) -> None:
            label = CaptionLabel(text, self.content)
            grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)

        def value(row: int) -> BodyLabel:
            label = BodyLabel("", self.content)
            label.setWordWrap(True)
            return label

        key("版本", 0)
        self.version_value = value(0)
        grid.addWidget(self.version_value, 0, 1)

        key("厂商", 1)
        self.vendor_value = value(1)
        self.vendor_edit = TransparentToolButton(FluentIcon.EDIT, self.content)
        self.vendor_edit.setFixedSize(24, 24)
        self.vendor_edit.setToolTip("改厂商")
        self.vendor_edit.clicked.connect(self.vendor_edit_requested)
        vendor_row = QHBoxLayout()
        vendor_row.setSpacing(SPACE_XS)
        vendor_row.addWidget(self.vendor_value)
        vendor_row.addWidget(self.vendor_edit)
        vendor_row.addStretch(1)
        grid.addLayout(vendor_row, 1, 1)

        key("文件", 2)
        self.files_value = value(2)
        self.file_copy = TransparentToolButton(FluentIcon.COPY, self.content)
        self.file_copy.setFixedSize(24, 24)
        self.file_copy.setToolTip("复制主文件路径")
        self.file_copy.clicked.connect(self.copy_file_requested)
        files_row = QHBoxLayout()
        files_row.setSpacing(SPACE_XS)
        files_row.addWidget(self.files_value, stretch=1)
        files_row.addWidget(self.file_copy, alignment=Qt.AlignmentFlag.AlignTop)
        grid.addLayout(files_row, 2, 1)

        key("方式", 3)
        self.mode_value = value(3)
        grid.addWidget(self.mode_value, 3, 1)
        return grid

    def _build_footer(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)
        layout.addWidget(_separator(box))
        row = QHBoxLayout()
        row.setSpacing(0)
        self.open_dir_btn = TransparentPushButton(FluentIcon.FOLDER, "目录", box)
        self.open_dir_btn.setToolTip("打开所在目录")
        self.open_dir_btn.clicked.connect(self.open_dir_requested)
        self.copy_dir_btn = TransparentPushButton(FluentIcon.COPY, "路径", box)
        self.copy_dir_btn.setToolTip("复制目录路径")
        self.copy_dir_btn.clicked.connect(self.copy_dir_requested)
        self.borrow_btn = TransparentPushButton(FluentIcon.LINK, "关联", box)
        self.borrow_btn.setToolTip("关联、更换或解除其他型号的程序")
        self.borrow_btn.clicked.connect(
            lambda: self.borrow_requested.emit(
                self.borrow_btn.mapToGlobal(QPoint(0, self.borrow_btn.height()))
            )
        )
        danger = STATUS_COLORS["error"]
        self.delete_btn = TransparentToolButton(
            FluentIcon.DELETE.colored(QColor(danger[0][1]), QColor(danger[1][1])), box
        )
        self.delete_btn.setToolTip("删除（可在回收站还原）")
        self.delete_btn.clicked.connect(self.delete_requested)
        for widget in (self.open_dir_btn, self.copy_dir_btn, self.borrow_btn):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.delete_btn)
        layout.addLayout(row)
        return box

    # ------------------------------------------------------------------ 状态
    def show_empty(self) -> None:
        self.caption.setText("选中程序")
        self.empty.show()
        self.content.hide()
        self.footer.hide()

    def show_variant(
        self,
        variant: ModuleVariant,
        *,
        can_write: bool,
        can_delete: bool,
    ) -> str:
        """填充属性区，返回应挂载的 flash_mode（借用缺失返回空串）。"""
        asset: FirmwareAsset = variant.effective_asset or variant.asset
        missing = variant.shared_state == "shared_missing"
        self.empty.hide()
        self.content.show()
        self.footer.show()

        name = variant.name or str(asset.get("directory_name", "")) or "-"
        self.name_label.setText(break_long_name(name))
        self.name_label.setToolTip(name)
        self.module_tag.set_tag(module_label_from_asset(asset), "accent")
        text, kind = source_tag(
            variant.source_label,
            variant.source_kind,
            variant.shared_state,
            variant.shared_source_label,
        )
        self.source_tag.set_tag(text, kind)
        self.source_tag.setToolTip(variant.shared_source_label)

        self.version_value.setText(variant.version or str(asset.get("version", "")) or "-")
        self.vendor_value.setText(str(asset.get("vendor", "")).strip() or "未填写")
        # 厂商写在本型号的程序上；借用行的程序属于源型号，不在这里改。
        self.vendor_edit.setVisible(can_write and not variant.borrowed_only and not missing)
        files = handcontrol_copy_filenames(list(asset.get("files", []))) or [
            asset_primary_file_name(asset)
        ]
        files_text = "\n".join(file for file in files if file) or "-"
        self.files_value.setText(break_long_name(files_text))
        self.files_value.setToolTip(files_text)
        mode = str(asset.get("flash_mode", "disabled")) or "disabled"
        self.mode_value.setText(flash_mode_label(mode))

        self.notice.setVisible(missing)
        if missing:
            self.notice.setText(
                "源型号未设默认，没有可用的关联程序。"
                if variant.shared_reason == "no_source_default"
                else "关联来源缺失，未回落本地副本，不能烧录。"
            )
        self.usb_row.setVisible(not missing and mode == "auto_usb")
        self.borrow_btn.setVisible(can_write)
        self.delete_btn.setVisible(can_delete)
        for button in (self.open_dir_btn, self.copy_dir_btn, self.file_copy):
            button.setEnabled(not missing)
        return "" if missing else mode

    def show_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.show()
