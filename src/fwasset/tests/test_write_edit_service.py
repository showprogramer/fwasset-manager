"""D1.6–D1.8 / D10.1a 受管写入口。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import fwasset.core.services.write_edit_service as write_edit_service
from fwasset.core.asset_index import AssetIndexError
from fwasset.core.file_scan import scan_firmware_subtree
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
from fwasset.core.services.model_scheme_service import create_model
from fwasset.core.services.write_edit_service import (
    clear_shared_module,
    register_shared_module,
    set_asset_default,
    undo_clear_shared_module,
    update_asset_vendor,
)
from fwasset.core.types import FirmwareAsset, ServiceResult
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_workspace_status,
)


def _model(workspace: Path, name: str) -> Path:
    result = create_model(str(workspace), str(workspace), name, "单3D")
    assert result["ok"], result
    return workspace / name


def _asset(model_root: Path, name: str = "程序A") -> Path:
    root = model_root / "通用" / "主板" / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(b"firmware")
    return root


def _shortcut_asset(model_root: Path, name: str = "程序A") -> Path:
    root = model_root / "通用" / "快捷键" / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(b"firmware")
    return root


def _scanned_asset(workspace: Path, asset_root: Path) -> FirmwareAsset:
    assets, issues = scan_firmware_subtree(str(workspace), str(asset_root))
    assert not issues
    assert len(assets) == 1
    return assets[0]


def _assert_converged_clean(workspace: Path) -> None:
    status = load_workspace_status(workspace)
    assert status.state == "clean"
    assert status.generation % 2 == 0


def test_update_vendor_same_value_is_zero_write(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    (asset_root / "程序信息.toml").write_text('vendor = "摩众"\n', encoding="utf-8")
    generation_before = load_workspace_status(tmp_path).generation

    result = update_asset_vendor(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root), "摩众"
    )

    assert result["ok"] is True
    assert result["code"] == "unchanged"
    assert load_workspace_status(tmp_path).generation == generation_before
    assert (asset_root / "程序信息.toml").read_text(encoding="utf-8") == 'vendor = "摩众"\n'


def test_set_asset_default_rejects_legacy_single_block(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    save_platform_config(model, [PlatformDefaults("默认", {})])
    generation_before = load_workspace_status(tmp_path).generation

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is False
    assert result["code"] == "platform_not_normalized"
    assert load_workspace_status(tmp_path).generation == generation_before


def test_set_asset_default_uses_asset_directory_name(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model, "程序A")
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "旧程序"})])

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is True, result
    assert load_platform_config(model)[0].defaults["主板程序"] == "程序A"


def test_register_shared_module_rejects_self_reference(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)

    result = register_shared_module(
        str(tmp_path), str(tmp_path), model, _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is False
    assert result["code"] == "self_reference"
    assert load_shared_modules(model) == []


def test_set_asset_default_reports_conflicting_canonical_aliases(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _shortcut_asset(model)
    save_platform_config(
        model,
        [PlatformDefaults("单3D", {"快捷键": "旧程序A", "快捷按键": "旧程序B"})],
    )

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is False
    assert result["code"] == "canonical_conflict"
    assert set(result["payload"]["keys"]) == {"快捷键", "快捷按键"}


def test_set_asset_default_reports_duplicate_canonical_aliases(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _shortcut_asset(model)
    save_platform_config(
        model,
        [PlatformDefaults("单3D", {"快捷键": "旧程序", "快捷按键": "旧程序"})],
    )

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is False
    assert result["code"] == "canonical_duplicate"
    assert result["payload"]["values"] == ["旧程序"]


def test_register_shared_module_reports_missing_target_model(tmp_path: Path) -> None:
    source_model = _model(tmp_path, "来源型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))

    result = register_shared_module(
        str(tmp_path), str(tmp_path), tmp_path / "不存在型号", source_asset
    )

    assert result["ok"] is False
    assert result["code"] == "target_model_missing"


def test_register_shared_module_rejects_confirmation_after_generation_changes(
    tmp_path: Path,
) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    first = register_shared_module(str(tmp_path), str(tmp_path), target_model, source_asset)
    assert first["ok"] is True, first

    preview = register_shared_module(
        str(tmp_path), str(tmp_path), target_model, source_asset
    )
    assert preview["code"] == "confirmation_required"
    with WorkspaceTransaction(tmp_path, operation="unrelated_write") as transaction:
        transaction.begin_product_write()
        transaction.commit()

    result = register_shared_module(
        str(tmp_path),
        str(tmp_path),
        target_model,
        source_asset,
        overwrite_token=preview["payload"]["overwrite_token"],
    )

    assert result["ok"] is False
    assert result["code"] == "stale_plan"


def test_register_shared_module_checks_no_other_model_at_service_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    monkeypatch.setattr(write_edit_service, "enumerate_model_roots", lambda _ws: [target_model])

    result = register_shared_module(str(tmp_path), str(tmp_path), target_model, source_asset)

    assert result["ok"] is False
    assert result["code"] == "no_other_model"


def test_clear_shared_module_undo_rejects_changed_postimage(tmp_path: Path) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    assert register_shared_module(str(tmp_path), str(tmp_path), target_model, source_asset)["ok"]

    cleared = write_edit_service.clear_shared_module(
        str(tmp_path), str(tmp_path), target_model, "主板程序"
    )
    assert cleared["ok"] is True, cleared
    (target_model / "型号配置.toml").write_text('model_id = "changed"\n', encoding="utf-8")

    result = write_edit_service.undo_clear_shared_module(
        str(tmp_path), str(tmp_path), cleared["payload"]["undo_token"]
    )

    assert result["ok"] is False
    assert result["code"] == "undo_conflict"
    _assert_converged_clean(tmp_path)


def test_update_vendor_keeps_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    (asset_root / "程序信息.toml").write_text(
        'vendor = "摩众"\nextra = "keep"\n', encoding="utf-8"
    )
    monkeypatch.setattr(write_edit_service, "replace_asset", lambda *a, **k: None)

    result = update_asset_vendor(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root), "新厂商"
    )

    assert result["ok"] is True, result
    text = (asset_root / "程序信息.toml").read_text(encoding="utf-8")
    assert 'vendor = "新厂商"' in text
    assert "extra" in text
    _assert_converged_clean(tmp_path)


def test_update_vendor_corrupt_metadata_keeps_file(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    original = "vendor = [unterminated\n"
    (asset_root / "程序信息.toml").write_text(original, encoding="utf-8")

    result = update_asset_vendor(
        str(tmp_path),
        str(tmp_path),
        cast(FirmwareAsset, {"path": str(asset_root)}),
        "新厂商",
    )

    assert result["ok"] is False
    assert result["code"] == "metadata_corrupt"
    assert (asset_root / "程序信息.toml").read_text(encoding="utf-8") == original
    _assert_converged_clean(tmp_path)


def test_update_vendor_index_failure_keeps_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssetIndexError("索引写入失败")

    monkeypatch.setattr(write_edit_service, "replace_asset", _boom)
    result = update_asset_vendor(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root), "新厂商"
    )

    assert result["ok"] is False
    assert result["code"] == "index_update_failed"
    assert result["payload"].get("detail")
    assert "新厂商" in (asset_root / "程序信息.toml").read_text(encoding="utf-8")
    _assert_converged_clean(tmp_path)


def test_update_vendor_rescan_failure_keeps_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    original = write_edit_service._asset_from_disk
    calls = {"n": 0}

    def _fail_after_write(
        workspace_root: Path, supplied: FirmwareAsset
    ) -> tuple[FirmwareAsset | None, ServiceResult | None]:
        calls["n"] += 1
        if calls["n"] >= 3:
            return None, {
                "ok": False,
                "code": "scan_failed",
                "message": "扫描失败",
                "payload": {"issues": [{"severity": "error"}]},
            }
        return original(workspace_root, supplied)

    monkeypatch.setattr(write_edit_service, "_asset_from_disk", _fail_after_write)
    result = update_asset_vendor(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root), "新厂商"
    )

    assert result["ok"] is False
    assert result["code"] == "rescan_failed"
    assert "新厂商" in (asset_root / "程序信息.toml").read_text(encoding="utf-8")
    _assert_converged_clean(tmp_path)


def test_set_asset_default_rejects_residual_follow_default(tmp_path: Path) -> None:
    source = _model(tmp_path, "L36程序")
    borrower = _model(tmp_path, "L50程序")
    asset_root = source / "通用" / "主板程序" / "程序A"
    asset_root.mkdir(parents=True)
    (asset_root / "fw.bin").write_bytes(b"firmware")
    save_platform_config(source, [PlatformDefaults("单3D", {"主板程序": "程序A"})])
    save_shared_module(
        borrower,
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path="L36程序/通用/主板程序",
            mode="follow_default",
        ),
    )

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is False
    assert result["code"] == "follow_default_migration_required"
    _assert_converged_clean(tmp_path)


def test_set_asset_default_writes_only_canonical_key(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36程序")
    asset_root = _shortcut_asset(model, "程序A")
    save_platform_config(model, [PlatformDefaults("单3D", {"快捷键": "旧程序"})])

    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )

    assert result["ok"] is True, result
    defaults = load_platform_config(model)[0].defaults
    assert defaults.get("快捷键程序") == "程序A"
    assert "快捷键" not in defaults
    _assert_converged_clean(tmp_path)


def test_register_shared_module_overwrite_requires_then_accepts_token(
    tmp_path: Path,
) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    first = _scanned_asset(tmp_path, _asset(source_model, "程序A"))
    second = _scanned_asset(tmp_path, _asset(source_model, "程序B"))
    created = register_shared_module(str(tmp_path), str(tmp_path), target_model, first)
    assert created["ok"] is True, created

    preview = register_shared_module(str(tmp_path), str(tmp_path), target_model, second)
    assert preview["ok"] is False
    assert preview["code"] == "confirmation_required"

    confirmed = register_shared_module(
        str(tmp_path),
        str(tmp_path),
        target_model,
        second,
        overwrite_token=preview["payload"]["overwrite_token"],
    )
    assert confirmed["ok"] is True, confirmed
    refs = {item.module_key: item for item in load_shared_modules(target_model)}
    assert "程序B" in refs["主板程序"].source_relative_path
    _assert_converged_clean(tmp_path)


def test_register_shared_module_rejects_retired_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    monkeypatch.setattr(
        write_edit_service,
        "managed_path_reason",
        lambda *args, **kwargs: "retired_versions",
    )

    result = register_shared_module(
        str(tmp_path), str(tmp_path), target_model, source_asset
    )

    assert result["ok"] is False
    assert result["code"] == "retired_anchor"
    _assert_converged_clean(tmp_path)


def test_clear_shared_module_undo_restores_entry(tmp_path: Path) -> None:
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    assert register_shared_module(str(tmp_path), str(tmp_path), target_model, source_asset)[
        "ok"
    ]
    cleared = clear_shared_module(str(tmp_path), str(tmp_path), target_model, "主板程序")
    assert cleared["ok"] is True, cleared
    assert load_shared_modules(target_model) == []

    restored = undo_clear_shared_module(
        str(tmp_path), str(tmp_path), cleared["payload"]["undo_token"]
    )
    assert restored["ok"] is True, restored
    assert [item.module_key for item in load_shared_modules(target_model)] == ["主板程序"]
    _assert_converged_clean(tmp_path)


def test_register_shared_module_revalidates_source_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WES-007：锁外校验后来源消失，锁内必须拒绝，不能写成悬空借用。"""
    import shutil

    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    asset_root = _asset(source_model)
    source_asset = _scanned_asset(tmp_path, asset_root)
    original = write_edit_service._asset_from_disk

    def _drop_source_after_preview(
        workspace_root: Path, supplied: FirmwareAsset
    ) -> tuple[FirmwareAsset | None, ServiceResult | None]:
        found, err = original(workspace_root, supplied)
        if found is not None and asset_root.exists():
            shutil.rmtree(asset_root)
        return found, err

    monkeypatch.setattr(write_edit_service, "_asset_from_disk", _drop_source_after_preview)
    result = register_shared_module(
        str(tmp_path), str(tmp_path), target_model, source_asset
    )
    assert result["ok"] is False
    assert result["code"] == "invalid_asset"
    assert load_shared_modules(target_model) == []
    _assert_converged_clean(tmp_path)


def test_set_asset_default_lock_inner_reference_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WES-008：锁内反查出现阻断 issue 时不得写默认。"""
    from fwasset.core.reference_lookup import LookupIssue, ReferenceLookupResult

    model = _model(tmp_path, "L36程序")
    asset_root = _asset(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "旧程序"})])
    original = write_edit_service.find_references_to
    calls = {"n": 0}

    def _blocking_on_lock(*args: object, **kwargs: object) -> ServiceResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return original(*args, **kwargs)
        issue = LookupIssue(
            category="model_config_parse_error",
            config_path="x",
            owner_root="y",
            detail="锁内配置损坏",
        )
        return {
            "ok": True,
            "code": "ok",
            "message": "",
            "payload": {"result": ReferenceLookupResult(hits=[], issues=[issue])},
        }

    monkeypatch.setattr(write_edit_service, "find_references_to", _blocking_on_lock)
    result = set_asset_default(
        str(tmp_path), str(tmp_path), _scanned_asset(tmp_path, asset_root)
    )
    assert result["ok"] is False
    assert result["code"] == "reference_incomplete"
    assert load_platform_config(model)[0].defaults["主板程序"] == "旧程序"
    _assert_converged_clean(tmp_path)


def test_register_shared_module_cas_uses_same_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WES-009：解析与 CAS 必须基于同一份原始字节，不能覆盖并发写入。"""
    source_model = _model(tmp_path, "来源型号")
    target_model = _model(tmp_path, "目标型号")
    source_asset = _scanned_asset(tmp_path, _asset(source_model))
    config_path = target_model / "型号配置.toml"
    original_bytes = write_edit_service._bytes
    injected = {"done": False}

    def _inject_comment(path: Path) -> bytes:
        data = original_bytes(path)
        if path == config_path and data and not injected["done"]:
            injected["done"] = True
            text = data.decode("utf-8") + "\nextra_marker = true\n"
            path.write_text(text, encoding="utf-8")
            return path.read_bytes()
        return data

    monkeypatch.setattr(write_edit_service, "_bytes", _inject_comment)
    result = register_shared_module(
        str(tmp_path), str(tmp_path), target_model, source_asset
    )
    assert result["ok"] is True, result
    assert "extra_marker" in config_path.read_text(encoding="utf-8")
    _assert_converged_clean(tmp_path)


def test_clear_shared_module_missing_entry_is_unchanged(tmp_path: Path) -> None:
    model = _model(tmp_path, "目标型号")
    result = clear_shared_module(str(tmp_path), str(tmp_path), model, "主板程序")
    assert result["ok"] is True
    assert result["code"] == "unchanged"
    _assert_converged_clean(tmp_path)
