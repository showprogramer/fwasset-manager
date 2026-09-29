"""型号 / 方案 CRUD 服务测试（TASK-20260918-model-scheme-crud）。

覆盖 D9 验收断言、MSC-001/005/006/008/014/015/016/017 回归，以及创建/
重命名/删除/撤销的常规与失败路径。工作区一律用 ``tmp_path``，配置根与
工作区根相同（MSC-017 的越界用例除外——需在 ``tmp_path`` 下另建工作区外目录）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fwasset.core.managed_paths import detect_workspace_layout
from fwasset.core.model_config import load_model_config
from fwasset.core.platform_config import (
    PlatformDefaults,
    load_platform_config_with_status,
    save_platform_config,
)
from fwasset.core.reference_lookup import enumerate_model_roots
from fwasset.core.services.model_scheme_service import (
    change_chassis_type,
    create_model,
    create_scheme,
    delete_model,
    delete_scheme,
    rename_model,
    rename_scheme,
    undo_model_scheme_delete,
)
from fwasset.core.workspace_transaction import load_workspace_status


def _create_model(ws: Path, name: str = "L99程序", chassis: str = "单3D") -> dict:
    return create_model(str(ws), str(ws), name, chassis)  # type: ignore[arg-type]


def test_same_model_two_chassis_coexist_as_separate_models(tmp_path: Path) -> None:
    assert _create_model(tmp_path, "L36 单3D", "单3D")["ok"] is True
    assert _create_model(tmp_path, "L36 双2D", "双2D")["ok"] is True
    names = sorted(p.name for p in enumerate_model_roots(tmp_path))
    assert names == ["L36 单3D", "L36 双2D"]


def test_change_chassis_type_keeps_defaults(tmp_path: Path) -> None:
    _create_model(tmp_path, "L36", "单3D")
    root = tmp_path / "L36"
    save_platform_config(root, [PlatformDefaults("单3D", {"蓝牙": "v1"})])
    result = change_chassis_type(str(tmp_path), str(tmp_path), root, "双2D")  # type: ignore[arg-type]
    assert result["ok"] is True
    platforms, status, _err = load_platform_config_with_status(root)
    assert status == "ok"
    assert [(p.platform_name, p.defaults) for p in platforms] == [("双2D", {"蓝牙": "v1"})]
    assert load_workspace_status(tmp_path).state == "clean"


def test_change_chassis_type_same_value_is_unchanged(tmp_path: Path) -> None:
    _create_model(tmp_path, "L36", "单3D")
    result = change_chassis_type(str(tmp_path), str(tmp_path), tmp_path / "L36", "单3D")
    assert result["ok"] is True
    assert result["code"] == "unchanged"


def test_change_chassis_type_rejects_invalid_and_multi_block(tmp_path: Path) -> None:
    _create_model(tmp_path, "L36", "单3D")
    root = tmp_path / "L36"
    bad = change_chassis_type(str(tmp_path), str(tmp_path), root, "乱写")  # type: ignore[arg-type]
    assert bad["code"] == "invalid_chassis_type"
    save_platform_config(root, [PlatformDefaults("A"), PlatformDefaults("B")])
    multi = change_chassis_type(str(tmp_path), str(tmp_path), root, "双2D")
    assert multi["code"] == "platform_not_normalized"
    assert load_workspace_status(tmp_path).state == "clean"


def test_dir_name_helpers() -> None:
    from fwasset.ui_common.workspace_actions import (
        compose_model_dir_name,
        retarget_model_dir_name,
    )

    assert compose_model_dir_name(" L36 ", "单3D") == "L36 单3D"
    assert compose_model_dir_name("L36 单3D", "单3D") == "L36 单3D"
    assert retarget_model_dir_name("L36 单3D", "单3D", "双2D") == "L36 双2D"
    assert retarget_model_dir_name("L36", "单3D", "双2D") is None


# ---------------------------------------------------------------------------
# create_model
# ---------------------------------------------------------------------------


def test_create_model_in_empty_workspace(tmp_path: Path) -> None:
    assert detect_workspace_layout(tmp_path) == "empty"
    result = _create_model(tmp_path)
    assert result["ok"] is True
    assert detect_workspace_layout(tmp_path) == "multi_model"
    mid, status, _err = load_model_config(tmp_path / "L99程序")
    assert status == "ok"
    assert mid == "l99"


def test_create_model_id_dedup_on_collision(tmp_path: Path) -> None:
    from fwasset.core.model_config import slugify_model_id

    # MSC-012：「L99程序」与「L99程序2」的 slug 不相同，不会触发去重逻辑
    # （空跑）。改用「L99程序」与「L99目录」——slugify_model_id 会剥离
    # 「程序」「目录」两种后缀，两者剥离后同为「L99」，slug 都是 "l99"，
    # 真实触发碰撞。
    assert slugify_model_id("L99程序") == slugify_model_id("L99目录") == "l99"

    r1 = _create_model(tmp_path, "L99程序")
    assert r1["ok"] is True
    assert r1["payload"]["model_id"] == "l99"
    r2 = create_model(str(tmp_path), str(tmp_path), "L99目录", "单3D")  # type: ignore[arg-type]
    assert r2["ok"] is True
    assert r2["payload"]["model_id"] != r1["payload"]["model_id"]
    assert r2["payload"]["model_id"] == "l99-2"


def test_create_model_single_model_layout_migration_required(tmp_path: Path) -> None:
    from fwasset.core.model_config import save_model_id

    save_model_id(tmp_path, "legacy")
    assert detect_workspace_layout(tmp_path) == "single_model"
    result = _create_model(tmp_path)
    assert result["ok"] is False
    assert result["code"] == "migration_required"


def test_create_model_invalid_layout(tmp_path: Path) -> None:
    (tmp_path / "杂物").mkdir()
    (tmp_path / "杂物" / "readme.txt").write_text("x", encoding="utf-8")
    assert detect_workspace_layout(tmp_path) == "invalid"
    result = _create_model(tmp_path)
    assert result["ok"] is False
    assert result["code"] == "layout_invalid"


def test_create_model_invalid_chassis_type(tmp_path: Path) -> None:
    result = create_model(str(tmp_path), str(tmp_path), "L99程序", "不存在的类型")  # type: ignore[arg-type]
    assert result["ok"] is False
    assert result["code"] == "invalid_chassis_type"


def test_create_model_path_exists_rejected_and_workspace_clean(tmp_path: Path) -> None:
    _create_model(tmp_path)
    result = _create_model(tmp_path)
    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert load_workspace_status(tmp_path).state == "clean"


def test_create_model_staging_promote_failure_leaves_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import model_scheme_service as svc

    def _boom(*args: object, **kwargs: object) -> None:
        raise svc.StagingError("模拟提升失败")

    monkeypatch.setattr(svc, "promote_staging", _boom)
    result = _create_model(tmp_path)
    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert not (tmp_path / "L99程序").exists()


def test_create_model_index_write_failure_is_ok_with_index_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import model_scheme_service as svc

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟索引失败")

    monkeypatch.setattr(svc, "bulk_reindex_subtree", _boom)
    result = _create_model(tmp_path)
    assert result["ok"] is True
    assert result["code"] == "index_pending"
    assert (tmp_path / "L99程序").is_dir()


# ---------------------------------------------------------------------------
# create_scheme
# ---------------------------------------------------------------------------


def test_create_scheme_success_writes_name_without_platform(tmp_path: Path) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    result = create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    assert result["ok"] is True
    scheme_root = model_root / "定制" / "方案A"
    assert scheme_root.is_dir()
    content = (scheme_root / "方案配置.toml").read_text(encoding="utf-8")
    assert 'name = "方案A"' in content
    assert "platform" not in content


def test_create_scheme_invalid_target(tmp_path: Path) -> None:
    _create_model(tmp_path)
    not_a_model_root = tmp_path / "L99程序" / "通用"
    not_a_model_root.mkdir(parents=True, exist_ok=True)
    result = create_scheme(str(tmp_path), str(tmp_path), str(not_a_model_root), "方案A")
    assert result["ok"] is False
    assert result["code"] == "invalid_target"


def test_create_scheme_path_exists_not_reused(tmp_path: Path) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    (model_root / "定制" / "方案A").mkdir(parents=True)
    result = create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    assert result["ok"] is False
    assert result["code"] == "path_exists"


# ---------------------------------------------------------------------------
# rename_model / rename_scheme
# ---------------------------------------------------------------------------


def test_rename_model_success_keeps_model_id(tmp_path: Path) -> None:
    r = _create_model(tmp_path)
    model_id = r["payload"]["model_id"]
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is True
    assert (tmp_path / "L99新名").is_dir()
    assert not (tmp_path / "L99程序").exists()
    mid, status, _err = load_model_config(tmp_path / "L99新名")
    assert status == "ok"
    assert mid == model_id


def test_rename_scheme_success_updates_name_field(tmp_path: Path) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    scheme_root = model_root / "定制" / "方案A"
    result = rename_scheme(str(tmp_path), str(tmp_path), scheme_root, "方案B")
    assert result["ok"] is True
    new_root = model_root / "定制" / "方案B"
    assert new_root.is_dir()
    content = (new_root / "方案配置.toml").read_text(encoding="utf-8")
    assert 'name = "方案B"' in content


def test_rename_model_rewrites_shared_borrower(tmp_path: Path) -> None:
    from fwasset.core.model_config import SharedModuleRef, save_shared_module

    _create_model(tmp_path, "L99程序")
    _create_model(tmp_path, "L100程序", "单2D")
    common = tmp_path / "L99程序" / "通用" / "快捷键程序"
    common.mkdir(parents=True)
    (common / "key.hex").write_text("x", encoding="utf-8")
    save_shared_module(
        tmp_path / "L100程序",
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l99",
            source_group="l99",
            source_module="快捷键程序",
            source_relative_path="L99程序/通用/快捷键程序",
            mode="static",
        ),
    )
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is True
    from fwasset.core.model_config import load_shared_modules

    refs = load_shared_modules(tmp_path / "L100程序")
    assert refs[0].source_relative_path == "L99新名/通用/快捷键程序"


def test_rename_model_rollback_conflict_returns_rename_inconsistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)

    def _fake_apply(plan: object, configured_root: object, log_fn: object = print):
        return {
            "ok": False,
            "code": "rollback_conflict",
            "message": "模拟冲突",
            "payload": {"applied": [], "rolled_back": [], "rollback_conflict": [{"path": "x"}], "failed": []},
        }

    from fwasset.core.services import model_scheme_service as svc

    monkeypatch.setattr(svc, "apply_rewrite_plan", _fake_apply)
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is False
    assert result["code"] == "rename_inconsistent"
    assert (tmp_path / "L99新名").is_dir()
    assert not (tmp_path / "L99程序").exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_rename_model_full_rollback_moves_dir_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)

    def _fake_apply(plan: object, configured_root: object, log_fn: object = print):
        return {
            "ok": False,
            "code": "rolled_back",
            "message": "模拟回滚成功",
            "payload": {"applied": [], "rolled_back": ["x"], "rollback_conflict": [], "failed": []},
        }

    from fwasset.core.services import model_scheme_service as svc

    monkeypatch.setattr(svc, "apply_rewrite_plan", _fake_apply)
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is False
    assert result["code"] == "rewrite_failed"
    assert (tmp_path / "L99程序").is_dir()
    assert not (tmp_path / "L99新名").exists()
    assert load_workspace_status(tmp_path).state == "clean"


def test_rename_model_os_replace_failure_returns_rename_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)

    from fwasset.core.services import model_scheme_service as svc

    apply_called = []
    original_apply = svc.apply_rewrite_plan

    def _tracking_apply(*args: object, **kwargs: object):
        apply_called.append(True)
        return original_apply(*args, **kwargs)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("模拟移动失败")

    monkeypatch.setattr(svc, "_move_directory", _boom)
    monkeypatch.setattr(svc, "apply_rewrite_plan", _tracking_apply)
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is False
    assert result["code"] == "rename_failed"
    assert not apply_called
    assert load_workspace_status(tmp_path).state == "clean"


def test_rename_model_index_boundary_reconciles_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)
    from fwasset.core.services import model_scheme_service as svc

    calls: list[tuple[str, str]] = []
    original = svc.reconcile_subtree

    def _tracking(workspace_root: str, subtree_root: str, **kwargs: object) -> None:
        calls.append((workspace_root, subtree_root))
        original(workspace_root, subtree_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "reconcile_subtree", _tracking)
    rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert calls
    assert calls[0][1] == str(tmp_path)


def test_rename_scheme_index_boundary_reconciles_custom_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    from fwasset.core.services import model_scheme_service as svc

    calls: list[tuple[str, str]] = []
    original = svc.reconcile_subtree

    def _tracking(workspace_root: str, subtree_root: str, **kwargs: object) -> None:
        calls.append((workspace_root, subtree_root))
        original(workspace_root, subtree_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "reconcile_subtree", _tracking)
    scheme_root = model_root / "定制" / "方案A"
    rename_scheme(str(tmp_path), str(tmp_path), scheme_root, "方案B")
    assert calls
    assert calls[0][1] == str(model_root / "定制")


def test_rename_model_lock_scoped_revalidation_rejects_concurrent_occupation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-006：锁外校验通过后、锁内重验前构造并发占用 → path_exists，且不落 recovery_required。"""
    _create_model(tmp_path)
    from fwasset.core.admission import validate_new_path as real_validate
    from fwasset.core.services import model_scheme_service as svc

    calls = {"count": 0}
    move_called = {"hit": False}

    def _validate_then_occupy(target: Path, **kwargs: object) -> Path:
        calls["count"] += 1
        result = real_validate(target, **kwargs)  # type: ignore[arg-type]
        if calls["count"] == 1:
            # 锁外校验通过后，模拟第三方在锁内重验前抢占新路径
            Path(target).mkdir(parents=True)
        return result

    def _tracking_move(*args: object, **kwargs: object) -> None:
        move_called["hit"] = True

    monkeypatch.setattr(svc, "validate_new_path", _validate_then_occupy)
    monkeypatch.setattr(svc, "_move_directory", _tracking_move)
    result = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert load_workspace_status(tmp_path).state == "clean"
    assert not move_called["hit"]


# ---------------------------------------------------------------------------
# delete_model / delete_scheme / undo
# ---------------------------------------------------------------------------


def test_delete_model_no_external_borrow_succeeds_directly(tmp_path: Path) -> None:
    _create_model(tmp_path)
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=False
    )
    assert result["ok"] is True
    assert not (tmp_path / "L99程序").exists()


def test_delete_model_register_delete_failure_returns_service_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-009/MSC-014：register_delete 抛 QuarantineError 不得穿透 service 边界。

    该失败点（manifest 计算失败等）发生在源目录被实际移动之前（原路径仍
    存在），属零产物，应 commit() 收敛为 clean 并返回 ServiceResult，目标
    目录原样保留。移动之后的失败见
    ``test_delete_model_register_delete_failure_after_move_keeps_recovery_required``
    （MSC-014）。
    """
    from fwasset.core.quarantine import QuarantineError
    from fwasset.core.services import model_scheme_service as svc

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"

    def _boom(workspace_root: object, source: object) -> None:
        raise QuarantineError("模拟隔离登记失败")

    monkeypatch.setattr(svc, "register_delete", _boom)
    result = delete_model(str(tmp_path), str(tmp_path), model_root, confirm_shared=False)
    assert result["ok"] is False
    assert result["code"] == "quarantine_failed"
    assert model_root.is_dir()
    assert load_workspace_status(tmp_path).state == "clean"


def test_delete_model_register_delete_failure_after_move_keeps_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-014：register_delete 的第二次清单写入（转正）失败时目标已移动。

    ``quarantine._register`` 先落盘 "moving" 记录、再 ``os.replace`` 实际
    移动、最后再写一次清单转正为 "pending"。mock
    ``_save_quarantine_manifest`` 使其在第二次调用（转正）时抛错，此时
    源目录已经不在原路径——属非零产物半成品，不得 commit() 掩盖为 clean；
    必须保持 recovery_required，且返回 ServiceResult 而非裸异常。
    """
    from fwasset.core import quarantine as quarantine_module
    from fwasset.core.quarantine import QuarantineError

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"

    call_count = {"n": 0}
    original_save = quarantine_module._save_quarantine_manifest

    def _boom(workspace_root: object, records: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise QuarantineError("模拟转正阶段清单写入失败")
        original_save(workspace_root, records)  # type: ignore[arg-type]

    monkeypatch.setattr(quarantine_module, "_save_quarantine_manifest", _boom)
    result = delete_model(str(tmp_path), str(tmp_path), model_root, confirm_shared=False)
    assert result["ok"] is False
    assert result["code"] == "quarantine_failed"
    assert result["payload"]["recovery_required"] is True

    # 目标已被移动 = 有产物，工作区必须停在 recovery_required 待人工恢复
    assert not model_root.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_delete_model_cross_owner_borrow_requires_confirmation(tmp_path: Path) -> None:
    from fwasset.core.model_config import SharedModuleRef, save_shared_module

    _create_model(tmp_path, "L99程序")
    _create_model(tmp_path, "L100程序", "单2D")
    common = tmp_path / "L99程序" / "通用" / "快捷键程序"
    common.mkdir(parents=True)
    (common / "key.hex").write_text("x", encoding="utf-8")
    save_shared_module(
        tmp_path / "L100程序",
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l99",
            source_group="l99",
            source_module="快捷键程序",
            source_relative_path="L99程序/通用/快捷键程序",
            mode="static",
        ),
    )
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=False
    )
    assert result["ok"] is False
    assert result["code"] == "confirmation_required"
    assert (tmp_path / "L99程序").exists()

    confirmed = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=True
    )
    assert confirmed["ok"] is True
    assert not (tmp_path / "L99程序").exists()


def test_delete_scheme_same_model_reference_does_not_require_confirmation(
    tmp_path: Path,
) -> None:
    """MSC-010：方案删除时同型号内的自引用不应被误判为跨 owner。

    ``_cross_owner_hits`` 必须传入方案所属的型号根（``scheme_root.parent.parent``）
    与 ``hit.owner_root``（恒为型号根）比较，而不是方案目录本身——否则方案
    目录永远不等于任何 ``hit.owner_root``，同型号内的引用会被误判为跨 owner，
    凭空要求用户勾选确认。
    """
    from fwasset.core.model_config import SharedModuleRef, save_shared_module

    _create_model(tmp_path, "L99程序")
    model_root = tmp_path / "L99程序"
    create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    scheme_root = model_root / "定制" / "方案A"

    # L99 自身的共享引用指向自己方案 A 内的一个目录（同型号内引用）
    borrowed = scheme_root / "借用源"
    borrowed.mkdir(parents=True)
    (borrowed / "x.hex").write_text("x", encoding="utf-8")
    save_shared_module(
        model_root,
        SharedModuleRef(
            module_key="借用源",
            source_model_id="l99",
            source_group="l99",
            source_module="借用源",
            source_relative_path="L99程序/定制/方案A/借用源",
            mode="static",
        ),
    )

    result = delete_scheme(
        str(tmp_path), str(tmp_path), scheme_root, confirm_shared=False
    )
    assert result["ok"] is True
    assert not scheme_root.exists()


def test_delete_model_invalid_layout_rejected(tmp_path: Path) -> None:
    """MSC-011：delete_model 遇 invalid 布局须拒绝（D9 布局前置矩阵）。"""
    _create_model(tmp_path, "L99程序")
    (tmp_path / "杂物").mkdir()
    (tmp_path / "杂物" / "readme.txt").write_text("x", encoding="utf-8")
    assert detect_workspace_layout(tmp_path) == "invalid"
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=False
    )
    assert result["ok"] is False
    assert result["code"] == "layout_invalid"


def test_delete_model_blocking_issue_returns_lookup_blocked(tmp_path: Path) -> None:
    _create_model(tmp_path, "L99程序")
    _create_model(tmp_path, "L100程序", "单2D")
    # 损坏 L100 的 型号配置.toml（借入方配置损坏 → 阻断级 issue）
    (tmp_path / "L100程序" / "型号配置.toml").write_text("не valid toml [[[", encoding="utf-8")
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=False
    )
    assert result["ok"] is False
    assert result["code"] == "lookup_blocked"


def test_delete_model_undo_within_window_restores_content(tmp_path: Path) -> None:
    from fwasset.core.manifest import directory_manifest_hash

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    before_hash = directory_manifest_hash(model_root)
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    assert "回收站" in delete_result["message"]
    record_id = delete_result["payload"]["quarantine_record_id"]

    undo_result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert undo_result["ok"] is True
    assert model_root.is_dir()
    after_hash = directory_manifest_hash(model_root)
    assert before_hash == after_hash


def test_undo_after_window_expired_returns_undo_failed_and_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fwasset.core.quarantine as quarantine_module

    monkeypatch.setattr(quarantine_module, "RETENTION_SECONDS", 0.0)
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]

    import time

    time.sleep(0.05)
    undo_result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert undo_result["ok"] is False
    assert undo_result["code"] == "undo_failed"
    assert load_workspace_status(tmp_path).state == "clean"


def test_undo_manifest_error_returns_undo_failed_and_clean_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-008：隔离侧 manifest 计算失败 → undo_failed，clean，隔离记录仍在可重试。"""
    from fwasset.core.manifest import ManifestError
    from fwasset.core.services import model_scheme_service as svc

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]

    call_count = {"n": 0}
    original_manifest = svc.directory_manifest

    def _boom(root: object, **kwargs: object):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ManifestError("模拟 manifest 计算失败")
        return original_manifest(root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "directory_manifest", _boom)
    undo_result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert undo_result["ok"] is False
    assert undo_result["code"] == "undo_failed"
    assert load_workspace_status(tmp_path).state == "clean"

    # 隔离记录仍在，可重试撤销（第二次调用不再 mock，走真实路径）
    retry = undo_model_scheme_delete(str(tmp_path), record_id)
    assert retry["ok"] is True
    assert model_root.is_dir()


def test_undo_manifest_read_failure_returns_undo_failed_without_moved_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-015：``load_quarantine_manifest`` 纯读失败不得被外层误报为「已移动」。

    该读取发生在 ``begin_product_write()`` 之前，属零产物；必须在事务内
    单独捕获并空提交为 clean，返回 ``undo_failed``，消息中不得出现
    「已移动」字样（外层 ``except`` 的措辞是给「确认移动之后失败」用的）。
    """
    from fwasset.core.quarantine import QuarantineError
    from fwasset.core.services import model_scheme_service as svc

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]

    def _boom(workspace_root: object) -> None:
        raise QuarantineError("模拟隔离清单损坏")

    monkeypatch.setattr(svc, "load_quarantine_manifest", _boom)
    result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert result["ok"] is False
    assert result["code"] == "undo_failed"
    assert "已移动" not in result["message"]
    assert "recovery_required" not in result["payload"]
    assert load_workspace_status(tmp_path).state == "clean"


def test_undo_failure_after_move_keeps_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤销「移动之后」失败不得被空提交掩盖为 clean。

    ``quarantine.undo_delete`` 的 os.replace 失败（quarantine.py:460-461）与
    清单改写失败（:464）都抛 QuarantineError，但此时隔离内容已移回原路径，
    属**非零产物**分支。若一律 commit()，恢复流程会看到 clean 而漏掉半成品。

    MSC-009：``with`` 块以未提交状态退出、``__exit__`` 落
    ``recovery_required`` 后，本函数在外层捕获 ``QuarantineError`` 并转成
    ``ServiceResult``（不再让裸异常穿透 service 边界）。判据是
    ``quarantine_path`` 是否已消失（本次移动已发生），不是
    ``original_path.exists()``（可被外部程序在原路径新建同名目录误判）。
    """
    import os

    from fwasset.core.quarantine import QuarantineError, load_quarantine_manifest
    from fwasset.core.services import model_scheme_service as svc

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]

    def _move_then_fail(workspace_root: object, rid: object) -> None:
        # 模拟 :464 清单改写失败：目录已移回，随后抛错
        records = load_quarantine_manifest(tmp_path)
        record = next(r for r in records if r["id"] == rid)
        os.replace(Path(record["quarantine_path"]), Path(record["original_path"]))
        raise QuarantineError("模拟移动后清单改写失败")

    monkeypatch.setattr(svc, "undo_delete", _move_then_fail)
    result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert result["ok"] is False
    assert result["code"] == "undo_failed"
    assert result["payload"]["recovery_required"] is True

    # 目录已移回 = 有产物，工作区必须停在 recovery_required 待人工恢复
    assert model_root.is_dir()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_undo_success_and_conflict_generation_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤销成功 / UndoConflictError 两条路径的 generation 收束断言。

    MSC-013：只断言「最终 generation 为偶数」不足以捕获 MSC-005 回归——
    把 ``begin_product_write()`` 整段删掉，撤销全程 generation 停在偶数，
    这里的断言原样照样通过。改为跟踪 ``begin_product_write`` 是否被真实
    调用，直接锁定该回归。
    """
    from fwasset.core.workspace_transaction import WorkspaceTransaction

    calls: list[int] = []
    original_begin = WorkspaceTransaction.begin_product_write

    def _tracking_begin(self: WorkspaceTransaction) -> None:
        calls.append(1)
        original_begin(self)

    monkeypatch.setattr(WorkspaceTransaction, "begin_product_write", _tracking_begin)

    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]

    # 撤销前先在原位置占用目标路径，制造 UndoConflictError
    model_root.mkdir()
    calls.clear()
    conflict_result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert conflict_result["ok"] is False
    assert conflict_result["code"] == "undo_conflict"
    assert calls, "UndoConflictError 分支必须先 begin_product_write() 才能 commit() 收敛"
    status_after_conflict = load_workspace_status(tmp_path)
    assert status_after_conflict.state == "clean"
    assert status_after_conflict.generation % 2 == 0

    # 移除占用后正常撤销成功
    model_root.rmdir()
    calls.clear()
    success_result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert success_result["ok"] is True
    assert calls, "撤销成功路径必须调用 begin_product_write()（MSC-005）"
    status_after_success = load_workspace_status(tmp_path)
    assert status_after_success.state == "clean"
    assert status_after_success.generation % 2 == 0


def test_undo_occupied_target_returns_undo_conflict_and_retains_quarantine(
    tmp_path: Path,
) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    delete_result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    record_id = delete_result["payload"]["quarantine_record_id"]
    model_root.mkdir()

    result = undo_model_scheme_delete(str(tmp_path), record_id)
    assert result["ok"] is False
    assert result["code"] == "undo_conflict"

    from fwasset.core.quarantine import load_quarantine_manifest

    records = load_quarantine_manifest(tmp_path)
    assert any(r["id"] == record_id for r in records)


def test_delete_model_single_model_layout_rejected(tmp_path: Path) -> None:
    from fwasset.core.model_config import save_model_id

    save_model_id(tmp_path, "legacy")
    assert detect_workspace_layout(tmp_path) == "single_model"
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path, confirm_shared=False
    )
    assert result["ok"] is False
    assert result["code"] == "migration_required"


# ---------------------------------------------------------------------------
# D9 验收断言（父规格 D9 + 本规格「D9 验收断言清单」）
# ---------------------------------------------------------------------------


def test_blank_model_visible_without_assets(tmp_path: Path) -> None:
    _create_model(tmp_path)
    roots = enumerate_model_roots(tmp_path)
    assert len(roots) == 1


def test_delete_last_model_keeps_root_and_undoable(tmp_path: Path) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    result = delete_model(
        str(tmp_path), str(tmp_path), model_root, confirm_shared=False
    )
    assert result["ok"] is True
    assert tmp_path.is_dir()
    assert detect_workspace_layout(tmp_path) == "empty"
    record_id = result["payload"]["quarantine_record_id"]
    undo = undo_model_scheme_delete(str(tmp_path), record_id)
    assert undo["ok"] is True
    assert model_root.is_dir()
    assert detect_workspace_layout(tmp_path) == "multi_model"


def test_multi_model_workspace_with_single_model_allows_full_crud(tmp_path: Path) -> None:
    _create_model(tmp_path)
    assert detect_workspace_layout(tmp_path) == "multi_model"
    rn0 = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99预改名")
    assert rn0["ok"] is True
    assert (tmp_path / "L99预改名").is_dir()
    r2 = create_model(str(tmp_path), str(tmp_path), "L100程序", "单2D")  # type: ignore[arg-type]
    assert r2["ok"] is True
    rn = rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99预改名", "L99改名")
    assert rn["ok"] is True
    dl = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L100程序", confirm_shared=False
    )
    assert dl["ok"] is True


def test_no_other_model_precondition(tmp_path: Path) -> None:
    _create_model(tmp_path, "L99程序")
    _create_model(tmp_path, "L100程序", "单2D")
    dl = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L100程序", confirm_shared=False
    )
    assert dl["ok"] is True
    roots = [r for r in enumerate_model_roots(tmp_path) if r.name != "L99程序"]
    assert roots == []


# ---------------------------------------------------------------------------
# MSC-001 回归：无产品写入的校验失败不得落 recovery_required
# ---------------------------------------------------------------------------


def test_msc001_create_model_failure_keeps_workspace_clean(tmp_path: Path) -> None:
    _create_model(tmp_path)
    result = _create_model(tmp_path)
    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert load_workspace_status(tmp_path).state == "clean"


def test_msc001_create_scheme_failure_keeps_workspace_clean(tmp_path: Path) -> None:
    _create_model(tmp_path)
    model_root = tmp_path / "L99程序"
    create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    result = create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert load_workspace_status(tmp_path).state == "clean"


def test_msc001_rename_invalid_name_keeps_workspace_clean(tmp_path: Path) -> None:
    _create_model(tmp_path)
    result = rename_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", "非法<名称"
    )
    assert result["ok"] is False
    assert result["code"] == "invalid_name"
    assert load_workspace_status(tmp_path).state == "clean"


def test_msc001_delete_confirmation_required_keeps_workspace_clean(
    tmp_path: Path,
) -> None:
    from fwasset.core.model_config import SharedModuleRef, save_shared_module

    _create_model(tmp_path, "L99程序")
    _create_model(tmp_path, "L100程序", "单2D")
    common = tmp_path / "L99程序" / "通用" / "快捷键程序"
    common.mkdir(parents=True)
    (common / "key.hex").write_text("x", encoding="utf-8")
    save_shared_module(
        tmp_path / "L100程序",
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l99",
            source_group="l99",
            source_module="快捷键程序",
            source_relative_path="L99程序/通用/快捷键程序",
            mode="static",
        ),
    )
    result = delete_model(
        str(tmp_path), str(tmp_path), tmp_path / "L99程序", confirm_shared=False
    )
    assert result["ok"] is False
    assert result["code"] == "confirmation_required"
    assert load_workspace_status(tmp_path).state == "clean"


# ---------------------------------------------------------------------------
# MSC-017 回归：out_of_workspace 是 validate_new_path 的第一道检查，须登记并透传
# ---------------------------------------------------------------------------


def test_msc017_create_model_out_of_workspace_rejected_zero_write(
    tmp_path: Path,
) -> None:
    """MSC-017：型号名含 ``..`` 逃逸出工作区 → out_of_workspace，零写且保持 clean。

    ``assert_within_workspace`` 是 ``validate_new_path`` 的第一步（先于
    ``_assert_target_absent``），此时尚未 ``begin_product_write()``，属零产物失败。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    result = create_model(str(ws), str(ws), "../outside/L99程序", "单3D")  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["code"] == "out_of_workspace"
    assert not (outside / "L99程序").exists()
    assert load_workspace_status(ws).state == "clean"


def test_msc017_create_scheme_out_of_workspace_model_root_rejected(
    tmp_path: Path,
) -> None:
    """MSC-017：型号根落在工作区外 → out_of_workspace，不在外部目录落盘。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # 在工作区外单独建一个合法型号根，使其能过 _has_model_marker 而止步于边界检查
    create_model(str(outside), str(outside), "L99程序", "单3D")  # type: ignore[arg-type]
    foreign_root = outside / "L99程序"

    result = create_scheme(str(ws), str(ws), str(foreign_root), "方案A")

    assert result["ok"] is False
    assert result["code"] == "out_of_workspace"
    assert not (foreign_root / "定制" / "方案A").exists()
    assert load_workspace_status(ws).state == "clean"


def test_msc017_rename_model_out_of_workspace_new_name_rejected(
    tmp_path: Path,
) -> None:
    """MSC-017：重命名入口同样透传 out_of_workspace，且不移动原目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    create_model(str(ws), str(ws), "L99程序", "单3D")  # type: ignore[arg-type]

    result = rename_model(str(ws), str(ws), ws / "L99程序", "../outside/L98程序")

    assert result["ok"] is False
    assert result["code"] == "out_of_workspace"
    assert (ws / "L99程序").is_dir()
    assert not (outside / "L98程序").exists()
    assert load_workspace_status(ws).state == "clean"


# ---------------------------------------------------------------------------
# 中断恢复（阶段边界测试，非逐行崩溃注入）
# ---------------------------------------------------------------------------


def test_crash_before_promote_leaves_recovery_required_and_no_model_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import model_scheme_service as svc

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟进程退出（不调用 commit）")

    monkeypatch.setattr(svc, "promote_staging", _boom)
    with pytest.raises(RuntimeError):
        # promote_staging 被替换为抛出未捕获类型的异常，绕开 StagingError 分支，
        # 模拟 commit() 之前进程整体退出。
        _create_model(tmp_path)

    status = load_workspace_status(tmp_path)
    assert status.state == "recovery_required"
    assert not (tmp_path / "L99程序").exists()


def test_rename_model_rollback_conflict_leaves_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_model(tmp_path)

    def _fake_apply(plan: object, configured_root: object, log_fn: object = print):
        return {
            "ok": False,
            "code": "rollback_conflict",
            "message": "模拟冲突",
            "payload": {"applied": [], "rolled_back": [], "rollback_conflict": [{"path": "x"}], "failed": []},
        }

    from fwasset.core.services import model_scheme_service as svc

    monkeypatch.setattr(svc, "apply_rewrite_plan", _fake_apply)
    rename_model(str(tmp_path), str(tmp_path), tmp_path / "L99程序", "L99新名")
    status = load_workspace_status(tmp_path)
    assert status.state == "recovery_required"


def test_crash_after_promote_before_index_recovery_required_and_reindex_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSC-016：提升成功、索引对账之前中断的崩溃恢复契约（规格测试计划「中断恢复」第二条）。

    中断点位于 ``promote_staging`` 成功之后、``bulk_reindex_subtree`` 之前，
    用 ``KeyboardInterrupt`` 模拟（不被服务的 ``except Exception`` 捕获，
    ``with`` 块未提交退出，``__exit__`` 落 ``recovery_required``）。断言：
    ① 磁盘侧型号目录已存在且骨架完整；② 状态为 ``recovery_required``；
    ③ 预置一条型号边界内的陈旧索引行，补一次 ``bulk_reindex_subtree(..., [])``
    后被清除——「补一次索引对账即可收敛」这一恢复契约被真正锁定。自动恢复
    编排属子任务 8，不在本测试范围。
    """
    from fwasset.core.asset_index import query_assets, save_assets
    from fwasset.core.services import model_scheme_service as svc
    from fwasset.tests.test_asset_index import make_asset

    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = tmp_path / "fwasset.db"
    monkeypatch.setattr("fwasset.core.asset_index.ASSET_INDEX_PATH", db_path)

    # 预置一条「型号边界内的陈旧行」：模拟索引里残留的旧状态，恢复动作
    # 的 bulk_reindex_subtree(..., []) 应在边界对账时清除它。
    model_root = ws / "L99程序"
    stale = make_asset(model_root, directory_name="旧程序")
    save_assets([stale], str(ws), db_path, scanned_at=1000.0)
    assert any(str(model_root) in a["path"] for a in query_assets(path=db_path))

    original = svc.bulk_reindex_subtree

    def _interrupt(workspace_root: str, subtree_root: str, *args: object) -> None:
        raise KeyboardInterrupt("模拟提升后、索引前进程退出")

    monkeypatch.setattr(svc, "bulk_reindex_subtree", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        _create_model(ws)

    assert model_root.is_dir()
    assert (model_root / "通用").is_dir()
    assert (model_root / "定制").is_dir()
    assert load_workspace_status(ws).state == "recovery_required"

    # 恢复动作：补一次索引对账即可收敛（幂等重放，规格「崩溃恢复」节）。
    monkeypatch.setattr(svc, "bulk_reindex_subtree", original)
    original(str(ws), str(model_root), [])
    assert not any(
        str(model_root) in a["path"] for a in query_assets(path=db_path)
    )
