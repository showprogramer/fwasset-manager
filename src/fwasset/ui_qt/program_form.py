"""新建程序与更新程序共用的表单控件：范围下拉、文件拖放区。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QWidget,
)

from fwasset.ui_common.workspace_actions import direct_scheme_dir_names

NEW_SCHEME = "__new_scheme__"

# on_create(方案名, finish)：调用方负责落盘，完成后回调 finish(是否成功)
CreateScheme = Callable[[str, Callable[[bool], None]], None]


class ScopeCombo(QComboBox):
    """「通用 / 各方案 / + 新建定制方案…」单个下拉；``scheme()`` 为空表示通用。"""

    scope_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        *,
        model_root: Path,
        on_create: CreateScheme,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("scope_combo")
        self._model_root = Path(model_root)
        self._on_create = on_create
        self._last = 0
        self.reload()
        self.activated.connect(self._on_activated)

    def reload(self, select: str = "") -> None:
        self.blockSignals(True)
        self.clear()
        self.addItem("通用", "")
        for name in direct_scheme_dir_names(self._model_root):
            self.addItem(name, name)
        self.insertSeparator(self.count())
        self.addItem("+ 新建定制方案…", NEW_SCHEME)
        index = self.findData(select) if select else 0
        self.setCurrentIndex(index if index >= 0 else 0)
        self._last = self.currentIndex()
        self.blockSignals(False)
        self.scope_changed.emit()

    def scheme(self) -> str:
        data = self.currentData()
        return "" if data in (None, NEW_SCHEME) else str(data)

    def set_scheme(self, name: str) -> None:
        index = self.findData(name)
        self._select(index if index >= 0 else 0)

    def set_common_only(self, on: bool) -> None:
        """借用只支持通用：锁在「通用」。"""
        if on:
            self._select(0)
        self.setEnabled(not on)

    def _select(self, index: int) -> None:
        changed = index != self._last
        self.blockSignals(True)
        self.setCurrentIndex(index)
        self.blockSignals(False)
        self._last = index
        if changed:
            self.scope_changed.emit()

    def _on_activated(self, index: int) -> None:
        if self.itemData(index) != NEW_SCHEME:
            if index != self._last:
                self._last = index
                self.scope_changed.emit()
            return
        # 选中「新建」只是动作：先回到原选项，建成后再选中新方案
        self.blockSignals(True)
        self.setCurrentIndex(self._last)
        self.blockSignals(False)
        name, accepted = QInputDialog.getText(self, "新建定制方案", "方案名")
        scheme_name = str(name).strip()
        if not accepted or not scheme_name:
            return

        def finish(ok: bool) -> None:
            if ok:
                self.reload(select=scheme_name)

        self._on_create(scheme_name, finish)


class FileDropZone(QFrame):
    """「拖到这里，或点选择」：只收本地文件，文件夹忽略。"""

    files_chosen = Signal(list)

    def __init__(self, parent: QWidget | None, *, hint: str = "拖到这里，或点选择") -> None:
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setStyleSheet(
            "QFrame#dropZone { border: 1px dashed #b8c4cc; border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        self.label = QLabel(hint, self)
        self.label.setWordWrap(True)
        self.button = QPushButton("选择…", self)
        row.addWidget(self.label, stretch=1)
        row.addWidget(self.button)
        self.button.clicked.connect(self._pick)

    def show_files(self, files: list[str]) -> None:
        names = "、".join(Path(item).name for item in files[:3])
        more = f" 等 {len(files)} 个" if len(files) > 3 else ""
        self.label.setText(f"已选择：{names}{more}")

    def _pick(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(self, "选择新的程序文件")
        if paths:
            self.files_chosen.emit(list(paths))

    @staticmethod
    def _local_files(event: QDragEnterEvent | QDropEvent) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [
            url.toLocalFile()
            for url in mime.urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        files = self._local_files(event)
        if files:
            event.acceptProposedAction()
            self.files_chosen.emit(files)
