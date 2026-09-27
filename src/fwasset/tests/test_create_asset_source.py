"""新建程序：来源类型由所选路径推断，不由下拉框指定。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fwasset.ui_common.asset_name_prefill import (
    classify_create_source,
    describe_create_source,
)


def test_classify_create_source_infers_directory_archive_or_files() -> None:
    folder = classify_create_source(directory=r"D:\src")
    assert folder.kind == "directory"
    assert folder.source == r"D:\src"
    assert folder.files == ()
    assert describe_create_source(folder) == r"已选择：文件夹 D:\src"

    archive = classify_create_source(files=[r"D:\src\UI.ZIP"])
    assert archive.kind == "archive"
    assert archive.source == r"D:\src\UI.ZIP"
    assert archive.files == ()
    assert describe_create_source(archive) == "已选择：UI.ZIP"

    one = classify_create_source(files=[r"D:\src\readme.txt"])
    assert one.kind == "files"
    assert one.source == r"D:\src\readme.txt"
    assert one.files == (r"D:\src\readme.txt",)
    assert describe_create_source(one) == "已选择：1 个文件"

    many = classify_create_source(files=["a.rom", "b.pkg", "c.zip"])
    assert many.kind == "files"
    assert many.files == ("a.rom", "b.pkg", "c.zip")
    assert describe_create_source(many) == "已选择：3 个文件"


def test_classify_create_source_treats_cancel_as_empty() -> None:
    empty = classify_create_source(directory="  ")
    assert empty.kind == ""
    assert empty.source is None
    assert empty.files == ()
    assert describe_create_source(empty) == "尚未选择来源"
    assert classify_create_source(files=[]).kind == ""
    assert classify_create_source(files=["", "  "]).kind == ""


def test_create_asset_dialog_infers_source_without_kind_combo(monkeypatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QFileDialog,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QWidget,
    )

    from fwasset.ui_qt.entry_flows import open_create_asset

    QApplication.instance() or QApplication([])
    notices: list[str] = []
    calls: list[dict[str, object]] = []
    picker = {"directory": "", "files": []}

    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **kwargs: notices.append(str(args[-1] if args else "")),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *args, **kwargs: picker["directory"],
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *args, **kwargs: (list(picker["files"]), ""),
    )

    def fake_create_asset(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "code": "ok", "message": "已入库", "payload": {}}

    monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.create_asset", fake_create_asset
    )

    class _Host(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.root_dir = r"D:\workspace"
            self.current_selection = SimpleNamespace(model_name="L36")

        def model_root_for(self, name: str) -> Path:
            return Path(r"D:\workspace\L36")

        def _write_gate(self, target: object) -> bool:
            return True

        def _log(self, message: str) -> None:
            return None

        def _refresh_main_grid(self) -> None:
            return None

        def run_write(self, name: str, fn, on_done) -> None:
            on_done(fn(lambda _line: None))

    host = _Host()

    def open_and_drive(dialog) -> int:
        combos = dialog.findChildren(QComboBox)
        for combo in combos:
            stored = [combo.itemData(i) for i in range(combo.count())]
            assert "directory" not in stored
            assert "archive" not in stored
            assert "files" not in stored
            assert "散选文件" not in [combo.itemText(i) for i in range(combo.count())]
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        assert "选择来源" not in buttons
        # 程序内容都是平铺文件，新建只从文件选；整夹导入走 zip
        assert "选择文件夹" not in buttons
        file_button = buttons["选择文件"]
        save_button = buttons["保存"]
        source_label = next(
            label
            for label in dialog.findChildren(QLabel)
            if label.text() == "尚未选择来源"
        )
        name_box = dialog.findChild(QLineEdit)
        assert name_box is not None
        hint = next(
            label
            for label in dialog.findChildren(QLabel)
            if label is not source_label and label.wordWrap()
        )

        save_button.click()
        assert notices[-1] == "请先选择来源。"
        assert calls == []

        picker["files"] = [r"D:\包\UI.ZIP"]
        file_button.click()
        assert source_label.text() == "已选择：UI.ZIP"
        assert name_box.text() == "UI"
        assert hint.text() == ""
        save_button.click()
        assert calls[-1]["source_kind"] == "archive"
        assert calls[-1]["source"] == r"D:\包\UI.ZIP"
        save_button.setEnabled(True)

        picker["files"] = [r"D:\散\手控.rom", r"D:\散\说明.txt"]
        file_button.click()
        assert source_label.text() == "已选择：2 个文件"
        assert name_box.text() == "手控"
        assert "仍可保存" in hint.text()
        save_button.click()
        assert calls[-1]["source_kind"] == "files"
        assert calls[-1]["source"] == [r"D:\散\手控.rom", r"D:\散\说明.txt"]
        assert calls[-1]["scope"] == "通用"
        save_button.setEnabled(True)

        # 取消文件对话框不改已选来源
        picker["files"] = []
        file_button.click()
        assert source_label.text() == "已选择：2 个文件"
        save_button.click()
        assert calls[-1]["source_kind"] == "files"
        return 0

    monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec",
        lambda dialog: open_and_drive(dialog),
    )
    open_create_asset(host)
