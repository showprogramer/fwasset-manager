"""备用版本列表与恢复入口。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

_QT_APP = None


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    app = _QT_APP
    root = tmp_path / "工作区"
    current = root / "L36程序" / "通用" / "主板程序" / "v2"
    current.mkdir(parents=True)
    backup = current / "旧版本" / "v1-20260927"
    backup.mkdir(parents=True)
    old = current.parent / "v1"
    calls: list[Path] = []

    class Host(QWidget):
        root_dir = str(root)

        def __init__(self) -> None:
            super().__init__()
            self.grid_panel = SimpleNamespace(
                get_selected_variant=lambda: SimpleNamespace(
                    asset={"path": str(current), "directory_name": "v2"},
                    borrowed_only=False,
                )
            )
            self.refreshed = 0

        def _write_gate(self, _target: object) -> bool:
            return True

        def _log(self, _message: str) -> None:
            pass

        def _refresh_main_grid(self, **_kwargs: object) -> None:
            self.refreshed += 1

        def run_write(self, _name: str, fn, done) -> None:
            done(fn(lambda _message: None))

    def fake_list(*_args):
        return {
            "ok": True,
            "code": "ok",
            "message": "找到 1 份备用副本",
            "payload": {
                "versions": [
                    {
                        "backup_path": str(backup),
                        "name": backup.name,
                        "retired_from": str(old),
                        "retired_at": "2026-09-27T10:00:00Z",
                        "can_restore": True,
                        "reason": "",
                    }
                ]
            },
        }

    def fake_restore(_configured, _workspace, backup_path, *, log_fn):
        del log_fn
        calls.append(Path(backup_path))
        return {"ok": True, "code": "ok", "message": "已恢复", "payload": {}}

    monkeypatch.setattr("fwasset.ui_qt.entry_flows.list_retired_versions", fake_list)
    monkeypatch.setattr("fwasset.ui_qt.entry_flows.restore_retired_version", fake_restore)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_k: None)
    return SimpleNamespace(
        app=app,
        host=Host(),
        backup=backup,
        calls=calls,
        monkeypatch=monkeypatch,
    )


def test_retired_versions_dialog_lists_and_restores_selected_copy(env) -> None:
    from PySide6.QtWidgets import QDialog, QListWidget, QPushButton

    from fwasset.ui_qt.entry_flows import open_retired_versions

    def drive(dialog: QDialog) -> int:
        versions = dialog.findChild(QListWidget, "retired_versions_list")
        restore = dialog.findChild(QPushButton, "restore_retired_button")
        assert versions is not None and restore is not None
        assert versions.count() == 1
        assert "v1" in versions.item(0).text()
        versions.setCurrentRow(0)
        assert restore.isEnabled()
        restore.click()
        return 0

    env.monkeypatch.setattr(QDialog, "exec", drive)

    open_retired_versions(env.host)

    assert env.calls == [env.backup]
    assert env.host.refreshed == 1


def test_retired_versions_dialog_disables_unavailable_copy(env) -> None:
    from PySide6.QtWidgets import QDialog, QListWidget, QPushButton

    from fwasset.ui_qt.entry_flows import open_retired_versions

    def fake_list(*_args):
        return {
            "ok": True,
            "code": "ok",
            "message": "找到 1 份备用副本",
            "payload": {
                "versions": [
                    {
                        "backup_path": str(env.backup),
                        "name": env.backup.name,
                        "retired_from": "",
                        "retired_at": "",
                        "can_restore": False,
                        "reason": "退位信息无效",
                    }
                ]
            },
        }

    env.monkeypatch.setattr("fwasset.ui_qt.entry_flows.list_retired_versions", fake_list)

    def drive(dialog: QDialog) -> int:
        versions = dialog.findChild(QListWidget, "retired_versions_list")
        restore = dialog.findChild(QPushButton, "restore_retired_button")
        assert versions is not None and restore is not None
        versions.setCurrentRow(0)
        assert "退位信息无效" in versions.item(0).text()
        assert not restore.isEnabled()
        return 0

    env.monkeypatch.setattr(QDialog, "exec", drive)

    open_retired_versions(env.host)

    assert env.calls == []


def test_retired_versions_dialog_allows_retry_after_restore_error(env) -> None:
    from PySide6.QtWidgets import QDialog, QListWidget, QPushButton

    from fwasset.ui_qt.entry_flows import open_retired_versions

    def fake_restore(_configured, _workspace, _backup, *, log_fn):
        del log_fn
        return {
            "ok": False,
            "code": "content_hash_mismatch",
            "message": "备用副本内容已变化",
            "payload": {},
        }

    env.monkeypatch.setattr("fwasset.ui_qt.entry_flows.restore_retired_version", fake_restore)

    def drive(dialog: QDialog) -> int:
        versions = dialog.findChild(QListWidget, "retired_versions_list")
        restore = dialog.findChild(QPushButton, "restore_retired_button")
        assert versions is not None and restore is not None
        versions.setCurrentRow(0)
        restore.click()
        assert restore.isEnabled()
        return 0

    env.monkeypatch.setattr(QDialog, "exec", drive)

    open_retired_versions(env.host)


def test_retired_versions_dialog_can_choose_unlisted_program_directory(env) -> None:
    from PySide6.QtWidgets import QDialog, QFileDialog, QListWidget

    from fwasset.ui_qt.entry_flows import open_retired_versions

    env.host.grid_panel = SimpleNamespace(get_selected_variant=lambda: None)
    chosen: list[Path] = []

    def choose_directory(*_args):
        current = env.backup.parent.parent
        chosen.append(current)
        return str(current)

    env.monkeypatch.setattr(QFileDialog, "getExistingDirectory", choose_directory)

    def drive(dialog: QDialog) -> int:
        versions = dialog.findChild(QListWidget, "retired_versions_list")
        assert versions is not None and versions.count() == 1
        return 0

    env.monkeypatch.setattr(QDialog, "exec", drive)

    open_retired_versions(env.host)

    assert chosen == [env.backup.parent.parent]
