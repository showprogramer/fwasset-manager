"""新建程序：范围（通用/定制）、对话框内新建方案、使用其他型号的程序。

TASK-20260923。对话框只收集输入并分派到既有服务，判定留在服务层。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_host(widget_cls, root: Path):
    class _Host(widget_cls):
        def __init__(self) -> None:
            super().__init__()
            self.root_dir = str(root)
            self.current_selection = SimpleNamespace(model_name="L36")
            self.logs: list[str] = []

        def model_root_for(self, name: str) -> Path:
            return root / "L36"

        def _write_gate(self, target: object) -> bool:
            return True

        def _log(self, message: str) -> None:
            self.logs.append(message)

        def _refresh_main_grid(self, *, reload_data: bool = False) -> None:
            return None

        def run_write(self, name: str, fn, on_done) -> None:
            on_done(fn(lambda _line: None))

    return _Host()


def _build_workspace(tmp_path: Path) -> Path:
    """L36 有一个已存在的方案；L50S 有一份可借用的主板程序。"""
    root = tmp_path / "workspace"
    model = root / "L36"
    (model / "定制" / "西班牙").mkdir(parents=True)
    (model / "定制" / "西班牙" / "方案配置.toml").write_text(
        'name = "西班牙"\n', encoding="utf-8"
    )
    return root


@pytest.fixture()
def qt_env(tmp_path: Path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget

    QApplication.instance() or QApplication([])
    root = _build_workspace(tmp_path)

    created: list[dict] = []
    borrowed: list[dict] = []
    schemes: list[dict] = []
    notices: list[str] = []

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **kwargs: notices.append(str(args[-1] if args else "")),
    )

    def fake_create_asset(*args, **kwargs):
        created.append(kwargs)
        return {"ok": True, "code": "ok", "message": "已入库", "payload": {}}

    def fake_create_scheme(*args, **kwargs):
        schemes.append({"args": args, "kwargs": kwargs})
        name = args[3] if len(args) > 3 else kwargs.get("scheme_name", "")
        target = root / "L36" / "定制" / str(name)
        target.mkdir(parents=True, exist_ok=True)
        (target / "方案配置.toml").write_text(
            f'name = "{name}"\n', encoding="utf-8"
        )
        return {"ok": True, "code": "ok", "message": "方案已创建", "payload": {}}

    def fake_register(*args, **kwargs):
        borrowed.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "code": "ok", "message": "已登记借用", "payload": {}}

    monkeypatch.setattr("fwasset.ui_qt.entry_flows.create_asset", fake_create_asset)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.create_scheme", fake_create_scheme)
    monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.register_shared_module", fake_register
    )

    host = _make_host(QWidget, root)
    return SimpleNamespace(
        host=host,
        root=root,
        created=created,
        borrowed=borrowed,
        schemes=schemes,
        notices=notices,
        monkeypatch=monkeypatch,
    )


def _widgets(dialog):
    from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QRadioButton

    return (
        {b.text(): b for b in dialog.findChildren(QPushButton)},
        {r.text(): r for r in dialog.findChildren(QRadioButton)},
        dialog.findChildren(QComboBox),
        dialog.findChildren(QLineEdit),
    )


def _combo_named(dialog, marker: str):
    """按 objectName 取下拉，避免依赖控件顺序。"""
    from PySide6.QtWidgets import QComboBox

    for combo in dialog.findChildren(QComboBox):
        if combo.objectName() == marker:
            return combo
    raise AssertionError(f"未找到下拉：{marker}")


def _scope_items(combo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def test_scope_combo_lists_common_schemes_and_new_entry(qt_env) -> None:
    """范围下拉：通用、各方案、分隔线、「+ 新建定制方案…」；默认通用。"""
    from fwasset.ui_qt.entry_flows import open_create_asset

    def drive(dialog) -> int:
        scope = _combo_named(dialog, "scope_combo")
        items = _scope_items(scope)
        assert items[0] == "通用"
        assert items[1] == "西班牙"
        assert items[-1] == "+ 新建定制方案…"
        assert scope.scheme() == ""
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_custom_scope_submits_scheme_name(qt_env) -> None:
    """scope=定制 时 create_asset 必须收到「定制」与所选方案名。"""
    from PySide6.QtWidgets import QLineEdit

    from fwasset.ui_qt.entry_flows import open_create_asset

    qt_env.monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileNames",
        lambda *a, **k: ([str(qt_env.root / "来料" / "main.bin")], ""),
    )

    def drive(dialog) -> int:
        buttons, _radios, _combos, _edits = _widgets(dialog)
        buttons["选择文件"].click()
        scope = _combo_named(dialog, "scope_combo")
        scope.setCurrentIndex(scope.findData("西班牙"))
        scope.activated.emit(scope.currentIndex())
        assert scope.scheme() == "西班牙"
        name_box = dialog.findChild(QLineEdit)
        assert name_box is not None and name_box.text() == "main"
        buttons["保存"].click()
        assert qt_env.created, "应调用 create_asset"
        assert qt_env.created[-1]["scope"] == "定制"
        assert qt_env.created[-1]["scheme_name"] == "西班牙"
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_common_scope_submits_empty_scheme_name(qt_env) -> None:
    """默认通用：与改动前一致，scheme_name 为空。"""
    from fwasset.ui_qt.entry_flows import open_create_asset

    qt_env.monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileNames",
        lambda *a, **k: ([str(qt_env.root / "来料" / "main.bin")], ""),
    )

    def drive(dialog) -> int:
        buttons, _radios, _combos, _edits = _widgets(dialog)
        buttons["选择文件"].click()
        buttons["保存"].click()
        assert qt_env.created[-1]["scope"] == "通用"
        assert qt_env.created[-1]["scheme_name"] == ""
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_create_scheme_from_dialog_selects_new_scheme(qt_env) -> None:
    """空型号也能在对话框里建出方案，建完自动选中。"""
    from PySide6.QtWidgets import QInputDialog

    from fwasset.ui_qt.entry_flows import open_create_asset

    qt_env.monkeypatch.setattr(
        QInputDialog, "getText", lambda *a, **k: ("葡萄牙", True)
    )

    def drive(dialog) -> int:
        scope = _combo_named(dialog, "scope_combo")
        new_index = scope.count() - 1
        scope.setCurrentIndex(new_index)
        scope.activated.emit(new_index)
        assert qt_env.schemes, "应调用 create_scheme"
        assert "葡萄牙" in _scope_items(scope)
        assert scope.scheme() == "葡萄牙"
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_borrow_mode_calls_register_not_create(qt_env) -> None:
    """借用模式：程序名/厂商禁用、定制置灰，提交走 register_shared_module。"""
    from fwasset.ui_qt.entry_flows import open_create_asset

    source_asset = {
        "path": str(qt_env.root / "L50S" / "通用" / "主板程序" / "量产_默认"),
        "firmware_label": "主板程序",
        "directory_name": "量产_默认",
        "model": "L50S",
    }
    qt_env.host.borrow_candidates_for = lambda module_key: [source_asset]

    def drive(dialog) -> int:
        from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit

        buttons, _radios, _combos, _edits = _widgets(dialog)
        buttons["使用其他型号的程序"].click()

        source_combo = _combo_named(dialog, "borrow_source_combo")
        assert source_combo.isEnabled()
        assert source_combo.count() == 1
        assert dialog.findChild(QComboBox, "borrow_mode_combo") is None
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert not any("更新方式" in text or "固定版本" in text for text in labels)

        # 借用不产生新程序目录，程序名/厂商无意义；借用也只支持通用
        name_box = dialog.findChild(QLineEdit)
        assert name_box is not None and not name_box.isEnabled()
        assert not _combo_named(dialog, "vendor_combo").isEnabled()
        scope = _combo_named(dialog, "scope_combo")
        assert not scope.isEnabled() and scope.scheme() == ""

        buttons["保存"].click()
        assert qt_env.borrowed, "应调用 register_shared_module"
        assert not qt_env.created, "借用不得调用 create_asset"
        assert qt_env.borrowed[-1]["kwargs"]["mode"] == "follow_asset"
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_choose_file_leaves_borrow_mode_even_if_cancelled(qt_env) -> None:
    """借用模式下点「选择文件」立即切回导入模式；取消文件对话框也不留在借用。"""
    from PySide6.QtWidgets import QLineEdit

    from fwasset.ui_qt.entry_flows import open_create_asset

    qt_env.host.borrow_candidates_for = lambda module_key: []
    qt_env.monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileNames", lambda *a, **k: ([], "")
    )

    def drive(dialog) -> int:
        buttons, radios, _combos, _edits = _widgets(dialog)
        source_combo = _combo_named(dialog, "borrow_source_combo")
        buttons["使用其他型号的程序"].click()
        assert source_combo.isEnabled()

        buttons["选择文件"].click()
        assert not source_combo.isEnabled()
        assert not source_combo.isVisibleTo(dialog)
        name_box = dialog.findChild(QLineEdit)
        assert name_box is not None and name_box.isEnabled()
        assert _combo_named(dialog, "scope_combo").isEnabled()
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_borrow_lists_all_types_and_locks_program_type(qt_env) -> None:
    """使用其他型号的程序：列出全部类型，程序类型跟随源程序且不可手改。"""
    from fwasset.ui_qt.entry_flows import open_create_asset

    asked: list[str] = []
    sources = [
        {"firmware_label": "主板程序", "directory_name": "量产_默认", "model": "L50S"},
        {"firmware_label": "蓝牙程序", "directory_name": "蓝牙_默认", "model": "L50S"},
    ]

    def finder(module_key: str) -> list[dict]:
        asked.append(module_key)
        return sources

    qt_env.host.borrow_candidates_for = finder

    def drive(dialog) -> int:
        buttons, _radios, _combos, _edits = _widgets(dialog)
        modules = _combo_named(dialog, "module_combo")
        assert modules.isEnabled()
        buttons["使用其他型号的程序"].click()
        assert asked == [""], "借用候选不应按当前程序类型过滤"
        assert not modules.isEnabled()

        source_combo = _combo_named(dialog, "borrow_source_combo")
        assert source_combo.itemText(1) == "L50S / 蓝牙程序 / 蓝牙_默认"
        source_combo.setCurrentIndex(0)
        assert modules.currentData() == "主板程序"
        source_combo.setCurrentIndex(1)
        assert modules.currentData() == "蓝牙程序"

        buttons["选择文件"].click()  # 取消文件对话框
        assert modules.isEnabled()
        return 0

    qt_env.monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileNames", lambda *a, **k: ([], "")
    )
    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)


def test_new_scheme_cancel_keeps_previous_scope(qt_env) -> None:
    """选「+ 新建定制方案…」后取消：回到原选项，不调用 create_scheme。"""
    from PySide6.QtWidgets import QInputDialog

    from fwasset.ui_qt.entry_flows import open_create_asset

    qt_env.monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", False))

    def drive(dialog) -> int:
        scope = _combo_named(dialog, "scope_combo")
        new_index = scope.count() - 1
        scope.setCurrentIndex(new_index)
        scope.activated.emit(new_index)
        assert not qt_env.schemes
        assert scope.currentText() == "通用"
        return 0

    qt_env.monkeypatch.setattr(
        "fwasset.ui_qt.entry_flows.QDialog.exec", lambda dialog: drive(dialog)
    )
    open_create_asset(qt_env.host)
