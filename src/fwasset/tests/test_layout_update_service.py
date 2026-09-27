"""D0.3 legacy 布局归一 / D1.4a 普通 update 事务（子任务 6a）。"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

import fwasset.core.services.layout_update_service as layout_update_service
from fwasset.core.managed_paths import (
    RETIRED_METADATA_FILENAME,
    RETIRED_VERSIONS_DIRNAME,
)
from fwasset.core.model_config import SharedModuleRef, save_shared_module
from fwasset.core.platform_config import (
    PlatformDefaults,
    load_platform_config,
    save_platform_config,
)
from fwasset.core.quarantine import list_records
from fwasset.core.services.layout_update_service import (
    normalize_module_leaf,
    update_asset,
)
from fwasset.core.services.model_scheme_service import create_model
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_operation_log,
    load_workspace_status,
)


def _model(workspace: Path, name: str = "L36程序") -> Path:
    result = create_model(str(workspace), str(workspace), name, "单3D")
    assert result["ok"], result
    return workspace / name


def _leaf(model_root: Path, module: str = "主板程序") -> Path:
    """legacy 模块叶子：模块目录直接含固件文件。"""
    root = model_root / "通用" / module
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(b"firmware")
    return root


def _variant(model_root: Path, module: str = "主板程序", name: str = "v1") -> Path:
    root = model_root / "通用" / module / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(b"firmware")
    return root


def _source(tmp_path: Path, name: str = "来源", payload: bytes = b"new-firmware") -> Path:
    root = tmp_path / "外部" / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(payload)
    return root


def _assert_converged_clean(workspace: Path) -> None:
    """锁内未写入的拒绝分支：clean + 偶数 generation（MSC-001 同构回归）。"""
    status = load_workspace_status(workspace)
    assert status.state == "clean"
    assert status.generation % 2 == 0


# ---------------------------------------------------------------------------
# D0.3 准入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset_name", ["", "   ", "非法/名称"])
def test_normalize_rejects_missing_or_invalid_asset_name(
    tmp_path: Path, asset_name: str
) -> None:
    """程序名必须由调用方（用户）传入，实现不得从类型名或版本号派生。"""
    model = _model(tmp_path)
    leaf = _leaf(model)

    result = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, asset_name)

    assert result["ok"] is False
    assert result["code"] == "invalid_asset_name"
    assert (leaf / "fw.bin").exists()
    _assert_converged_clean(tmp_path)


def test_normalize_rejects_module_container(tmp_path: Path) -> None:
    """已是容器（模块下只有变体子目录）→ not_module_leaf。"""
    model = _model(tmp_path)
    variant = _variant(model)

    result = normalize_module_leaf(
        str(tmp_path), str(tmp_path), variant.parent, "程序A"
    )

    assert result["ok"] is False
    assert result["code"] == "not_module_leaf"
    _assert_converged_clean(tmp_path)


def test_normalize_rejects_directory_without_firmware(tmp_path: Path) -> None:
    model = _model(tmp_path)
    empty = model / "通用" / "主板程序"
    empty.mkdir(parents=True)

    result = normalize_module_leaf(str(tmp_path), str(tmp_path), empty, "程序A")

    assert result["ok"] is False
    assert result["code"] in ("not_module_leaf", "not_single_asset")
    _assert_converged_clean(tmp_path)


# ---------------------------------------------------------------------------
# D0.3 成功路径
# ---------------------------------------------------------------------------


def test_normalize_moves_leaf_into_variant_and_syncs_refs(tmp_path: Path) -> None:
    """六步状态机：M → M/V，static / defaults 同步，索引对账。"""
    model = _model(tmp_path)
    borrower = _model(tmp_path, "L50程序")
    leaf = _leaf(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": ""})])
    save_shared_module(
        borrower,
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path="L36程序/通用/主板程序",
            mode="static",
        ),
    )

    result = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, "程序A")

    assert result["ok"] is True, result
    assert result["code"] in ("ok", "reindex_failed")
    assert (leaf / "程序A" / "fw.bin").exists()
    assert not (leaf / "fw.bin").exists()
    assert load_platform_config(model)[0].defaults["主板程序"] == "程序A"
    from fwasset.core.model_config import load_shared_modules

    refs = {r.module_key: r for r in load_shared_modules(borrower)}
    assert refs["主板程序"].source_relative_path == "L36程序/通用/主板程序/程序A"
    _assert_converged_clean(tmp_path)


def test_normalize_keeps_scene_when_new_container_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """步骤 4 失败且新 M 非空：禁止递归清理未知内容，保留现场。"""
    model = _model(tmp_path)
    leaf = _leaf(model)

    def _fail_promote(*args: object, **kwargs: object) -> None:
        # 模拟外部写入后提升失败：新 M 内已有无关内容
        target = Path(str(args[3]))
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.parent / "外部文件.txt").write_text("x", encoding="utf-8")
        raise layout_update_service.StagingError("提升失败")

    monkeypatch.setattr(layout_update_service, "promote_staging", _fail_promote)

    result = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, "程序A")

    assert result["ok"] is False
    assert result["code"] == "layout_inconsistent"
    assert (leaf / "外部文件.txt").exists()  # 未被递归删除
    assert load_workspace_status(tmp_path).state == "recovery_required"


# ---------------------------------------------------------------------------
# D1.4a
# ---------------------------------------------------------------------------


def test_update_rejects_unnormalized_module_leaf(tmp_path: Path) -> None:
    """未归一的模块叶子不允许直接 update：零写盘、generation 不变。"""
    model = _model(tmp_path)
    leaf = _leaf(model)
    source = _source(tmp_path)
    generation_before = load_workspace_status(tmp_path).generation

    result = update_asset(
        str(tmp_path),
        str(tmp_path),
        leaf,
        source,
        retire_mode="retire_to_trash",
    )

    assert result["ok"] is False
    assert result["code"] == "normalize_required"
    assert load_workspace_status(tmp_path).generation == generation_before
    assert (leaf / "fw.bin").read_bytes() == b"firmware"
    _assert_converged_clean(tmp_path)


def test_update_retire_to_trash_registers_quarantine(tmp_path: Path) -> None:
    """retire_to_trash：旧程序进 transactional_retire 隔离记录。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is True, result
    assert result["code"] in ("ok", "reindex_failed")
    replacement = Path(result["payload"]["replacement"])
    assert replacement.parent == old.parent  # 兄弟变体目录，非原位覆盖
    assert (replacement / "fw.bin").read_bytes() == b"new-firmware"
    kinds = [r["kind"] for r in list_records(tmp_path)]
    assert "transactional_retire" in kinds
    assert not old.exists()
    _assert_converged_clean(tmp_path)


def test_update_retire_to_backup_writes_old_version_copy(tmp_path: Path) -> None:
    """retire_to_backup：落 <replacement>/旧版本/<旧名>-<时间戳>/，不进隔离区。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )

    assert result["ok"] is True, result
    assert result["code"] in ("ok", "reindex_failed")
    replacement = Path(result["payload"]["replacement"])
    backups = list((replacement / RETIRED_VERSIONS_DIRNAME).iterdir())
    assert len(backups) == 1
    assert backups[0].name.startswith(f"{old.name}-")
    assert (backups[0] / "fw.bin").read_bytes() == b"firmware"
    assert [r for r in list_records(tmp_path) if r["kind"] == "transactional_retire"] == []
    assert not old.exists()
    _assert_converged_clean(tmp_path)


def test_update_keeps_scene_when_product_manifest_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1.4c：外部写入 replacement 后 manifest 不符 → 保留现场，不递归删除。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    original_build = layout_update_service.build_rewrite_plan

    def _tamper_then_fail(*args: object, **kwargs: object) -> dict:
        # 在清理触发前模拟外部程序往 replacement 里塞文件
        replacement = old.parent / source.name
        if replacement.is_dir():
            (replacement / "外部写入.txt").write_text("x", encoding="utf-8")
        return {
            "ok": False,
            "code": "canonical_conflict",
            "message": "构建失败",
            "payload": {},
        }

    monkeypatch.setattr(layout_update_service, "build_rewrite_plan", _tamper_then_fail)
    assert original_build is not layout_update_service.build_rewrite_plan

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is False
    assert result["code"] == "update_inconsistent"
    replacement = old.parent / source.name
    assert (replacement / "外部写入.txt").exists()  # 现场保留
    assert old.exists()  # 旧程序未退位
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_update_cleans_replacement_when_plan_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build 失败且产物未被外部改动 → 按 D1.4c 清理，收敛为 clean。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    monkeypatch.setattr(
        layout_update_service,
        "build_rewrite_plan",
        lambda *a, **k: {
            "ok": False,
            "code": "canonical_conflict",
            "message": "构建失败",
            "payload": {},
        },
    )

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is False
    assert result["code"] == "plan_build_failed"
    assert not (old.parent / source.name).exists()  # 产物已清理
    assert (old / "fw.bin").read_bytes() == b"firmware"  # 旧程序原样
    _assert_converged_clean(tmp_path)


def test_update_reports_retire_failure_keeping_both_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply 成功、退位失败 → retire_failed，两份并存；新程序必须可用。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise layout_update_service.QuarantineError("退位失败")

    monkeypatch.setattr(layout_update_service, "register_retire", _boom)

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is False
    assert result["code"] == "retire_failed"
    replacement = old.parent / source.name
    assert (replacement / "fw.bin").read_bytes() == b"new-firmware"
    assert (old / "fw.bin").exists()  # 旧程序保留，不出现「旧已退位但新不可用」


def test_update_replaces_unrecognized_files(tmp_path: Path) -> None:
    """更新不再因内容对不上 catalog 而取消，来源文件原样替换。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = tmp_path / "外部" / "空来源"
    source.mkdir(parents=True)
    (source / "readme.txt").write_text("不是固件", encoding="utf-8")

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is True, result
    replacement = old.parent / source.name
    assert (replacement / "readme.txt").read_text(encoding="utf-8") == "不是固件"
    assert not old.exists()
    _assert_converged_clean(tmp_path)


def test_update_config_rewrite_failure_cleans_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply 完整回滚成功 → 清理本次 replacement，恢复原状，收敛 clean。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    monkeypatch.setattr(
        layout_update_service,
        "apply_rewrite_plan",
        lambda *a, **k: {
            "ok": False,
            "code": "rolled_back",
            "message": "写入失败，已恢复原状",
            "payload": {"rolled_back": [], "rollback_conflict": []},
        },
    )

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is False
    assert result["code"] == "config_rewrite_failed"
    assert not (old.parent / source.name).exists()
    assert (old / "fw.bin").read_bytes() == b"firmware"
    _assert_converged_clean(tmp_path)


def test_update_rollback_conflict_keeps_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply rollback conflict → 保留现场，**不自动删除 replacement**。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    monkeypatch.setattr(
        layout_update_service,
        "apply_rewrite_plan",
        lambda *a, **k: {
            "ok": False,
            "code": "rollback_conflict",
            "message": "回滚冲突",
            "payload": {"rollback_conflict": [{"path": "x"}]},
        },
    )

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )

    assert result["ok"] is False
    assert result["code"] == "update_inconsistent"
    assert (old.parent / source.name / "fw.bin").exists()  # replacement 保留
    assert old.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_inconsistent_scene_blocks_further_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D8.4：不一致现场保持 recovery_required，后续写操作一律阻止。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    monkeypatch.setattr(
        layout_update_service,
        "apply_rewrite_plan",
        lambda *a, **k: {
            "ok": False,
            "code": "rollback_conflict",
            "message": "回滚冲突",
            "payload": {},
        },
    )
    first = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert first["code"] == "update_inconsistent"
    monkeypatch.undo()

    second = update_asset(
        str(tmp_path),
        str(tmp_path),
        old,
        _source(tmp_path, "来源二"),
        retire_mode="retire_to_trash",
    )

    assert second["ok"] is False
    assert second["code"] == "recovery_required"
    status = load_workspace_status(tmp_path)
    assert status.state == "recovery_required"
    assert status.generation % 2 == 0  # seqlock 已收尾为偶数


# ---------------------------------------------------------------------------
# 6A-IMP-001 / 6A-IMP-002 / 6A-IMP-003
# ---------------------------------------------------------------------------


def test_update_backup_writes_minimum_retired_metadata(tmp_path: Path) -> None:
    """6A-IMP-001：backup 副本必须写最小退位信息（原路径、时间戳、来源操作）。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )

    assert result["ok"] is True, result
    replacement = Path(result["payload"]["replacement"])
    backups = list((replacement / RETIRED_VERSIONS_DIRNAME).iterdir())
    assert len(backups) == 1
    meta_path = backups[0] / RETIRED_METADATA_FILENAME
    assert meta_path.is_file()
    data = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    assert str(data.get("retired_from") or data.get("original_path") or "").strip()
    assert str(data.get("retired_at") or data.get("退位时间戳") or "").strip()
    source_op = str(
        data.get("retired_by") or data.get("operation") or data.get("来源") or ""
    ).strip()
    assert source_op


def test_update_accepts_firmware_asset_mapping(tmp_path: Path) -> None:
    """6A-IMP-003：公开契约是 FirmwareAsset，取其 path 后再冷扫，不能 Path(dict)。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    result = update_asset(
        str(tmp_path),
        str(tmp_path),
        {"path": str(old)},
        source,
        retire_mode="retire_to_trash",
    )

    assert result["ok"] is True, result
    assert result["code"] in ("ok", "reindex_failed")
    assert not old.exists()
    _assert_converged_clean(tmp_path)


def test_normalize_persists_staging_path_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-002：M → staging 之前必须把 staging 路径写入操作日志。"""
    model = _model(tmp_path)
    leaf = _leaf(model)

    original_replace = layout_update_service.os.replace

    def _crash_module_move(src: object, dst: object) -> None:
        destination = Path(dst)
        # 只打断产品目录搬进 staging 会话，不动事务日志的原子 replace。
        if destination.parent.name == "staging":
            raise RuntimeError("injected crash before replace")
        original_replace(src, dst)

    monkeypatch.setattr(layout_update_service.os, "replace", _crash_module_move)

    result = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, "程序A")

    assert result["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    log = load_operation_log(tmp_path)
    assert log is not None
    staging = str((log.get("details") or {}).get("staging") or "").strip()
    assert staging
    assert (leaf / "fw.bin").exists()  # replace 未发生，叶子仍在


def test_normalize_resume_after_move_to_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃在 M 已进 staging、容器尚未创建：续跑完成归一。"""
    model = _model(tmp_path)
    leaf = _leaf(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": ""})])
    original_set_phase = WorkspaceTransaction.set_phase

    def _crash_on_create(
        self: WorkspaceTransaction,
        phase: str,
        *,
        details: object = None,
    ) -> None:
        if phase == "create_container":
            raise RuntimeError("injected crash after staging")
        original_set_phase(self, phase, details=details)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceTransaction, "set_phase", _crash_on_create)
    crashed = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, "程序A")
    assert crashed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert not (leaf / "fw.bin").exists()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import (
        resume_normalize_module_leaf,
    )

    resumed = resume_normalize_module_leaf(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert (leaf / "程序A" / "fw.bin").exists()
    assert load_platform_config(model)[0].defaults["主板程序"] == "程序A"
    _assert_converged_clean(tmp_path)


def test_update_resume_cleans_replacement_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """规格崩溃表：replacement 已提升、尚未成功 apply → 按 D1.4c 清理回原状。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash at build")

    monkeypatch.setattr(layout_update_service, "build_rewrite_plan", _crash)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert crashed["ok"] is False
    assert (old.parent / source.name).is_dir()
    assert old.exists()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not (old.parent / source.name).exists()
    assert (old / "fw.bin").read_bytes() == b"firmware"
    _assert_converged_clean(tmp_path)


def test_update_resume_continues_retire_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """规格崩溃表：apply 成功、旧程序尚未退位 → 续跑继续退位。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash at retire")

    monkeypatch.setattr(layout_update_service, "register_retire", _crash)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    assert replacement.exists() and old.exists()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not old.exists()
    assert (replacement / "fw.bin").read_bytes() == b"new-firmware"
    kinds = [r["kind"] for r in list_records(tmp_path)]
    assert "transactional_retire" in kinds
    _assert_converged_clean(tmp_path)


def test_update_resume_postimage_is_apply_done_not_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-004：apply 已写完 postimage 但日志仍是 apply_plan → 继续退位，不得删 replacement。"""
    model = _model(tmp_path)
    old = _variant(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": "v1"})])
    source = _source(tmp_path)
    original_set_phase = WorkspaceTransaction.set_phase

    def _crash_on_apply_done(
        self: WorkspaceTransaction,
        phase: str,
        *,
        details: object = None,
    ) -> None:
        if phase == "apply_done":
            raise RuntimeError("injected crash after apply")
        original_set_phase(self, phase, details=details)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceTransaction, "set_phase", _crash_on_apply_done)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    assert replacement.exists() and old.exists()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not old.exists()
    assert (replacement / "fw.bin").read_bytes() == b"new-firmware"
    assert load_platform_config(model)[0].defaults["主板程序"] == source.name
    _assert_converged_clean(tmp_path)


def test_normalize_resume_postimage_does_not_reapply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-004：归一 apply 已成功，续跑不得因 preimage 失配而 stale_plan。"""
    model = _model(tmp_path)
    leaf = _leaf(model)
    save_platform_config(model, [PlatformDefaults("单3D", {"主板程序": ""})])
    original_set_phase = WorkspaceTransaction.set_phase

    def _crash_on_apply_done(
        self: WorkspaceTransaction,
        phase: str,
        *,
        details: object = None,
    ) -> None:
        if phase == "apply_done":
            raise RuntimeError("injected crash after normalize apply")
        original_set_phase(self, phase, details=details)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceTransaction, "set_phase", _crash_on_apply_done)
    crashed = normalize_module_leaf(str(tmp_path), str(tmp_path), leaf, "程序A")
    assert crashed["ok"] is False
    assert (leaf / "程序A" / "fw.bin").exists()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import (
        resume_normalize_module_leaf,
    )

    resumed = resume_normalize_module_leaf(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert load_platform_config(model)[0].defaults["主板程序"] == "程序A"
    _assert_converged_clean(tmp_path)


def test_update_resume_stops_when_old_vanishes_without_retire_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-005：apply 后第三方删走旧程序 → 停止，不得 clean 提交。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash at retire")

    monkeypatch.setattr(layout_update_service, "register_retire", _crash)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    assert old.exists() and replacement.exists()
    shutil.rmtree(old)
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert replacement.exists()


def test_update_persists_session_before_staging_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-006：开始写入 staging 内容前必须把 session 路径写入操作日志。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash during staging copy")

    monkeypatch.setattr(layout_update_service, "stage_import_directory", _crash)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_trash"
    )
    assert crashed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    log = load_operation_log(tmp_path)
    assert log is not None
    session = str((log.get("details") or {}).get("session") or "").strip()
    assert session
    assert Path(session).is_dir()
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not Path(session).exists()
    assert (old / "fw.bin").exists()
    _assert_converged_clean(tmp_path)


def test_update_resume_rejects_stale_backup_as_retire_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-007：历史 旧版本 仅路径相同不算本次退位证据。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash at retire")

    monkeypatch.setattr(layout_update_service, "retire_asset_to_backup", _crash)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    stale = replacement / RETIRED_VERSIONS_DIRNAME / "历史副本"
    stale.mkdir(parents=True)
    (stale / RETIRED_METADATA_FILENAME).write_text(
        f"retired_from = {old.as_posix()!r}\n"
        'content_hash = "deadbeef"\n'
        'retired_at = "2020-01-01T00:00:00Z"\n'
        'retired_by = "update_asset"\n',
        encoding="utf-8",
    )
    shutil.rmtree(old)
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert replacement.exists()


def test_update_resume_verifies_product_after_backup_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-009：正式 backup 已在时，续跑仍须校验 replacement manifest。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    original_set_phase = WorkspaceTransaction.set_phase

    def _crash_on_reconcile(
        self: WorkspaceTransaction,
        phase: str,
        *,
        details: object = None,
    ) -> None:
        if phase == "reconcile":
            raise RuntimeError("injected crash after backup")
        original_set_phase(self, phase, details=details)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceTransaction, "set_phase", _crash_on_reconcile)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    assert replacement.exists()
    (replacement / "fw.bin").write_bytes(b"tampered-after-crash")
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert (replacement / "fw.bin").read_bytes() == b"tampered-after-crash"


class _InjectedCrash(BaseException):
    """模拟进程在 except Exception 之外死亡。"""


def test_update_resume_keeps_temp_when_destination_is_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-010：destination 被占用且 temp 仍有旧程序时，不得清理 temp。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    original_replace = layout_update_service.os.replace

    def _occupy_destination(src: object, dst: object) -> None:
        destination = Path(dst)
        if destination.parent.name == RETIRED_VERSIONS_DIRNAME:
            destination.mkdir(parents=True, exist_ok=True)
            raise _InjectedCrash()
        original_replace(src, dst)

    monkeypatch.setattr(layout_update_service.os, "replace", _occupy_destination)
    with pytest.raises(_InjectedCrash):
        update_asset(
            str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
        )
    assert load_workspace_status(tmp_path).state == "recovery_required"
    log = load_operation_log(tmp_path)
    assert log is not None
    temp = Path(str((log.get("details") or {}).get("retire_temp") or ""))
    assert temp.is_dir()
    assert (temp / "fw.bin").read_bytes() == b"firmware"
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert (temp / "fw.bin").read_bytes() == b"firmware"


def test_update_resume_stops_if_old_reappears_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6A-IMP-011：正式 backup 完成后旧路径又出现 → 不得 clean 提交。"""
    model = _model(tmp_path)
    old = _variant(model)
    source = _source(tmp_path)
    original_set_phase = WorkspaceTransaction.set_phase

    def _crash_on_reconcile(
        self: WorkspaceTransaction,
        phase: str,
        *,
        details: object = None,
    ) -> None:
        if phase == "reconcile":
            raise RuntimeError("injected crash after backup")
        original_set_phase(self, phase, details=details)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceTransaction, "set_phase", _crash_on_reconcile)
    crashed = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert crashed["ok"] is False
    replacement = old.parent / source.name
    assert replacement.exists()
    assert not old.exists()
    old.mkdir()
    (old / "fw.bin").write_bytes(b"third-party-restored")
    monkeypatch.undo()

    from fwasset.core.services.layout_update_service import resume_update_asset

    resumed = resume_update_asset(str(tmp_path), tmp_path)
    assert resumed["ok"] is False
    assert load_workspace_status(tmp_path).state == "recovery_required"
    assert (old / "fw.bin").read_bytes() == b"third-party-restored"
    assert replacement.exists()
