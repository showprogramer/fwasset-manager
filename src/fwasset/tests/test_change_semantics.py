"""D1.1 改类型/改范围复合操作（子任务 6b）。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import fwasset.core.services.layout_update_service as layout_update_service
from fwasset.core.managed_paths import (
    ASSET_METADATA_FILENAME as META_NAME,
)
from fwasset.core.managed_paths import (
    RETIRED_METADATA_FILENAME,
    RETIRED_VERSIONS_DIRNAME,
)
from fwasset.core.model_config import (
    SharedModuleRef,
    load_shared_modules,
    save_shared_module,
)
from fwasset.core.platform_config import (
    PlatformDefaults,
    load_platform_config,
    save_platform_config,
)
from fwasset.core.quarantine import list_records
from fwasset.core.services.layout_update_service import (
    change_asset_semantics,
    resume_change_asset_semantics,
)
from fwasset.core.services.model_scheme_service import create_model, create_scheme
from fwasset.core.services.reference_service import build_clear_defaults_plan
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_workspace_status,
)


def _workspace(tmp_path: Path, name: str = "L36程序") -> Path:
    result = create_model(str(tmp_path), str(tmp_path), name, "单3D")
    assert result["ok"], result
    return tmp_path / name


def _variant(
    model: Path,
    module: str = "主板程序",
    name: str = "v1",
    payload: bytes = b"firmware",
    filename: str = "fw.bin",
) -> Path:
    root = model / "通用" / module / name
    root.mkdir(parents=True)
    (root / filename).write_bytes(payload)
    return root


def _source(
    tmp_path: Path,
    name: str = "来源",
    payload: bytes = b"new-firmware",
    filename: str = "fw.bin",
) -> Path:
    root = tmp_path / "外部" / name
    root.mkdir(parents=True)
    (root / filename).write_bytes(payload)
    return root


def _assert_converged_clean(workspace: Path) -> None:
    status = load_workspace_status(workspace)
    assert status.state == "clean"
    assert status.generation % 2 == 0


def test_change_type_does_not_call_update_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    called: list[str] = []
    original = layout_update_service.build_rewrite_plan

    def _spy(*args: object, **kwargs: object):
        request = args[2] if len(args) > 2 else kwargs.get("request")
        called.append(getattr(request, "operation", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(layout_update_service, "build_rewrite_plan", _spy)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex", payload=b"keys"),
        model / "通用" / "快捷键程序" / "新程序",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is True, result
    assert "update" not in called
    assert "主板程序" not in load_platform_config(model)[0].defaults
    _assert_converged_clean(tmp_path)


def test_change_type_clears_defaults_keeps_borrows(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    borrower = _workspace(tmp_path, "L50程序")
    old = _variant(model)
    save_platform_config(
        model, [PlatformDefaults("单3D", {"主板程序": "v1", "快捷键程序": "k1"})]
    )
    _variant(model, module="快捷键程序", name="k1", filename="key.hex", payload=b"k")
    save_shared_module(
        borrower,
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path="L36程序/通用/主板程序/v1",
            mode="static",
        ),
    )
    new_path = model / "通用" / "快捷键程序" / "新程序"
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex", payload=b"keys"),
        new_path,
        "change_type",
        retire_mode="retire_to_backup",
    )
    assert result["ok"] is True, result
    defaults = load_platform_config(model)[0].defaults
    assert "主板程序" not in defaults
    assert defaults["快捷键程序"] == "k1"
    refs = {item.module_key: item for item in load_shared_modules(borrower)}
    assert refs["主板程序"].source_relative_path == "L36程序/通用/主板程序/v1"
    assert refs["主板程序"].mode == "static"
    assert "backup" in result["payload"]
    assert "quarantine_id" not in result["payload"]
    _assert_converged_clean(tmp_path)


def test_change_kind_empty_plan_kinds_leave_defaults(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    scheme_a = create_scheme(str(tmp_path), tmp_path, model, "方案A")
    scheme_b = create_scheme(str(tmp_path), tmp_path, model, "方案B")
    assert scheme_a["ok"] and scheme_b["ok"]
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    before = (model / "平台配置.toml").read_bytes()

    to_custom = model / "定制" / "方案A" / "主板程序" / "定制程序"
    moved = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, name="c1"),
        to_custom,
        "general_to_custom",
        retire_mode="retire_to_trash",
    )
    assert moved["ok"] is True, moved
    assert "主板程序" not in load_platform_config(model)[0].defaults

    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    other = _variant(model, name="v2", payload=b"other")
    before = (model / "平台配置.toml").read_bytes()
    back = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        to_custom,
        _source(tmp_path, name="c2"),
        model / "通用" / "主板程序" / "回通用",
        "custom_to_general",
        retire_mode="retire_to_trash",
    )
    assert back["ok"] is True, back
    assert (model / "平台配置.toml").read_bytes() == before

    custom_a = model / "定制" / "方案A" / "主板程序" / "甲"
    custom_a.mkdir(parents=True)
    (custom_a / "fw.bin").write_bytes(b"a")
    before = (model / "平台配置.toml").read_bytes()
    scheme_move = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        custom_a,
        _source(tmp_path, name="c3"),
        model / "定制" / "方案B" / "主板程序" / "乙",
        "custom_scheme_move",
        retire_mode="retire_to_trash",
    )
    assert scheme_move["ok"] is True, scheme_move
    assert (model / "平台配置.toml").read_bytes() == before
    assert other.exists()
    _assert_converged_clean(tmp_path)


def test_multi_block_clears_exact_hits_only(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(
        model,
        [
            PlatformDefaults("单3D", {"主板程序": "v1", "快捷键程序": "k1"}),
            PlatformDefaults("双2D", {"主板程序": "v1", "语音程序": "voice"}),
        ],
    )
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        model / "通用" / "快捷键程序" / "新程序",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is True, result
    assert result["code"] != "platform_not_normalized"
    blocks = load_platform_config(model)
    assert "主板程序" not in blocks[0].defaults
    assert blocks[0].defaults["快捷键程序"] == "k1"
    assert "主板程序" not in blocks[1].defaults
    assert blocks[1].defaults["语音程序"] == "voice"
    _assert_converged_clean(tmp_path)


def test_keyword_dir_clear_defaults_on_change_type(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    old = model / "通用" / "主板" / "v1"
    old.mkdir(parents=True)
    (old / "fw.bin").write_bytes(b"firmware")
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    plan = build_clear_defaults_plan(str(tmp_path), tmp_path, old, "change_type")
    assert plan["ok"] is True, plan
    assert plan["payload"]["plan"].files
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        model / "通用" / "快捷键程序" / "新程序",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is True, result
    assert "主板程序" not in load_platform_config(model)[0].defaults


def test_change_kind_mismatch_and_normalize_required_are_clean(
    tmp_path: Path,
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    created = create_scheme(str(tmp_path), tmp_path, model, "方案A")
    assert created["ok"], created
    mismatch = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path),
        model / "定制" / "方案A" / "主板程序" / "x",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert mismatch["ok"] is False
    assert mismatch["code"] == "change_kind_mismatch"
    _assert_converged_clean(tmp_path)

    leaf = model / "通用" / "语音程序"
    leaf.mkdir(parents=True)
    (leaf / "fw.bin").write_bytes(b"leaf")
    required = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        leaf,
        _source(tmp_path, name="s2"),
        model / "通用" / "快捷键程序" / "新",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert required["ok"] is False
    assert required["code"] == "normalize_required"
    assert (leaf / "fw.bin").exists()
    _assert_converged_clean(tmp_path)


def test_incomplete_change_replaces_source_in_place(tmp_path: Path) -> None:
    """缺文件的换类型来源直接落到新路径，不再进入候选区。"""
    model = _workspace(tmp_path)
    old = _variant(model)
    source = tmp_path / "外部" / "残缺"
    source.mkdir(parents=True)
    (source / "only.rom").write_bytes(b"rom")
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        source,
        model / "通用" / "手控UI" / "残缺手控",
        "change_type",
        retire_mode="retire_to_backup",
        vendor="摩众",
    )
    assert result["ok"] is True, result
    assert result["code"] != "created_incomplete"
    landed = model / "通用" / "手控UI" / "残缺手控"
    assert (landed / "only.rom").read_bytes() == b"rom"
    assert not old.exists()
    incomplete = tmp_path / ".fwasset" / "incomplete"
    if incomplete.is_dir():
        assert [path for path in incomplete.iterdir() if path.is_dir()] == []
    _assert_converged_clean(tmp_path)


def test_unknown_file_change_replaces_source_in_place(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    source = tmp_path / "外部" / "未知"
    source.mkdir(parents=True)
    (source / "notes.txt").write_text("x", encoding="utf-8")
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        source,
        model / "通用" / "快捷键程序" / "残缺",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is True, result
    assert result["code"] != "created_incomplete"
    landed = model / "通用" / "快捷键程序" / "残缺"
    assert (landed / "notes.txt").read_text(encoding="utf-8") == "x"
    assert not old.exists()
    _assert_converged_clean(tmp_path)


def test_retire_modes_are_exclusive(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    trash = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        model / "通用" / "快捷键程序" / "trash",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert trash["ok"] is True, trash
    assert "quarantine_id" in trash["payload"]
    assert "backup" not in trash["payload"]
    kinds = [r["kind"] for r in list_records(tmp_path)]
    assert "transactional_retire" in kinds

    old2 = _variant(model, name="v2")
    backup = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old2,
        _source(tmp_path, name="s2", filename="key.hex"),
        model / "通用" / "快捷键程序" / "backup",
        "change_type",
        retire_mode="retire_to_backup",
    )
    assert backup["ok"] is True, backup
    assert "backup" in backup["payload"]
    assert "quarantine_id" not in backup["payload"]
    dest = Path(backup["payload"]["backup"])
    assert dest.parent.name == RETIRED_VERSIONS_DIRNAME
    meta = tomllib.loads((dest / RETIRED_METADATA_FILENAME).read_text(encoding="utf-8"))
    assert meta["retired_from"]
    assert meta["content_hash"]
    _assert_converged_clean(tmp_path)


def test_rollback_conflict_keeps_new_and_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    monkeypatch.setattr(
        layout_update_service,
        "apply_rewrite_plan",
        lambda *args, **kwargs: {
            "ok": False,
            "code": "rollback_conflict",
            "message": "冲突",
            "payload": {},
        },
    )
    new_path = model / "通用" / "快捷键程序" / "新程序"
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        new_path,
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is False
    assert result["code"] == "change_inconsistent"
    assert new_path.exists()
    assert old.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"
    blocked = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, name="x", filename="key.hex"),
        model / "通用" / "快捷键程序" / "另一",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert blocked["code"] == "recovery_required"


def test_retire_failed_keeps_both_and_cas_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    new_path = model / "通用" / "快捷键程序" / "新程序"

    def _fail_retire(*args: object, **kwargs: object) -> object:
        raise layout_update_service.QuarantineError("injected")

    monkeypatch.setattr(layout_update_service, "register_retire", _fail_retire)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        new_path,
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is False
    assert result["code"] == "retire_failed"
    assert old.exists() and new_path.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_retire_failed_with_config_conflict_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    new_path = model / "通用" / "快捷键程序" / "新程序"
    platform = model / "平台配置.toml"

    def _fail_and_tamper(*args: object, **kwargs: object) -> object:
        platform.write_bytes(platform.read_bytes() + b"\n# third-party\n")
        raise layout_update_service.QuarantineError("injected")

    monkeypatch.setattr(layout_update_service, "register_retire", _fail_and_tamper)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        new_path,
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is False
    assert result["code"] == "retire_failed_with_config_conflict"
    assert b"# third-party" in platform.read_bytes()
    assert old.exists() and new_path.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_change_resume_after_promote_before_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    new_path = model / "通用" / "快捷键程序" / "新程序"

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(layout_update_service, "apply_rewrite_plan", _crash)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        new_path,
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert new_path.exists() and old.exists()
    monkeypatch.undo()

    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not old.exists()
    assert new_path.exists()
    assert "主板程序" not in load_platform_config(model)[0].defaults
    _assert_converged_clean(tmp_path)


def test_change_does_not_issue_undo_token(tmp_path: Path) -> None:
    model = _workspace(tmp_path)
    old = _variant(model)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        model / "通用" / "快捷键程序" / "新程序",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is True, result
    assert "undo_token" not in result["payload"]
    assert result["payload"].get("undoable_delete") is None

# ---------------------------------------------------------------------------
# 6B-IMP 阻断项回归（2026-09-20 审查）
# ---------------------------------------------------------------------------


def test_change_resume_checks_defaults_rebuild_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6B-IMP-002：defaults 计划重建失败时续跑必须停止并保留现场。"""
    model = _workspace(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    new_path = model / "通用" / "快捷键程序" / "新程序"

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(layout_update_service, "apply_rewrite_plan", _crash)
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex"),
        new_path,
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert result["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert old.exists() and new_path.exists()
    monkeypatch.undo()

    def _fail_rebuild(*args: object, **kwargs: object) -> dict:
        return {
            "ok": False,
            "code": "reference_incomplete",
            "message": "第三方改动导致重建失败",
            "payload": {},
        }

    monkeypatch.setattr(
        layout_update_service, "build_clear_defaults_plan", _fail_rebuild
    )
    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert resumed["code"] == "reference_incomplete"
    assert old.exists() and new_path.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_change_resume_candidate_metadata_writes_missing_keys(
    tmp_path: Path,
) -> None:
    """6B-IMP-004：历史中断停在候选元数据阶段时，续跑仍补写三键。"""
    _workspace(tmp_path)
    candidate = tmp_path / ".fwasset" / "incomplete" / "deadbeef-only"
    candidate.mkdir(parents=True)
    (candidate / "only.rom").write_bytes(b"rom")

    # 手工删除元数据文件，模拟「移动后、save 前」崩溃。
    meta = candidate / META_NAME
    if meta.exists():
        meta.unlink()
    transaction = WorkspaceTransaction(tmp_path, operation="change_asset_semantics")
    transaction.__enter__()
    transaction.begin_product_write()
    transaction.set_phase(
        "candidate_metadata",
        details={
            "candidate": str(candidate),
            "candidate_vendor": "摩众",
            "candidate_intended": "handcontrol_ui",
        },
    )
    # 不 commit：模拟进程在该阶段崩溃（__exit__ 置 recovery_required）。
    transaction.__exit__(None, None, None)

    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    data = tomllib.loads((candidate / META_NAME).read_text(encoding="utf-8"))
    assert data["import_state"] == "incomplete"
    assert data["vendor"] == "摩众"
    assert data["intended_firmware_type"] == "handcontrol_ui"
    _assert_converged_clean(tmp_path)


def test_change_type_and_scope_together_lands_in_target_scheme(tmp_path: Path) -> None:
    """更新程序对话框可同时改类型与范围：服务按 change_type 处理并落到方案（TASK-20260924）。"""
    model = _workspace(tmp_path)
    assert create_scheme(str(tmp_path), str(tmp_path), model, "方案A")["ok"]
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    new_path = model / "定制" / "方案A" / "快捷键程序" / "新程序"
    result = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        old,
        _source(tmp_path, filename="key.hex", payload=b"keys"),
        new_path,
        "change_type",
        retire_mode="retire_to_backup",
    )
    assert result["ok"] is True, result
    assert (new_path / "key.hex").read_bytes() == b"keys"
    assert not old.exists()
    assert "主板程序" not in load_platform_config(model)[0].defaults
    _assert_converged_clean(tmp_path)
