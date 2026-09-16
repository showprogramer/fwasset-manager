import pytest

from fwasset.core.services.scan_service import (
    build_cached_scan_result,
    build_scan_result,
)


def test_build_scan_result_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets",
        lambda root, last_scan_at=None, cancel_event=None: (
            [
                {
                    "firmware_type": "handcontrol_ui",
                    "model": "L36",
                    "version": "V1.0.0",
                    "label": "x",
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.save_assets", lambda assets, root: None
    )
    # folders 由 handcontrol_folders_from_assets 从 assets 派生；本 fixture 无 rom/pkg → 0 folders
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.handcontrol_folders_from_assets",
        lambda assets: [{"model": "L36", "version": "V1.0.0", "label": "x"}],
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None)

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert len(result["payload"]["assets"]) == 1
    assert len(result["payload"]["folders"]) == 1
    assert result["payload"]["errors"] == []


def test_build_scan_result_uses_full_scan_when_scan_meta_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    seen = {}

    def fake_scan(root, last_scan_at=None, cancel_event=None):
        seen["last_scan_at"] = last_scan_at
        return (
            [
                {
                    "firmware_type": "handcontrol_ui",
                    "model": "L36",
                    "version": "V1.0.0",
                    "label": "x",
                }
            ],
            [],
        )

    monkeypatch.setattr(
        "fwasset.core.services.scan_service.load_scan_meta",
        lambda: [{"root_dir": "D:/x", "last_scan_at": 123.0, "schema_version": 2}],
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets", fake_scan
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.handcontrol_folders_from_assets",
        lambda assets: [],
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.save_assets", lambda assets, root: None
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None)

    assert result["ok"] is True
    assert seen["last_scan_at"] is None


def test_build_scan_result_failed(monkeypatch: pytest.MonkeyPatch):
    def raise_scan(root, last_scan_at=None, cancel_event=None):
        raise RuntimeError("scan boom")

    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets", raise_scan
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None)

    assert result["ok"] is False
    assert result["code"] == "scan_failed"
    assert "scan boom" in result["message"]


def test_build_cached_scan_result_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("fwasset.core.services.scan_service.count_assets", lambda: 1)
    monkeypatch.setattr("fwasset.core.services.scan_service.load_scan_meta", lambda: [])

    result = build_cached_scan_result(log_fn=lambda _m: None)

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert result["payload"]["asset_count"] == 1
    assert result["payload"]["assets"] == []
    assert result["payload"]["folders"] == []
    # CSC-002：缓存分支 payload 与 build_scan_result 契约一致，warnings 恒为空列表
    assert result["payload"]["warnings"] == []


def test_build_cached_scan_result_index_empty(monkeypatch: pytest.MonkeyPatch):
    """index_empty 分支 payload 同样携带空的 warnings（缓存读取无新扫描 warning）。"""
    monkeypatch.setattr("fwasset.core.services.scan_service.count_assets", lambda: 0)
    meta = [{"root_dir": "D:/x", "last_scan_at": 123.0, "schema_version": 2}]
    monkeypatch.setattr("fwasset.core.services.scan_service.load_scan_meta", lambda: meta)

    result = build_cached_scan_result(log_fn=lambda _m: None)

    assert result["ok"] is True
    assert result["code"] == "index_empty"
    assert result["payload"]["asset_count"] == 0
    assert result["payload"]["scan_meta"] == meta
    assert result["payload"]["warnings"] == []


def test_build_cached_scan_result_maps_db_errors_to_index_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """锁库/DB 错误不得裸抛，须返回 index_unavailable ServiceResult。"""
    import sqlite3

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("fwasset.core.services.scan_service.count_assets", boom)

    result = build_cached_scan_result(log_fn=lambda _m: None)

    assert result["ok"] is False
    assert result["code"] == "index_unavailable"
    assert "locked" in result["message"]
    assert result["payload"]["scan_meta"] == []
    assert result["payload"]["warnings"] == []


def test_build_scan_result_cancelled(monkeypatch: pytest.MonkeyPatch):
    import threading

    cancel = threading.Event()
    cancel.set()

    def fake_scan(root, last_scan_at=None, cancel_event=None):
        return ([], [])

    saved = {"called": False}

    def fake_save(assets, root):
        saved["called"] = True

    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets", fake_scan
    )
    monkeypatch.setattr("fwasset.core.services.scan_service.save_assets", fake_save)

    result = build_scan_result("D:/x", log_fn=lambda _m: None, cancel_event=cancel)

    assert result["ok"] is True
    assert result["code"] == "cancelled"
    assert "取消" in result["message"]
    assert result["payload"]["assets"] == []
    assert saved["called"] is False


# ---------------------------------------------------------------------------
# TASK-20260916：payload 诊断分级（warnings / errors）与 message 文案不变
# ---------------------------------------------------------------------------


def _fake_issues_scan(issues):
    def fake_scan(root, last_scan_at=None, cancel_event=None):
        return ([], issues)

    return fake_scan


def test_build_scan_result_separates_warnings_and_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    issues = [
        {
            "severity": "warning",
            "message": "平台配置读取失败（parse_error），机芯类型留空",
            "path": "D:/x/L36/平台配置.toml",
        },
        {
            "severity": "error",
            "message": "D:/x/受保护: Permission denied",
            "path": "D:/x/受保护",
        },
    ]
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets",
        _fake_issues_scan(issues),
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.save_assets", lambda assets, root: None
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None)

    assert result["ok"] is True
    assert result["code"] == "ok"
    # message 文案不变：仍按 error 级数量提示目录读取失败
    assert result["message"] == "扫描完成（1 个目录读取失败）"
    assert result["payload"]["errors"] == ["D:/x/受保护: Permission denied"]
    assert result["payload"]["warnings"] == [
        "平台配置读取失败（parse_error），机芯类型留空"
    ]


def test_build_scan_result_cancelled_keeps_graded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
):
    import threading

    cancel = threading.Event()
    cancel.set()
    issues = [
        {"severity": "warning", "message": "平台配置读取失败（parser_missing）", "path": "p"},
        {"severity": "error", "message": "扫描已被用户取消", "path": ""},
    ]
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets",
        _fake_issues_scan(issues),
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.save_assets", lambda assets, root: None
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None, cancel_event=cancel)

    assert result["code"] == "cancelled"
    assert result["message"] == "扫描已被用户取消"
    assert result["payload"]["errors"] == ["扫描已被用户取消"]
    assert result["payload"]["warnings"] == ["平台配置读取失败（parser_missing）"]


def test_build_scan_result_warnings_only_keeps_plain_message(
    monkeypatch: pytest.MonkeyPatch,
):
    """仅 warning 级时 message 仍为「扫描完成」，不套用目录读取失败文案。"""
    issues = [
        {"severity": "warning", "message": "平台配置读取失败（parse_error）", "path": "p"}
    ]
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.scan_firmware_assets",
        _fake_issues_scan(issues),
    )
    monkeypatch.setattr(
        "fwasset.core.services.scan_service.save_assets", lambda assets, root: None
    )

    result = build_scan_result("D:/x", log_fn=lambda _m: None)

    assert result["message"] == "扫描完成"
    assert result["payload"]["errors"] == []
    assert len(result["payload"]["warnings"]) == 1
