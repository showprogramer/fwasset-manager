"""子任务 8：可单测的界面编排与厂商名单写入。不建 Qt。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fwasset.core import settings
from fwasset.core.workspace_transaction import WorkspaceStatus
from fwasset.ui_common.asset_name_prefill import prefill_asset_name
from fwasset.ui_common.view_models.scheme_workbench_model import (
    SchemeWorkbenchModel,
)
from fwasset.ui_common.workspace_actions import (
    borrow_resubmit,
    chip_labels_from_workspace,
    delete_confirm_resubmit,
    direct_scheme_dir_names,
    effect_for_result,
    plan_program_update,
    recycle_rows,
    repair_rows,
    resume_function_name,
    run_startup_recovery,
    stage_files_as_named_dir,
    undo_bar_callable,
    version_replace_undo_target,
)


def test_prefill_uses_directory_zip_stem_and_skips_metadata() -> None:
    assert prefill_asset_name(source_kind="directory", source=Path("来料/主板A")) == "主板A"
    assert prefill_asset_name(source_kind="archive", source=Path("包/UI.ZIP")) == "UI"
    files = [Path("程序信息.toml"), Path("说明.txt"), Path("ui.rom")]
    assert prefill_asset_name(source_kind="files", files=files) == "ui"
    loose = [Path("程序信息.toml"), Path("readme.txt")]
    assert prefill_asset_name(source_kind="files", files=loose) == "readme"


def test_prefill_img_pair_uses_unzipped_folder_name() -> None:
    folder = Path("来料") / "YJ_d12x_massage_lcd_L50S_V21.07"
    files = [folder / "bootcfg.txt", folder / "d12x_mzkj_v1.0.0.img"]
    assert prefill_asset_name(source_kind="files", files=files) == folder.name


def test_startup_runs_all_steps_and_skips_staging_root(tmp_path: Path) -> None:
    staging = tmp_path / ".fwasset" / "staging"
    first = staging / "s1"
    second = staging / "s2"
    first.mkdir(parents=True)
    second.mkdir()
    (staging / "note.txt").write_text("x", encoding="utf-8")
    calls: list[tuple[str, Path]] = []

    def recover_interrupted(root: Path) -> WorkspaceStatus:
        calls.append(("interrupted", Path(root)))
        return WorkspaceStatus("recovery_required", 3, "create_model")

    def recover_on_startup(root: Path) -> list[str]:
        calls.append(("quarantine", Path(root)))
        return ["swept"]

    def cleanup(root: Path, session: Path) -> None:
        calls.append(("cleanup", Path(session)))
        if Path(session) == first:
            raise OSError("s1 failed")

    outcome = run_startup_recovery(
        tmp_path,
        recover_interrupted=recover_interrupted,
        recover_on_startup=recover_on_startup,
        cleanup=cleanup,
    )

    assert [name for name, _path in calls] == [
        "interrupted",
        "quarantine",
        "cleanup",
        "cleanup",
    ]
    assert calls[2][1] == first
    assert calls[3][1] == second
    assert staging not in [path for _name, path in calls]
    assert outcome.writes_enabled is False
    assert outcome.resume_name is None
    assert "create_model" in (outcome.banner or "")
    assert len(outcome.cleanup_errors) == 1


def test_resume_names_cover_only_four_operations() -> None:
    assert resume_function_name("normalize_module_leaf") == "resume_normalize_module_leaf"
    assert resume_function_name("update_asset") == "resume_update_asset"
    assert (
        resume_function_name("change_asset_semantics")
        == "resume_change_asset_semantics"
    )
    assert (
        resume_function_name("restore_retired_version")
        == "resume_restore_retired_version"
    )
    assert resume_function_name("create_model") is None
    assert resume_function_name("create_asset") is None
    assert resume_function_name(None) is None


def test_repair_rows_drop_normalized_and_stay_readonly() -> None:
    assert repair_rows([], []) == []
    rows = repair_rows(
        [
            (
                "已归一",
                {
                    "ok": True,
                    "code": "ok",
                    "message": "",
                    "payload": {"preview": {"already_normalized": True}},
                },
            ),
            (
                "旧型号",
                {
                    "ok": True,
                    "code": "ok",
                    "message": "",
                    "payload": {"preview": {"already_normalized": False}},
                },
            ),
            (
                "坏型号",
                {
                    "ok": False,
                    "code": "platform_config_missing",
                    "message": "没有平台配置",
                    "payload": {},
                },
            ),
        ],
        [],
    )
    assert [row["text"] for row in rows] == ["旧型号 需要归一", "没有平台配置"]
    assert all(row["executable"] is False for row in rows)

    legacy = repair_rows(
        [],
        [{"path": "D:/旧版本/a", "is_retired_versions": True}],
    )
    assert legacy[0]["text"] == "D:/旧版本/a 备用副本（有意排除）"
    assert legacy[0]["executable"] is False


def test_created_incomplete_does_not_open_a_page() -> None:
    effect = effect_for_result(
        {
            "ok": True,
            "code": "created_incomplete",
            "message": "已放入待补齐",
            "payload": {},
        },
        entry="create_asset",
    )
    assert effect.action != "open_incomplete"


def test_borrow_resubmit_keeps_the_same_token_object() -> None:
    token = {"generation": 2, "existing": {"mode": "static"}}
    original = {"mode": "static", "overwrite_token": None, "source": "asset-1"}
    again = borrow_resubmit(original, {"overwrite_token": token, "existing": {}})
    assert again["overwrite_token"] is token
    assert again["mode"] == "static"
    assert again["source"] == "asset-1"


def test_undo_bar_is_not_callable_after_five_seconds() -> None:
    assert undo_bar_callable(started_at=10.0, now=14.9) is True
    assert undo_bar_callable(started_at=10.0, now=15.0) is True
    assert undo_bar_callable(started_at=10.0, now=15.001) is False


def test_trash_retire_does_not_offer_asset_undo() -> None:
    assert (
        version_replace_undo_target(
            {
                "ok": True,
                "code": "ok",
                "message": "已换版本",
                "payload": {"quarantine_id": "q1"},
            },
            retire_mode="retire_to_trash",
        )
        is None
    )


def test_viewmodel_set_default_calls_set_asset_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_set(configured, workspace, asset, *, log_fn=print):
        calls.append((configured, workspace, asset))
        return {"ok": True, "code": "ok", "message": "已设为默认", "payload": {}}

    def old_writer(*_args, **_kwargs):
        raise AssertionError("右键不得再调用 set_module_default_for_model")

    monkeypatch.setattr(
        "fwasset.core.services.write_edit_service.set_asset_default",
        fake_set,
    )
    monkeypatch.setattr(
        "fwasset.ui_common.view_models.scheme_workbench_model._set_module_default_for_model",
        old_writer,
    )
    model = SchemeWorkbenchModel()
    model.root_dir = tmp_path
    asset = {"path": str(tmp_path / "通用" / "主板程序" / "A"), "category": "common"}

    result = model.apply_asset_default(asset)

    assert result["code"] == "ok"
    assert calls == [(tmp_path, tmp_path, asset)]


def test_viewmodel_register_borrow_uses_write_edit_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    token = {"generation": 4}

    def fake_register(
        configured,
        workspace,
        target,
        source,
        *,
        mode: str = "static",
        overwrite_token=None,
        log_fn=print,
    ):
        calls.append((mode, overwrite_token, Path(target), source))
        return {"ok": True, "code": "ok", "message": "已登记借用", "payload": {}}

    def old_writer(*_args, **_kwargs):
        raise AssertionError("右键不得再调用 set_shared_module")

    monkeypatch.setattr(
        "fwasset.core.services.write_edit_service.register_shared_module",
        fake_register,
    )
    monkeypatch.setattr(
        "fwasset.ui_common.view_models.scheme_workbench_model._set_shared_module_core",
        old_writer,
    )
    (tmp_path / "新型号").mkdir()
    model = SchemeWorkbenchModel()
    model.root_dir = tmp_path
    model._single_model_root = False
    source = {"path": str(tmp_path / "别的型号" / "通用" / "主板" / "A")}

    result = model.register_borrow(
        "新型号",
        source,
        mode="follow_asset",
        overwrite_token=token,
    )

    assert result["code"] == "ok"
    assert calls == [("follow_asset", token, tmp_path / "新型号", source)]


def test_blank_model_chip_comes_from_directory_not_assets(tmp_path: Path) -> None:
    model = tmp_path / "新型号"
    model.mkdir()
    (model / "平台配置.toml").write_text(
        '[[platform]]\nname = "单3D"\n[platform.defaults]\n',
        encoding="utf-8",
    )
    assert chip_labels_from_workspace(tmp_path) == ["新型号"]


def _point_config(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    cfg: dict,
    status: str,
    error: str = "",
) -> None:
    monkeypatch.setattr(settings, "CONFIG_PATH", path)
    monkeypatch.setattr(settings, "_cfg", cfg)
    monkeypatch.setattr(settings, "CONFIG_LOAD_STATUS", status)
    monkeypatch.setattr(settings, "CONFIG_LOAD_ERROR", error)


def test_save_vendor_preserves_keys_and_normalizes(tmp_path: Path) -> None:
    from fwasset.core.settings import save_vendor_candidates

    path = tmp_path / "config.toml"
    path.write_text(
        'vendors = ["摩众"]\n\n[paths]\nroot_dir = "D:/fw"\ntool_root = ""\n',
        encoding="utf-8",
    )
    result = save_vendor_candidates(
        [" 摩众 ", "摩众", "MZ", "mz "],
        config_path=path,
    )
    assert result["code"] == "ok"
    assert result["message"] == "厂商名单已保存"
    import tomllib

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["paths"]["root_dir"] == "D:/fw"
    assert data["vendors"] == ["摩众", "MZ"]


def test_save_vendor_corrupt_file_is_unchanged(tmp_path: Path) -> None:
    from fwasset.core.settings import save_vendor_candidates

    path = tmp_path / "config.toml"
    path.write_bytes(b"not = [valid\n")
    before = path.read_bytes()
    result = save_vendor_candidates(["国瑞"], config_path=path)
    assert result["code"] == "config_corrupt"
    assert path.read_bytes() == before


def test_save_vendor_accepts_empty_list(tmp_path: Path) -> None:
    from fwasset.core.settings import save_vendor_candidates

    path = tmp_path / "config.toml"
    path.write_text('vendors = ["摩众"]\n', encoding="utf-8")
    result = save_vendor_candidates([], config_path=path)
    assert result["ok"] is True
    import tomllib

    with path.open("rb") as handle:
        assert tomllib.load(handle)["vendors"] == []


def test_save_vendor_refreshes_process_cache_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.settings import load_vendor_candidates, save_vendor_candidates

    live = tmp_path / "live.toml"
    live.write_text(
        'vendors = ["摩众"]\n\n[paths]\nroot_dir = "D:/fw"\n',
        encoding="utf-8",
    )
    cached = {"paths": {"root_dir": "D:/fw"}, "vendors": ["摩众"]}
    _point_config(monkeypatch, live, cached, "ok")
    values = ["摩众", "新厂"]
    assert save_vendor_candidates(values)["code"] == "ok"
    values.append("后来改的")
    assert load_vendor_candidates() == ["摩众", "新厂"]
    assert cached["paths"]["root_dir"] == "D:/fw"
    assert settings.CONFIG_LOAD_STATUS == "ok"

    other = tmp_path / "other.toml"
    other.write_text('vendors = ["摩众"]\n', encoding="utf-8")
    assert save_vendor_candidates(["旁路"], config_path=other)["code"] == "ok"
    assert load_vendor_candidates() == ["摩众", "新厂"]


def test_save_vendor_ok_cache_replaces_vendors_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.settings import load_vendor_candidates, save_vendor_candidates

    live = tmp_path / "live.toml"
    live.write_text(
        'vendors = ["摩众"]\n\n[paths]\nroot_dir = "D:/on-disk"\ntool_root = "D:/disk-tool"\n',
        encoding="utf-8",
    )
    cached = {
        "paths": {"root_dir": "D:/in-memory", "tool_root": "D:/mem-tool"},
        "vendors": ["摩众"],
        "extra": "keep",
    }
    _point_config(monkeypatch, live, cached, "ok")
    assert save_vendor_candidates(["新厂"])["code"] == "ok"
    assert cached["paths"] == {
        "root_dir": "D:/in-memory",
        "tool_root": "D:/mem-tool",
    }
    assert cached["extra"] == "keep"
    assert cached["vendors"] == ["新厂"]
    assert settings.CONFIG_LOAD_STATUS == "ok"
    assert load_vendor_candidates() == ["新厂"]


def test_save_paths_ok_cache_updates_paths_and_keeps_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.settings import save_path_settings

    live = tmp_path / "live.toml"
    live.write_text(
        'vendors = ["摩众"]\n\n[paths]\nroot_dir = "D:/old"\ntool_root = ""\n',
        encoding="utf-8",
    )
    cached = {
        "paths": {"root_dir": "D:/old", "tool_root": ""},
        "vendors": ["摩众"],
        "extra": "keep",
    }
    _point_config(monkeypatch, live, cached, "ok")
    assert save_path_settings("D:/new", "D:/tools")["code"] == "ok"
    assert cached["paths"] == {"root_dir": "D:/new", "tool_root": "D:/tools"}
    assert cached["vendors"] == ["摩众"]
    assert cached["extra"] == "keep"
    assert settings.CONFIG_LOAD_STATUS == "ok"


def test_semantics_path_follows_existing_scheme(tmp_path: Path) -> None:
    model = tmp_path / "L1"
    (model / "定制" / "方案A").mkdir(parents=True)
    (model / "定制" / "方案B").mkdir()
    (model / "定制" / "单模块变体").mkdir()
    (model / "其他" / "定制" / "不该出现").mkdir(parents=True)

    assert direct_scheme_dir_names(model) == ["方案A", "方案B"]
    # 刚由 create_scheme 建出的方案只有 方案配置.toml、没有任何固件，
    # 必须立刻出现在新建程序的方案下拉里（TASK-20260923 第 4.1 节）。
    fresh = model / "定制" / "方案C"
    fresh.mkdir()
    (fresh / "方案配置.toml").write_text('name = "方案C"\n', encoding="utf-8")
    assert direct_scheme_dir_names(model) == ["方案A", "方案B", "方案C"]


def test_save_vendor_creates_missing_file_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.settings import load_vendor_candidates, save_vendor_candidates

    live = tmp_path / "missing.toml"
    _point_config(monkeypatch, live, {}, "missing")
    assert save_vendor_candidates(["新厂"])["code"] == "ok"
    assert live.is_file()
    assert load_vendor_candidates() == ["新厂"]
    assert settings.CONFIG_LOAD_STATUS == "ok"
    assert settings.CONFIG_LOAD_ERROR == ""


def test_save_paths_keeps_vendors_and_refuses_corrupt(tmp_path: Path) -> None:
    from fwasset.core.settings import save_path_settings

    path = tmp_path / "config.toml"
    path.write_text(
        'vendors = ["摩众", "国瑞"]\n\n[paths]\nroot_dir = "D:/old"\ntool_root = ""\n',
        encoding="utf-8",
    )
    result = save_path_settings("D:/new", "D:/tools", config_path=path)
    assert result["code"] == "ok"
    import tomllib

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["paths"]["root_dir"] == "D:/new"
    assert data["paths"]["tool_root"] == "D:/tools"
    assert data["vendors"] == ["摩众", "国瑞"]

    broken = tmp_path / "broken.toml"
    broken.write_bytes(b"[\n")
    before = broken.read_bytes()
    refused = save_path_settings("D:/new", "", config_path=broken)
    assert refused["code"] == "config_corrupt"
    assert broken.read_bytes() == before


# ---------------------------------------------------------------------------
# 删除程序（TASK-20260923）
# ---------------------------------------------------------------------------


def test_delete_confirmation_is_impact_dialog_not_overwrite() -> None:
    """删除的 confirmation_required 是「影响对话框」，不是借用的覆盖确认。"""
    result = {
        "ok": False,
        "code": "confirmation_required",
        "message": "存在 2 条跨型号借用命中，需确认后再删除",
        "payload": {"hits": [{"owner_root": "L50S"}], "retired_copies": 1},
    }
    assert effect_for_result(result, entry="delete_asset").action == "delete_confirm"
    # 借用入口不受影响
    assert (
        effect_for_result(result, entry="register_shared_module").action
        == "overwrite_confirm"
    )


def test_delete_confirm_resubmit_only_flips_confirm_shared() -> None:
    """确认后第二次调用只把 confirm_shared 置真，其余入参逐字不变。"""
    original = {
        "configured_root": r"D:\ws",
        "workspace_root": r"D:\ws",
        "asset_path": r"D:\ws\L36\通用\主板程序\量产_默认",
        "confirm_shared": False,
    }
    again = delete_confirm_resubmit(original)
    assert again["confirm_shared"] is True
    assert {k: v for k, v in again.items() if k != "confirm_shared"} == {
        k: v for k, v in original.items() if k != "confirm_shared"
    }
    assert original["confirm_shared"] is False, "不得就地改调用方的字典"


def test_delete_blocked_codes_are_alerts() -> None:
    """阻断码一律 alert；删除成功不再有撤销条（改为回收站）。"""
    for code in ("lookup_blocked", "stale_plan", "invalid_target"):
        blocked = {"ok": False, "code": code, "message": "", "payload": {}}
        assert effect_for_result(blocked, entry="delete_asset").action == "alert"


def test_delete_undo_bar_expires_after_five_seconds() -> None:
    assert undo_bar_callable(started_at=100.0, now=104.9) is True
    assert undo_bar_callable(started_at=100.0, now=105.0) is True
    assert undo_bar_callable(started_at=100.0, now=105.1) is False


def test_delete_flow_confirms_then_enters_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨型号命中 → 影响对话框 → 确认后 confirm_shared=True；不再弹撤销条。"""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QMessageBox

    from fwasset.ui_qt import entry_flows

    QApplication.instance() or QApplication([])
    calls: list[bool] = []
    bars: list[tuple[str, str]] = []

    def fake_delete(configured, workspace, path, *, confirm_shared, log_fn=print):
        calls.append(confirm_shared)
        if not confirm_shared:
            return {
                "ok": False,
                "code": "confirmation_required",
                "message": "存在 1 条跨型号借用命中，需确认后再删除",
                "payload": {
                    "hits": [{"owner_root": "L50S", "kind": "borrow"}],
                    "retired_copies": 2,
                },
            }
        return {
            "ok": True,
            "code": "ok",
            "message": "「量产_默认」已删除，可在 5 秒内撤销",
            "payload": {"quarantine_record_id": "q-9"},
        }

    monkeypatch.setattr(entry_flows, "delete_asset", fake_delete)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    host = SimpleNamespace(
        root_dir=r"D:\ws",
        _write_gate=lambda _p: True,
        _log=lambda _m: None,
        _refresh_main_grid=lambda **_k: None,
        show_undo_bar=lambda text, token, caller: bars.append((text, token)),
        run_write=lambda name, fn, on_done: on_done(fn(lambda _l: None)),
        apply_recovery_hold=lambda *a, **k: None,
    )

    entry_flows.open_delete_asset(
        host, {"path": r"D:\ws\L36\通用\主板程序\量产_默认", "directory_name": "量产_默认"}
    )

    assert calls == [False, True], "先试删，确认后才 confirm_shared=True"
    assert bars == [], "删除进回收站，不再有 5 秒撤销条"


def test_delete_flow_cancel_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """影响对话框取消：不再调用服务，也不出撤销条。"""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QMessageBox

    from fwasset.ui_qt import entry_flows

    QApplication.instance() or QApplication([])
    calls: list[bool] = []
    bars: list[str] = []

    def fake_delete(configured, workspace, path, *, confirm_shared, log_fn=print):
        calls.append(confirm_shared)
        return {
            "ok": False,
            "code": "confirmation_required",
            "message": "存在 1 条跨型号借用命中",
            "payload": {"hits": [{"owner_root": "L50S"}], "retired_copies": 0},
        }

    monkeypatch.setattr(entry_flows, "delete_asset", fake_delete)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )

    host = SimpleNamespace(
        root_dir=r"D:\ws",
        _write_gate=lambda _p: True,
        _log=lambda _m: None,
        _refresh_main_grid=lambda **_k: None,
        show_undo_bar=lambda text, token, caller: bars.append(token),
        run_write=lambda name, fn, on_done: on_done(fn(lambda _l: None)),
        apply_recovery_hold=lambda *a, **k: None,
    )

    entry_flows.open_delete_asset(
        host, {"path": r"D:\ws\L36\通用\主板程序\量产_默认", "directory_name": "量产_默认"}
    )

    assert calls == [False], "取消后不得再调用服务"
    assert bars == []


def test_reload_after_write_drops_deleted_row_without_restart(tmp_path: Path) -> None:
    """写操作后重载缓存：删掉的程序立刻从网格消失，不必重启应用。"""
    from fwasset.core.asset_index import save_assets
    from fwasset.core.file_scan import scan_firmware_assets

    root = tmp_path / "L36程序"

    def w(path: Path, text: str = "x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    w(root / "平台配置.toml", '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n')
    w(root / "通用" / "主板程序" / "量产_默认" / "a.bin")
    w(root / "通用" / "主板程序" / "防夹功能" / "b.bin")

    db = tmp_path / "index.db"
    assets, _ = scan_firmware_assets(str(root))
    save_assets(assets, str(root), path=db)

    model = SchemeWorkbenchModel()
    model.bind(db, root, root)
    assert len(model.get_common_modules("L36", "主板程序")) == 2

    # 模拟一次删除：磁盘移走 + 索引重建（服务内部就是这么做的）
    import shutil

    shutil.rmtree(root / "通用" / "主板程序" / "防夹功能")
    assets, _ = scan_firmware_assets(str(root))
    save_assets(assets, str(root), path=db)

    # 不重载时仍是旧数据（这正是「要重启才正确」的根因）
    assert len(model.get_common_modules("L36", "主板程序")) == 2

    model.reload()
    names = [
        str(c.asset.get("directory_name", ""))
        for c in model.get_common_modules("L36", "主板程序")
    ]
    assert names == ["量产_默认"]


def test_recycle_rows_only_list_restorable_deletes() -> None:
    """回收站只列可还原的删除项：退位记录与已送出的不出现。"""
    now = 1000.0
    records = [
        {
            "id": "a",
            "kind": "undoable_delete",
            "status": "pending",
            "original_path": r"D:\ws\L36\通用\主板程序\量产_默认",
            "created_at": now - 600.0,
            "expires_at": now + 3000.0,
        },
        {
            "id": "b",
            "kind": "transactional_retire",
            "status": "committed",
            "original_path": r"D:\ws\L36\通用\主板程序\旧版",
            "created_at": now - 10.0,
            "expires_at": 0.0,
        },
        {
            "id": "c",
            "kind": "undoable_delete",
            "status": "send_failed",
            "original_path": r"D:\ws\L36\通用\腿部程序\旧",
            "created_at": now - 20.0,
            "expires_at": now - 5.0,
        },
    ]
    rows = recycle_rows(records, now=now)
    assert [row["record_id"] for row in rows] == ["a"]
    assert rows[0]["name"] == "量产_默认"
    assert rows[0]["original_path"].endswith("量产_默认")
    assert "50 分钟" in rows[0]["remaining"]

    # 到期但尚未清理的条目仍可还原，并明确标注
    overdue = [{**records[0], "expires_at": now - 1.0}]
    assert recycle_rows(overdue, now=now)[0]["remaining"] == "已到期，下次启动时清理"
    assert recycle_rows([], now=now) == []


def test_startup_sweeps_recycle_bin_after_staging_cleanup(tmp_path: Path) -> None:
    """启动第 4 步清理到期回收站条目；异常不阻断启动（TASK-20260923）。"""
    staging = tmp_path / ".fwasset" / "staging"
    (staging / "s1").mkdir(parents=True)
    order: list[str] = []

    def recover_interrupted(root: Path) -> WorkspaceStatus:
        order.append("interrupted")
        return WorkspaceStatus("clean", 1, None)

    def recover_on_startup(root: Path) -> list[str]:
        order.append("quarantine")
        return []

    def cleanup(root: Path, session: Path) -> None:
        order.append("cleanup")

    def sweep(root: Path) -> list[dict]:
        order.append("sweep")
        return [{"id": "q-1"}]

    outcome = run_startup_recovery(
        tmp_path,
        recover_interrupted=recover_interrupted,
        recover_on_startup=recover_on_startup,
        cleanup=cleanup,
        sweep=sweep,
    )
    assert order == ["interrupted", "quarantine", "cleanup", "sweep"]
    assert outcome.swept == 1
    assert outcome.writes_enabled is True

    def boom(root: Path) -> list[dict]:
        raise OSError("sweep failed")

    failed = run_startup_recovery(
        tmp_path,
        recover_interrupted=recover_interrupted,
        recover_on_startup=recover_on_startup,
        cleanup=cleanup,
        sweep=boom,
    )
    assert failed.writes_enabled is True, "清理失败不得阻断启动"
    assert failed.swept == 0


def test_plan_program_update_derives_kind_and_path(tmp_path: Path) -> None:
    """更新程序：按改了哪几项派生操作（TASK-20260924 §4）。"""
    model = tmp_path / "L36"
    common_old = model / "通用" / "主板程序" / "V1"
    custom_old = model / "定制" / "方案A" / "主板程序" / "V1"

    plan = plan_program_update(model, common_old, "主板程序", "", "主板程序", "", "V2")
    assert (plan.kind, plan.new_path) == ("update", common_old.parent / "V2")

    plan = plan_program_update(model, common_old, "主板程序", "", "蓝牙程序", "", "V1")
    assert (plan.kind, plan.new_path) == ("change_type", model / "通用" / "蓝牙程序" / "V1")

    # 定制程序改类型留在目标方案里，不再被挪到通用
    plan = plan_program_update(
        model, custom_old, "主板程序", "方案A", "蓝牙程序", "方案A", "V1"
    )
    assert plan.new_path == model / "定制" / "方案A" / "蓝牙程序" / "V1"
    assert plan.kind == "change_type"

    plan = plan_program_update(
        model, common_old, "主板程序", "", "主板程序", "方案A", "V1"
    )
    assert (plan.kind, plan.new_path) == (
        "general_to_custom",
        model / "定制" / "方案A" / "主板程序" / "V1",
    )
    plan = plan_program_update(
        model, custom_old, "主板程序", "方案A", "主板程序", "", "V1"
    )
    assert (plan.kind, plan.new_path) == (
        "custom_to_general",
        model / "通用" / "主板程序" / "V1",
    )
    plan = plan_program_update(
        model, custom_old, "主板程序", "方案A", "主板程序", "方案B", "V1"
    )
    assert (plan.kind, plan.new_path) == (
        "custom_scheme_move",
        model / "定制" / "方案B" / "主板程序" / "V1",
    )


def test_plan_program_update_rejects_bad_names(tmp_path: Path) -> None:
    model = tmp_path / "L36"
    old = model / "通用" / "主板程序" / "V1"
    for name, fragment in (
        ("  ", "请填写程序名称"),
        ("a/b", "斜杠"),
        ("a\\b", "斜杠"),
        ("v1", "不能和旧程序相同"),
    ):
        plan = plan_program_update(model, old, "主板程序", "", "主板程序", "", name)
        assert plan.kind == "" and plan.new_path is None
        assert fragment in plan.error
    # 改了类型时沿用旧名合法
    assert plan_program_update(
        model, old, "主板程序", "", "蓝牙程序", "", "V1"
    ).kind == "change_type"


def test_plan_program_update_requires_normalized_asset_path(tmp_path: Path) -> None:
    model = tmp_path / "L36"
    leaf = model / "通用" / "主板程序"
    plan = plan_program_update(model, leaf, "主板程序", "", "主板程序", "", "新名称")
    assert plan.kind == ""
    assert "布局归一" in plan.error


def test_stage_files_as_named_dir_copies_into_named_folder(tmp_path: Path) -> None:
    import shutil

    first = tmp_path / "main.bin"
    second = tmp_path / "main.hex"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    temp_root, source = stage_files_as_named_dir([str(first), str(second)], "V2")
    try:
        assert source.name == "V2"
        assert sorted(p.name for p in source.iterdir()) == ["main.bin", "main.hex"]
        assert tmp_path not in temp_root.parents
    finally:
        shutil.rmtree(temp_root)
    assert not temp_root.exists()


def test_stage_files_as_named_dir_cleans_up_on_failure(tmp_path: Path) -> None:
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("fwasset-update-*"))
    with pytest.raises(OSError):
        stage_files_as_named_dir([str(tmp_path / "missing.bin")], "V2")
    after = set(Path(tempfile.gettempdir()).glob("fwasset-update-*"))
    assert after == before
