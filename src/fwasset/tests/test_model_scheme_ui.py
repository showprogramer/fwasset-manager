"""型号与方案工作台入口。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session")
def qt_app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    app.processEvents()


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qt_app):
    from PySide6.QtWidgets import QMessageBox, QWidget

    from fwasset.ui_qt import entry_flows

    root = tmp_path / "workspace"
    model = root / "L36"
    (model / "定制" / "空方案").mkdir(parents=True)
    (model / "定制" / "空方案" / "方案配置.toml").write_text(
        'name = "空方案"\n', encoding="utf-8"
    )
    calls: list[tuple[str, tuple, dict]] = []
    refreshed: list[str | None] = []
    questions: list[str] = []

    def service(name: str):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name.startswith("delete") and not kwargs["confirm_shared"]:
                return {
                    "ok": False,
                    "code": "confirmation_required",
                    "message": "存在跨型号借用",
                    "payload": {"hits": [{"owner_root": "L50"}]},
                }
            return {
                "ok": True,
                "code": "ok",
                "message": "操作成功",
                "payload": {"quarantine_record_id": "q1"},
            }

        return run

    for name in ("create_scheme", "rename_model", "rename_scheme", "delete_model", "delete_scheme"):
        monkeypatch.setattr(entry_flows, name, service(name))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: questions.append(str(a[-1])) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)

    class Host(QWidget):
        def __init__(self):
            super().__init__()
            self.root_dir = str(root)
            self.current_selection = SimpleNamespace(model_name="L36", scheme_name="空方案", node_type="custom_scheme")

        def model_root_for(self, name):
            return root / name

        def _write_gate(self, _path):
            return True

        def _log(self, _message):
            pass

        def refresh_model_chips(self, select=None):
            refreshed.append(select)

        def _refresh_main_grid(self, *, reload_data=False):
            refreshed.append("grid" if reload_data else "plain")

        def run_write(self, _name, fn, done):
            done(fn(lambda _line: None))

        def apply_recovery_hold(self, *_args, **_kwargs):
            pass

    host = Host()
    yield SimpleNamespace(host=host, root=root, model=model, calls=calls, refreshed=refreshed, questions=questions, monkeypatch=monkeypatch)
    host.close()
    host.deleteLater()
    qt_app.processEvents()


def test_empty_scheme_is_visible_in_sidebar(tmp_path: Path) -> None:
    from fwasset.ui_common.view_models.scheme_workbench_model import (
        SchemeWorkbenchModel,
    )

    root = tmp_path / "workspace"
    model = root / "L36"
    scheme = model / "定制" / "空方案"
    scheme.mkdir(parents=True)
    (scheme / "方案配置.toml").write_text('name = "空方案"\n', encoding="utf-8")
    vm = SchemeWorkbenchModel()
    vm.bind(None, root, root)
    assert "空方案" in vm.build_sidebar_tree("L36")["custom"]


def test_rename_model_uses_selected_model_and_refreshes(ui) -> None:
    from PySide6.QtWidgets import QInputDialog

    from fwasset.ui_qt.entry_flows import open_rename_model

    ui.monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("L39", True))
    open_rename_model(ui.host)
    assert ui.calls[0][0] == "rename_model"
    assert ui.calls[0][1][:4] == (str(ui.root), str(ui.root), ui.model, "L39")
    assert ui.refreshed == ["L39"]


def test_create_and_rename_scheme_use_existing_model(ui) -> None:
    from PySide6.QtWidgets import QInputDialog

    from fwasset.ui_qt.entry_flows import open_create_scheme, open_rename_scheme

    ui.monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("新方案", True))
    open_create_scheme(ui.host)
    open_rename_scheme(ui.host)
    assert ui.calls[0][0:2] == ("create_scheme", (str(ui.root), str(ui.root), ui.model, "新方案"))
    assert ui.calls[1][0:2] == ("rename_scheme", (str(ui.root), str(ui.root), ui.model / "定制" / "空方案", "新方案"))
    assert ui.refreshed == ["grid", "grid"]


def test_scope_combo_scheme_creation_refreshes_sidebar(ui) -> None:
    from fwasset.ui_qt.entry_flows import _scheme_creator

    finished: list[bool] = []
    _scheme_creator(ui.host, ui.model)("新方案", finished.append)
    assert finished == [True]
    assert ui.refreshed == ["grid"]


@pytest.mark.parametrize("kind", ["model", "scheme"])
def test_delete_confirms_cross_owner_hits_then_refreshes(ui, kind: str) -> None:
    from fwasset.ui_qt.entry_flows import open_delete_model, open_delete_scheme

    (open_delete_model if kind == "model" else open_delete_scheme)(ui.host)
    assert [call[2]["confirm_shared"] for call in ui.calls] == [False, True]
    assert ui.questions
    assert ui.refreshed == ([None] if kind == "model" else ["grid"])


@pytest.mark.parametrize(
    ("relative", "expected"),
    [("L36", "container"), ("L36/定制/空方案", "container"), ("L36/通用/主板/程序", "asset")],
)
def test_recycle_restores_container_with_its_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qt_app, relative: str, expected: str) -> None:
    from fwasset.ui_qt import recycle_interface

    calls: list[str] = []

    def fake_container(*_args):
        calls.append("container")
        return {"ok": True, "code": "ok", "message": "已还原", "payload": {}}

    def fake_asset(*_args):
        calls.append("asset")
        return {"ok": True, "code": "ok", "message": "已还原", "payload": {}}

    monkeypatch.setattr(recycle_interface, "undo_model_scheme_delete", fake_container)
    monkeypatch.setattr(recycle_interface, "undo_asset_delete", fake_asset)
    widget = recycle_interface.RecycleInterface()
    widget._root = str(tmp_path)
    widget._host = SimpleNamespace(
        run_write=lambda _name, fn, done: done(fn(lambda _line: None)),
        _log=lambda _message: None,
        _refresh_main_grid=lambda **_kwargs: None,
        refresh_model_chips=lambda: None,
    )
    widget._rows = [{"record_id": "q1", "name": "目标", "original_path": str(tmp_path / relative)}]
    widget.rows.addItem("目标")
    widget.rows.setCurrentRow(0)
    monkeypatch.setattr(widget, "reload", lambda: None)
    widget._restore_selected()
    assert calls == [expected]
    widget.close()
    widget.deleteLater()
    qt_app.processEvents()
