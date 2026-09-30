"""更新程序对话框：按改了哪几项派生操作（TASK-20260924）。

服务全部打桩，只断言对话框收集的输入与分派。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def _host(widget_cls, root: Path, variant):
    class _Host(widget_cls):
        def __init__(self) -> None:
            super().__init__()
            self.root_dir = str(root)
            self.current_selection = SimpleNamespace(model_name="L36")
            self.grid_panel = SimpleNamespace(get_selected_variant=lambda: variant)
            self.logs: list[str] = []
            self.refreshed = 0

        def model_root_for(self, name: str) -> Path:
            return root / "L36"

        def _write_gate(self, target: object) -> bool:
            return True

        def _log(self, message: str) -> None:
            self.logs.append(message)

        def _refresh_main_grid(self, **_kwargs) -> None:
            self.refreshed += 1

        def run_write(self, name: str, fn, on_done) -> None:
            on_done(fn(lambda _line: None))

    return _Host()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    QApplication.instance() or QApplication([])
    root = tmp_path / "workspace"
    model = root / "L36"
    for scheme in ("西班牙", "葡萄牙"):
        (model / "定制" / scheme).mkdir(parents=True)
        (model / "定制" / scheme / "方案配置.toml").write_text(
            f'name = "{scheme}"\n', encoding="utf-8"
        )
    old = model / "通用" / "主板程序" / "dad"
    old.mkdir(parents=True)
    incoming = tmp_path / "来料"
    incoming.mkdir()
    for name in ("V68.bin", "V68.hex"):
        (incoming / name).write_bytes(b"x")

    calls: dict[str, list] = {"update": [], "change": [], "move": [], "vendor": [], "questions": []}
    hits: list[object] = []

    def fake_update(*args, **kwargs):
        source = Path(args[3])
        calls["update"].append(
            {
                "asset": args[2],
                "source_name": source.name,
                "files": sorted(p.name for p in source.iterdir()),
                "source": source,
                **kwargs,
            }
        )
        return {
            "ok": True,
            "code": "ok",
            "message": "已更新",
            "payload": {"replacement": str(old.parent / source.name)},
        }

    def fake_change(*args, **kwargs):
        calls["change"].append({"args": args, **kwargs})
        return {"ok": True, "code": "ok", "message": "已改", "payload": {}}

    def fake_vendor(*args, **kwargs):
        calls["vendor"].append({"asset": args[2], "vendor": args[3]})
        return {"ok": True, "code": "ok", "message": "", "payload": {}}

    def fake_move(*args, **kwargs):
        calls["move"].append({"old": args[2], "new": args[3]})
        return {
            "ok": True,
            "code": "ok",
            "message": "已移动",
            "payload": {"replacement": str(args[3])},
        }

    def fake_lookup(*args, **kwargs):
        return {
            "ok": True,
            "code": "ok",
            "message": "",
            "payload": {"result": SimpleNamespace(hits=list(hits))},
        }

    monkeypatch.setattr("fwasset.ui_qt.entry_flows.update_asset", fake_update)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.change_asset_semantics", fake_change)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.update_asset_vendor", fake_vendor)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.move_asset_in_place", fake_move)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.find_references_to", fake_lookup)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

    def question(*args, **kwargs):
        calls["questions"].append(args[-1])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)

    asset = {
        "path": str(old),
        "firmware_label": "主板程序",
        "category": "common",
        "vendor": "摩众",
    }
    variant = SimpleNamespace(asset=asset, borrowed_only=False)
    return SimpleNamespace(
        host=_host(QWidget, root, variant),
        variant=variant,
        model=model,
        old=old,
        files=[str(incoming / "V68.bin"), str(incoming / "V68.hex")],
        calls=calls,
        hits=hits,
        monkeypatch=monkeypatch,
    )


def _named(dialog, cls, name: str):
    for widget in dialog.findChildren(cls):
        if widget.objectName() == name:
            return widget
    raise AssertionError(f"未找到控件：{name}")


def _parts(dialog):
    from PySide6.QtWidgets import (
        QComboBox,
        QLabel,
        QLineEdit,
        QPushButton,
        QRadioButton,
        QWidget,
    )

    from fwasset.ui_qt.program_form import FileDropZone

    return SimpleNamespace(
        dialog=dialog,
        drop=dialog.findChild(FileDropZone),
        modules=_named(dialog, QComboBox, "module_combo"),
        vendors=_named(dialog, QComboBox, "vendor_combo"),
        scope=_named(dialog, QComboBox, "scope_combo"),
        name=_named(dialog, QLineEdit, "program_name_edit"),
        hint=_named(dialog, QLabel, "update_hint"),
        update={b.text(): b for b in dialog.findChildren(QPushButton)}["更新"],
        keep=_named(dialog, QPushButton, "keep_original_files"),
        retire=_named(dialog, QWidget, "retire_options"),
        radios={r.text(): r for r in dialog.findChildren(QRadioButton)},
    )


def _run(env, drive) -> None:
    from fwasset.ui_qt.entry_flows import open_update_program

    seen: list[bool] = []

    def exec_(dialog) -> int:
        drive(_parts(dialog))
        seen.append(True)
        return 0

    env.monkeypatch.setattr("fwasset.ui_qt.entry_flows.QDialog.exec", exec_)
    open_update_program(env.host)
    assert seen, "对话框没有打开"


def test_defaults_come_from_current_program(env) -> None:
    def drive(ui) -> None:
        assert ui.modules.currentData() == "主板程序"
        assert ui.vendors.currentText() == "摩众"
        assert ui.scope.scheme() == ""
        assert ui.name.text() == "dad"
        assert list(ui.radios) == ["删除旧程序", "留作备用副本"]
        assert ui.radios["删除旧程序"].isChecked()
        assert not ui.update.isEnabled()
        assert "尚未修改程序信息" in ui.hint.text()
        assert ui.retire.isHidden()

    _run(env, drive)


def test_long_current_filename_is_available_without_wrapping(env) -> None:
    name = "YJ_3DMain_Foot_16_MA82G5C64_V40-221223-32023.0710.bin"
    (env.old / name).write_bytes(b"firmware")

    def drive(ui) -> None:
        assert ui.drop.label.text() == "沿用原文件（1 个）"
        assert name in ui.drop.label.toolTip()
        assert not ui.drop.label.wordWrap()
        assert ui.drop.button.text() == "选择新程序"
        assert ui.drop.button.minimumWidth() >= 112
        assert ui.keep.minimumWidth() >= 112
        ui.drop.resize(760, ui.drop.minimumHeight())
        ui.drop.layout().activate()
        assert ui.drop.rect().contains(ui.drop.button.geometry())
        assert ui.drop.rect().contains(ui.keep.geometry())
        ui.drop.files_chosen.emit(env.files)
        assert ui.drop.button.minimumWidth() >= 112

    _run(env, drive)


def test_name_change_keeps_current_files(env) -> None:
    def drive(ui) -> None:
        ui.name.setText("修正名称")
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    assert env.calls["move"] == [
        {"old": env.old, "new": env.old.parent / "修正名称"}
    ]
    assert not env.calls["update"]
    assert not env.calls["change"]


def _drop_bottom_is_inside_dialog(ui) -> None:
    """虚线框底边必须落在字段和窗口里，不能被换行提示挤出父控件。"""
    from PySide6.QtCore import QPoint

    field = ui.drop.parentWidget()
    assert field is not None
    layout = ui.dialog.layout()
    assert layout is not None
    layout.activate()
    assert ui.drop.y() + ui.drop.height() <= field.height()
    bottom = ui.drop.mapTo(ui.dialog, QPoint(0, ui.drop.height() - 1))
    assert bottom.y() < ui.dialog.height()
    assert ui.dialog.height() >= layout.heightForWidth(ui.dialog.width())


def test_can_return_to_original_files_after_picking_new_ones(env) -> None:
    def drive(ui) -> None:
        ui.dialog.show()
        initial_height = ui.dialog.height()
        ui.drop.files_chosen.emit(env.files)
        ui.name.setText("很长的程序名" * 12)
        assert ui.keep.isEnabled()
        assert not ui.retire.isHidden()
        assert ui.dialog.height() > initial_height
        _drop_bottom_is_inside_dialog(ui)
        ui.keep.click()
        _drop_bottom_is_inside_dialog(ui)
        assert ui.name.text() == "dad"
        assert ui.drop.label.text() == "沿用原文件（0 个）"
        assert ui.retire.isHidden()
        ui.name.setText("修正名称")
        ui.update.click()

    _run(env, drive)
    assert len(env.calls["move"]) == 1
    assert not env.calls["update"]


def test_scope_change_without_new_files_moves_current_program(env) -> None:
    def drive(ui) -> None:
        index = ui.scope.findData("葡萄牙")
        ui.scope.setCurrentIndex(index)
        ui.scope.activated.emit(index)
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    assert env.calls["move"][0]["new"] == (
        env.model / "定制" / "葡萄牙" / "主板程序" / "dad"
    )


def test_type_change_without_new_files_moves_current_program(env) -> None:
    def drive(ui) -> None:
        ui.modules.setCurrentIndex(ui.modules.findData("蓝牙程序"))
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    assert env.calls["move"][0]["new"] == env.model / "通用" / "蓝牙程序" / "dad"


def test_vendor_only_updates_metadata_without_moving(env) -> None:
    def drive(ui) -> None:
        ui.vendors.setCurrentText("新厂商")
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    assert env.calls["vendor"] == [{"asset": env.variant.asset, "vendor": "新厂商"}]
    assert not env.calls["move"]


def test_empty_vendor_does_not_select_first_candidate(env) -> None:
    env.variant.asset["vendor"] = ""

    def drive(ui) -> None:
        assert ui.vendors.currentText() == ""
        assert not ui.update.isEnabled()

    _run(env, drive)


def test_legacy_module_leaf_allows_vendor_only(env) -> None:
    leaf = env.old.parent
    env.old.rmdir()
    (leaf / "firmware.bin").write_bytes(b"legacy firmware")
    env.variant.asset["path"] = str(leaf)

    def drive(ui) -> None:
        assert not ui.update.isEnabled()
        assert "布局归一" in ui.hint.text()
        ui.vendors.setCurrentText("新厂商")
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    assert env.calls["vendor"] == [{"asset": env.variant.asset, "vendor": "新厂商"}]
    assert not env.calls["move"]


def test_only_new_files_updates_with_prefilled_name(env) -> None:
    """只换文件：走 update_asset，来源目录以程序名称命名，厂商再单独写入。"""

    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        assert ui.name.text() == "V68"
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    [call] = env.calls["update"]
    assert call["source_name"] == "V68"
    assert call["files"] == ["V68.bin", "V68.hex"]
    assert call["retire_mode"] == "retire_to_trash"
    assert not call["source"].exists(), "临时来源目录应在调用后删除"
    assert not env.calls["change"]
    assert env.calls["vendor"] == [
        {"asset": {"path": str(env.old.parent / "V68")}, "vendor": "摩众"}
    ]
    assert env.host.refreshed == 1


def test_rename_takes_effect_and_backup_option(env) -> None:
    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        ui.name.setText("量产V2")
        ui.radios["留作备用副本"].setChecked(True)
        ui.update.click()

    _run(env, drive)
    [call] = env.calls["update"]
    assert call["source_name"] == "量产V2"
    assert call["retire_mode"] == "retire_to_backup"


def test_same_name_as_old_blocks_plain_update(env) -> None:
    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        ui.name.setText("DAD")
        assert not ui.update.isEnabled()
        assert "不能和旧程序相同" in ui.hint.text()

    _run(env, drive)


def test_scope_change_moves_into_scheme(env) -> None:
    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        index = ui.scope.findData("葡萄牙")
        ui.scope.setCurrentIndex(index)
        ui.scope.activated.emit(index)
        ui.name.setText("dad")
        assert ui.update.isEnabled()
        ui.update.click()

    _run(env, drive)
    [call] = env.calls["change"]
    assert call["args"][4] == str(env.model / "定制" / "葡萄牙" / "主板程序" / "dad")
    assert call["args"][5] == "general_to_custom"
    assert call["vendor"] == "摩众"
    assert not env.calls["update"]


def test_type_change_uses_change_type(env) -> None:
    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        ui.modules.setCurrentIndex(ui.modules.findData("蓝牙程序"))
        ui.update.click()

    _run(env, drive)
    [call] = env.calls["change"]
    assert call["args"][5] == "change_type"
    assert call["args"][4] == str(env.model / "通用" / "蓝牙程序" / "V68")


def test_references_require_confirmation(env) -> None:
    """有借用/默认指向旧程序时先确认；选否不写入。"""
    env.hits.append(SimpleNamespace(kind="shared", module_key="主板程序", owner_root="L50S"))

    def drive(ui) -> None:
        ui.drop.files_chosen.emit(env.files)
        ui.update.click()
        assert ui.update.isEnabled()

    _run(env, drive)
    assert len(env.calls["questions"]) == 1
    assert "L50S" in env.calls["questions"][0]
    assert not env.calls["update"]


def test_borrowed_only_row_is_refused(env) -> None:
    from fwasset.ui_qt.entry_flows import open_update_program

    env.variant.borrowed_only = True
    opened: list[bool] = []
    env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda d: opened.append(True) or 0
    )
    open_update_program(env.host)
    assert not opened


def test_drop_zone_accepts_files_ignores_folders(env, tmp_path: Path) -> None:
    from PySide6.QtCore import QMimeData, QUrl

    from fwasset.ui_qt.program_form import FileDropZone

    folder = tmp_path / "一个文件夹"
    folder.mkdir()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(env.files[0]), QUrl.fromLocalFile(str(folder))])
    event = SimpleNamespace(mimeData=lambda: mime)
    assert [Path(p) for p in FileDropZone._local_files(event)] == [Path(env.files[0])]

    empty = QMimeData()
    assert FileDropZone._local_files(SimpleNamespace(mimeData=lambda: empty)) == []
