"""软件修复页。本轮只展示只读行，不执行归一。"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, PushButton, SubtitleLabel

from fwasset.core.legacy_exclusions import scan_legacy_excluded_dirs
from fwasset.core.reference_lookup import enumerate_model_roots
from fwasset.core.services.platform_normalize_service import preview_platform_normalize
from fwasset.ui_common.workspace_actions import repair_rows
from fwasset.ui_qt.design_tokens import SPACE_LG, SPACE_MD

_EMPTY = "没有需要处理的项目"
_CHECKING = "正在检查…"


class RepairInterface(QWidget):
    """打开时预览各型号并列出 legacy 排除目录。没有执行按钮。"""

    _rows_ready = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("repairInterface")
        self._root = ""
        self._generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        layout.setSpacing(SPACE_MD)
        layout.addWidget(SubtitleLabel("软件修复", self))
        self.caption = CaptionLabel(_CHECKING, self)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.rows = QListWidget(self)
        layout.addWidget(self.rows, stretch=1)
        refresh = PushButton("重新检查", self)
        refresh.clicked.connect(self.reload)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self._rows_ready.connect(self._show_rows)

    def set_workspace(self, root: str) -> None:
        self._root = (root or "").strip()
        if self.isVisible():
            self.reload()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.reload()

    def reload(self) -> None:
        self.caption.setText(_CHECKING)
        root = self._root
        self._generation += 1
        generation = self._generation

        def work() -> None:
            payload = _collect(root)
            payload["generation"] = generation
            self._rows_ready.emit(payload)

        threading.Thread(target=work, daemon=True).start()

    def _show_rows(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("generation") != self._generation:
            return
        self.rows.clear()
        texts = payload.get("texts") or []
        issues = payload.get("issues") or []
        if not texts:
            self.caption.setText(_EMPTY)
        else:
            self.caption.setText("以下项目只读展示，本轮不能在这里执行。")
            for text in texts:
                item = QListWidgetItem(str(text))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.rows.addItem(item)
        if issues:
            self.caption.setText(
                self.caption.text() + "\n" + "\n".join(str(item) for item in issues)
            )


def _collect(root: str) -> dict[str, object]:
    if not root or not Path(root).is_dir():
        return {"texts": [], "issues": []}
    previews: list[tuple[str, dict]] = []
    for model_root in enumerate_model_roots(Path(root)):
        result = preview_platform_normalize(root, root, model_root)
        previews.append((model_root.name, result))
    legacy = scan_legacy_excluded_dirs(root)
    rows = repair_rows(previews, [dict(item) for item in legacy])
    return {"texts": [row.text for row in rows], "issues": []}
