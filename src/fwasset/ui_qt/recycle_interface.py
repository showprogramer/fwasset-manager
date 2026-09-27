"""回收站页。删除的程序在这里保留 1 小时，可还原或永久删除。

出了这个回收站就找不回来——清理一律真删，不进电脑回收站。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PushButton, SubtitleLabel

from fwasset.core.quarantine import discard_now, list_records
from fwasset.core.services.asset_service import undo_asset_delete
from fwasset.core.workspace_transaction import WorkspaceLock
from fwasset.ui_common.workspace_actions import recycle_rows
from fwasset.ui_qt.design_tokens import SPACE_LG, SPACE_MD

_EMPTY = "回收站是空的"
_NOTICE = "删除的程序在这里保留 1 小时。出了回收站就永久删除，电脑回收站里也没有。"


class RecycleInterface(QWidget):
    """列出可还原的删除项，提供还原 / 彻底删除 / 清空。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("recycleInterface")
        self._root = ""
        self._host: Any = None
        self._rows: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        layout.setSpacing(SPACE_MD)
        layout.addWidget(SubtitleLabel("回收站", self))
        self.caption = CaptionLabel(_NOTICE, self)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.rows = QListWidget(self)
        self.rows.setWordWrap(True)
        layout.addWidget(self.rows, stretch=1)

        buttons = QHBoxLayout()
        self.restore_button = PushButton("还原", self)
        self.discard_button = PushButton("彻底删除", self)
        self.empty_button = PushButton("清空回收站", self)
        refresh = PushButton("刷新", self)
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.discard_button)
        buttons.addStretch(1)
        buttons.addWidget(self.empty_button)
        buttons.addWidget(refresh)
        layout.addLayout(buttons)

        self.restore_button.clicked.connect(self._restore_selected)
        self.discard_button.clicked.connect(self._discard_selected)
        self.empty_button.clicked.connect(self._empty_all)
        refresh.clicked.connect(self.reload)
        self.rows.currentRowChanged.connect(lambda _r: self._sync_buttons())

    def bind(self, host: Any, root: str) -> None:
        self._host = host
        self._root = (root or "").strip()
        if self.isVisible():
            self.reload()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.reload()

    def reload(self) -> None:
        self.rows.clear()
        self._rows = []
        if self._root and Path(self._root).is_dir():
            try:
                records = [dict(item) for item in list_records(self._root)]
            except Exception as exc:  # noqa: BLE001
                self.caption.setText(f"{_NOTICE}\n读取回收站失败：{exc}")
                self._sync_buttons()
                return
            self._rows = recycle_rows(records, now=time.time())
        for row in self._rows:
            item = QListWidgetItem(
                f"{row['name']}    {row['remaining']}\n{row['original_path']}"
            )
            item.setToolTip(row["original_path"])
            self.rows.addItem(item)
        self.caption.setText(_NOTICE if self._rows else f"{_NOTICE}\n\n{_EMPTY}")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_selection = 0 <= self.rows.currentRow() < len(self._rows)
        self.restore_button.setEnabled(has_selection)
        self.discard_button.setEnabled(has_selection)
        self.empty_button.setEnabled(bool(self._rows))

    def _selected(self) -> dict[str, Any] | None:
        index = self.rows.currentRow()
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def _finish(self, message: str) -> None:
        if self._host is not None:
            self._host._log(message)
            self._host._refresh_main_grid(reload_data=True)
        self.reload()

    def _restore_selected(self) -> None:
        row = self._selected()
        if row is None or self._host is None:
            return
        root = self._root

        def run(log: Any) -> dict[str, Any]:
            return undo_asset_delete(root, row["record_id"], log)

        def done(result: dict[str, Any]) -> None:
            message = str(result.get("message") or "")
            if result.get("ok"):
                self._finish(message or f"已还原「{row['name']}」")
                return
            QMessageBox.warning(self, "无法还原", message)
            self.reload()

        self._host.run_write("还原", run, done)

    def _discard_selected(self) -> None:
        row = self._selected()
        if row is None:
            return
        if not self._confirm_purge(f"彻底删除「{row['name']}」？", 1):
            return
        self._purge([row["record_id"]])

    def _empty_all(self) -> None:
        if not self._rows:
            return
        if not self._confirm_purge("清空回收站？", len(self._rows)):
            return
        self._purge([row["record_id"] for row in self._rows])

    def _confirm_purge(self, title: str, count: int) -> bool:
        answer = QMessageBox.question(
            self,
            "彻底删除",
            f"{title}\n\n"
            f"将永久删除 {count} 项，电脑回收站里也不会有，无法恢复。",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _purge(self, record_ids: list[str]) -> None:
        if self._host is None:
            return
        root = self._root

        def run(_log: Any) -> dict[str, Any]:
            # discard_now 要求调用方已持有工作区写锁。
            with WorkspaceLock(root):
                processed = discard_now(root, record_ids)
            failed = [r for r in processed if r["status"] == "send_failed"]
            return {
                "ok": not failed,
                "code": "ok" if not failed else "purge_failed",
                "message": (
                    f"已永久删除 {len(processed) - len(failed)} 项"
                    if not failed
                    else f"{len(failed)} 项清理失败，将在下次启动时重试"
                ),
                "payload": {},
            }

        def done(result: dict[str, Any]) -> None:
            message = str(result.get("message") or "")
            if result.get("ok"):
                self._finish(message)
                return
            QMessageBox.warning(self, "清理未完成", message)
            self.reload()

        self._host.run_write("彻底删除", run, done)
