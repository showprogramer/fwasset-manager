"""R8 级联改写与迁移测试：操作矩阵、CAS 回滚、阻止码、幂等。"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest as _pytest

from fwasset.core.config_io import atomic_write_text
from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    load_shared_modules,
    save_model_id,
    save_shared_module,
)
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.services.reference_service import (
    RewritePlan,
    apply_rewrite_plan,
    build_clear_defaults_plan,
    build_rewrite_plan,
    migrate_follow_default_refs,
)
from fwasset.core.shared_module_resolver import resolve_shared_module
from fwasset.core.types import ReferenceSemantics, RewriteRequest


def _write(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """L36（默认借用来源）/L50（借入方）双型号工作区。"""
    root = tmp_path / "ws"
    l36 = root / "L36程序"
    l50 = root / "L50程序"
    l36.mkdir(parents=True)
    l50.mkdir(parents=True)
    save_model_id(l36, "l36")
    save_model_id(l50, "l50")
    save_platform_config(
        l36,
        [PlatformDefaults("标准单机芯3D", {"快捷键程序": "贝乐", "主板程序": "v1"})],
    )
    _write(l36 / "通用" / "快捷键" / "贝乐" / "key.hex")
    _write(l36 / "通用" / "快捷键" / "量产_默认" / "key.hex")
    _write(l36 / "通用" / "主板程序" / "v1" / "rom.bin")
    _write(l50 / "通用" / "主板程序" / "v9" / "rom.bin")
    return root


def _borrow(
    target_root: Path,
    key: str,
    source_rel: str,
    mode: str = "static",
    sid: str = "l36",
    source_platform: str = "",
) -> None:
    save_shared_module(
        target_root,
        SharedModuleRef(
            module_key=key,
            source_model_id=sid,
            source_group=sid,
            source_module=key,
            source_relative_path=source_rel,
            mode=mode,  # type: ignore[arg-type]
            source_platform=source_platform,
        ),
    )


def _plan(cfg: Path, ws: Path, request: RewriteRequest):
    res = build_rewrite_plan(str(cfg), str(ws), request)
    return res


# ---------------------------------------------------------------------------
# rename 矩阵
# ---------------------------------------------------------------------------


def test_rename_asset_rewrites_static_and_defaults(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    # 规格顺序：build（preimage 采集）→ 文件动作 → apply
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is True
    plan = res["payload"]["plan"]
    assert any(f.kind == "model_config" for f in plan.files)
    assert any(f.kind == "platform_config" for f in plan.files)
    old.rename(new)
    assert apply_rewrite_plan(plan, str(ws))["ok"] is True
    from fwasset.core.model_config import load_shared_modules
    from fwasset.core.platform_config import load_platform_config

    l50_refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert l50_refs["快捷键程序"].source_relative_path == "L36程序/通用/快捷键/贝乐改"
    # defaults value 同步
    blocks = load_platform_config(ws / "L36程序")
    assert blocks[0].defaults["快捷键程序"] == "贝乐改"
    # 新路径解析命中
    resolution = resolve_shared_module(l50_refs["快捷键程序"], ws)
    assert resolution.status == "hit"


def test_rename_module_rewrites_key_and_refs(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键"
    new = ws / "L36程序" / "通用" / "快捷按键"  # alias：canonical 保持「快捷键程序」
    req = RewriteRequest(
        operation="rename", target_kind="module", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is True
    plan = res["payload"]["plan"]
    old.rename(new)
    assert apply_rewrite_plan(plan, str(ws))["ok"] is True
    from fwasset.core.model_config import load_shared_modules
    from fwasset.core.platform_config import load_platform_config

    l50_refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert l50_refs["快捷键程序"].source_relative_path == (
        "L36程序/通用/快捷按键/贝乐"
    )
    blocks = load_platform_config(ws / "L36程序")
    # 别名改名 → canonical 键保持不变（规则 4 字段矩阵）
    assert "快捷键程序" in blocks[0].defaults
    assert blocks[0].defaults["快捷键程序"] == "贝乐"


def test_rename_module_cross_canonical_blocked(ws: Path):
    """模块改名导致 canonical 变化 → unsupported_semantic_change。"""
    old = ws / "L36程序" / "通用" / "快捷键"
    new = ws / "L36程序" / "通用" / "主板程序目录"
    req = RewriteRequest(
        operation="rename", target_kind="module", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "unsupported_semantic_change"


def test_rename_model_keeps_id_and_group(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序"
    new = ws / "L36改名程序"
    req = RewriteRequest(
        operation="rename", target_kind="model", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is True
    plan = res["payload"]["plan"]
    old.rename(new)
    assert apply_rewrite_plan(plan, str(ws))["ok"] is True
    from fwasset.core.model_config import load_model_config, load_shared_modules

    l50_refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert l50_refs["快捷键程序"].source_relative_path == "L36改名程序/通用/快捷键/贝乐"
    assert l50_refs["快捷键程序"].source_model_id == "l36"
    assert l50_refs["快捷键程序"].source_group == "l36"
    # 被改名型号自身配置随目录移动且仍可读
    assert load_model_config(ws / "L36改名程序")[0] == "l36"


def test_rename_identity_equal_blocked(ws: Path):
    # ASCII 大小写变体：Windows 下指向同一物理目录（normcase 归一后相等）
    old = ws / "L36程序" / "通用" / "主板程序" / "v1"
    req = RewriteRequest(
        operation="rename",
        target_kind="asset",
        old_path=str(old),
        new_path=str(ws / "L36程序" / "通用" / "主板程序" / "V1"),
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "invalid_operation"


def test_single_model_root_rename_unsupported(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    save_model_id(root, "l36")
    req = RewriteRequest(
        operation="rename",
        target_kind="model",
        old_path=str(root),
        new_path=str(tmp_path / "ws2"),
    )
    res = _plan(root, root, req)
    assert res["ok"] is False and res["code"] == "root_rename_unsupported"


# ---------------------------------------------------------------------------
# update 矩阵
# ---------------------------------------------------------------------------


def _sem(model_id: str, module_key: str, scheme: str = "") -> ReferenceSemantics:
    return ReferenceSemantics(
        model_id=model_id, module_key=module_key, source_group=model_id, scheme_name=scheme
    )


def test_update_moves_follow_asset_and_defaults_only(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐", mode="follow_asset")
    _borrow(ws / "L50程序", "主板程序", "L36程序/通用/主板程序/v1", mode="static")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    replacement = ws / "L36程序" / "通用" / "快捷键" / "贝乐v2"
    _write(replacement / "key.hex")
    req = RewriteRequest(
        operation="update",
        target_kind="asset",
        old_path=str(old),
        replacement_path=str(replacement),
        old_semantics=_sem("l36", "快捷键程序"),
        new_semantics=_sem("l36", "快捷键程序"),
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is True
    assert apply_rewrite_plan(res["payload"]["plan"], str(ws))["ok"] is True
    from fwasset.core.model_config import load_shared_modules
    from fwasset.core.platform_config import load_platform_config

    l50_refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert l50_refs["快捷键程序"].source_relative_path == "L36程序/通用/快捷键/贝乐v2"
    # static 不改写（固定旧程序，旧目录已被 CRUD 移除/回收后自然 missing）
    assert l50_refs["主板程序"].source_relative_path == "L36程序/通用/主板程序/v1"
    # defaults 跟随 replacement
    blocks = load_platform_config(ws / "L36程序")
    assert blocks[0].defaults["快捷键程序"] == "贝乐v2"
    assert blocks[0].defaults["主板程序"] == "v1"


def test_update_overlap_with_static_anchor_blocked(ws: Path):
    """旧模块叶子是 static 锚点、replacement 是其子变体 → 双向重叠阻止。"""
    _borrow(ws / "L50程序", "主板程序", "L36程序/通用/主板程序/v1", mode="static")
    old = ws / "L36程序" / "通用" / "主板程序" / "v1"
    replacement = ws / "L36程序" / "通用" / "主板程序" / "v1" / "新变体"
    _write(replacement / "rom.bin")
    req = RewriteRequest(
        operation="update",
        target_kind="asset",
        old_path=str(old),
        replacement_path=str(replacement),
        old_semantics=_sem("l36", "主板程序"),
        new_semantics=_sem("l36", "主板程序"),
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "invalid_operation"


def test_update_semantic_change_blocked(ws: Path):
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    replacement = ws / "L36程序" / "通用" / "主板程序" / "v1b"
    _write(replacement / "rom.bin")
    req = RewriteRequest(
        operation="update",
        target_kind="asset",
        old_path=str(old),
        replacement_path=str(replacement),
        old_semantics=_sem("l36", "快捷键程序"),
        new_semantics=_sem("l36", "主板程序"),
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "unsupported_semantic_change"


def test_update_forged_old_semantics_invalid_request(ws: Path):
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    replacement = ws / "L36程序" / "通用" / "快捷键" / "贝乐v2"
    _write(replacement / "key.hex")
    req = RewriteRequest(
        operation="update",
        target_kind="asset",
        old_path=str(old),
        replacement_path=str(replacement),
        # 伪造旧语义：把快捷键伪装成主板程序，企图绕过类型校验
        old_semantics=_sem("l36", "主板程序"),
        new_semantics=_sem("l36", "主板程序"),
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# gate / stale / 阻止
# ---------------------------------------------------------------------------


def test_build_not_configured(ws: Path):
    req = RewriteRequest(
        operation="rename",
        target_kind="asset",
        old_path=str(ws / "L36程序" / "通用" / "快捷键" / "贝乐"),
        new_path=str(ws / "L36程序" / "通用" / "快捷键" / "b2"),
    )
    res = build_rewrite_plan(None, str(ws), req)
    assert res["ok"] is False and res["code"] == "not_configured"


def test_build_root_changed(ws: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    req = RewriteRequest(
        operation="rename",
        target_kind="asset",
        old_path=str(ws / "L36程序" / "通用" / "快捷键" / "贝乐"),
        new_path=str(ws / "L36程序" / "通用" / "快捷键" / "b2"),
    )
    res = build_rewrite_plan(str(other), str(ws), req)
    assert res["ok"] is False and res["code"] == "root_changed"


def test_apply_rejects_changed_configured_root(ws: Path):
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is True
    plan = res["payload"]["plan"]
    old.rename(new)
    other = ws.parent / "other-configured"
    other.mkdir()
    res2 = apply_rewrite_plan(plan, str(other))
    assert res2["ok"] is False and res2["code"] == "root_changed"


def test_stale_plan_file_modified(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    plan = res["payload"]["plan"]
    old.rename(new)
    # 文件动作后、apply 前第三方修改了平台配置
    (ws / "L36程序" / "平台配置.toml").write_text("# tampered\n", encoding="utf-8")
    res2 = apply_rewrite_plan(plan, str(ws))
    assert res2["ok"] is False and res2["code"] == "stale_plan"


def test_canonical_conflict_blocks(ws: Path):
    save_platform_config(
        ws / "L36程序",
        [
            PlatformDefaults(
                "标准单机芯3D", {"快捷键程序": "贝乐", "快捷按键": "量产_默认"}
            )
        ],
    )
    old = ws / "L36程序" / "通用" / "快捷键"
    new = ws / "L36程序" / "通用" / "快捷键-旋钮"  # 同 canonical 别名
    req = RewriteRequest(
        operation="rename", target_kind="module", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    assert res["ok"] is False and res["code"] == "canonical_conflict"


# ---------------------------------------------------------------------------
# CAS 回滚
# ---------------------------------------------------------------------------


def test_apply_rollback_restores_bytes(ws: Path, monkeypatch: _pytest.MonkeyPatch):
    """第二个文件写失败 → 第一个文件按 preimage 字节完全恢复。"""
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = _plan(ws, ws, req)
    plan = res["payload"]["plan"]
    assert len(plan.files) >= 2
    old.rename(new)

    real_atomic = atomic_write_text
    calls = {"n": 0}

    def flaky(path: Path, content: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("boom")
        real_atomic(path, content)

    monkeypatch.setattr(
        "fwasset.core.services.reference_service.atomic_write_text", flaky
    )
    res2 = apply_rewrite_plan(plan, str(ws))
    assert res2["ok"] is False
    assert res2["code"] in ("rolled_back", "rollback_conflict")
    assert res2["payload"]["rolled_back"], res2["payload"]
    if res2["code"] == "rolled_back":
        rolled_first = plan.files[0]
        assert Path(rolled_first.pre_path).read_bytes() == rolled_first.original_bytes


def test_apply_rollback_conflict_keeps_concurrent_change(
    ws: Path, monkeypatch: _pytest.MonkeyPatch
):
    """首文件写后被第三方修改、第二文件失败 → rollback_conflict 保留并发内容。"""
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    plan = _plan(ws, ws, req)["payload"]["plan"]
    assert len(plan.files) >= 2
    old.rename(new)

    real_atomic = atomic_write_text
    calls = {"n": 0}

    def flaky_then_tamper(path: Path, content: str) -> None:
        calls["n"] += 1
        real_atomic(path, content)
        if calls["n"] == 1:
            # 模拟第三方在首文件写入后立即修改
            path.write_text("# concurrent\n", encoding="utf-8")
        if calls["n"] == 2:
            raise OSError("boom")

    monkeypatch.setattr(
        "fwasset.core.services.reference_service.atomic_write_text", flaky_then_tamper
    )
    res = apply_rewrite_plan(plan, str(ws))
    assert res["ok"] is False and res["code"] == "rollback_conflict"
    conflicts = res["payload"]["rollback_conflict"]
    assert conflicts and conflicts[0]["preimage"]
    # 并发修改内容被保留
    assert plan.files[0].post_path.read_text(encoding="utf-8").startswith("# concurrent")


# ---------------------------------------------------------------------------
# follow_default 迁移
# ---------------------------------------------------------------------------


def test_migrate_converts_follow_default_to_follow_asset(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
        source_platform="标准单机芯3D",
    )
    res = migrate_follow_default_refs(str(ws), str(ws))
    assert res["ok"] is True
    assert res["payload"]["converted"] == 1
    from fwasset.core.model_config import load_shared_modules

    refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    ref = refs["快捷键程序"]
    assert ref.mode == "follow_asset"
    assert ref.source_relative_path == "L36程序/通用/快捷键/贝乐"
    assert ref.source_platform == ""
    # 迁移后解析仍命中
    assert resolve_shared_module(ref, ws).status == "hit"
    # TOML 落盘内容：mode=follow_asset、无 source_platform
    text = (ws / "L50程序" / MODEL_CONFIG_FILENAME).read_text(encoding="utf-8")
    assert 'mode = "follow_asset"' in text
    assert "source_platform" not in text


def test_migrate_keeps_unresolvable_entry(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/不存在模块",
        mode="follow_default",
    )
    res = migrate_follow_default_refs(str(ws), str(ws))
    assert res["ok"] is True
    assert res["payload"]["converted"] == 0 and res["payload"]["kept"] == 1
    from fwasset.core.model_config import load_shared_modules

    ref = load_shared_modules(ws / "L50程序")[0]
    assert ref.mode == "follow_default"


def test_migrate_idempotent_second_run(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
    )
    first = migrate_follow_default_refs(str(ws), str(ws))
    assert first["ok"] is True
    second = migrate_follow_default_refs(str(ws), str(ws))
    assert second["payload"]["converted"] == 0
    assert second["payload"]["changed"] == []


def test_migrate_damaged_borrower_failed(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
    )
    (ws / "L50程序" / MODEL_CONFIG_FILENAME).write_text("[[broken\n", encoding="utf-8")
    res = migrate_follow_default_refs(str(ws), str(ws))
    assert res["ok"] is False and res["code"] == "migrate_failed"
    assert res["payload"]["failed"] and "parse_error" in res["payload"]["failed"][0]["reason"]


def test_migrate_gate(ws: Path):
    res = migrate_follow_default_refs(None, str(ws))
    assert res["ok"] is False and res["code"] == "not_configured"


# ---------------------------------------------------------------------------
# plan 校验：伪造 / 篡改 → invalid_plan 零写
# ---------------------------------------------------------------------------


def test_forged_plan_rejected(ws: Path):
    from fwasset.core.services.reference_service import (
        FileRewrite,
        _sha256,
    )

    victim = ws / "L50程序" / MODEL_CONFIG_FILENAME
    plan = RewritePlan(
        configured_root=str(ws),
        workspace_root=str(ws),
        request=RewriteRequest(
            operation="rename",
            target_kind="asset",
            old_path=str(ws / "L36程序" / "通用" / "快捷键" / "贝乐"),
            new_path=str(ws / "L36程序" / "通用" / "快捷键" / "b2"),
        ),
        files=[
            FileRewrite(
                kind="model_config",
                pre_path=victim,
                post_path=victim,
                owner_root=str(ws / "L50程序"),
                original_bytes=victim.read_bytes(),
                original_sha256=_sha256(victim.read_bytes()),
                new_content="# forged\n",
                new_sha256=_sha256(b"# forged\n"),
            )
        ],
    )
    res = apply_rewrite_plan(plan, str(ws))
    assert res["ok"] is False and res["code"] == "invalid_plan"
    # 文件未被篡改
    assert victim.read_text(encoding="utf-8") != "# forged\n"


def test_tampered_new_content_rejected(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    plan = build_rewrite_plan(str(ws), str(ws), req)["payload"]["plan"]
    plan.files[0].new_content = "# tampered\n"
    res = apply_rewrite_plan(plan, str(ws))
    assert res["ok"] is False and res["code"] == "invalid_plan"


def test_model_rename_rollback_restores_post_path(
    ws: Path, monkeypatch: pytest.MonkeyPatch
):
    # 自引用：L36 借用自己的程序，使型号自身配置进入计划
    _borrow(ws / "L36程序", "快捷键程序", "L36程序/通用/快捷键/贝乐", sid="l36")
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序"
    new = ws / "L36改名程序"
    req = RewriteRequest(
        operation="rename", target_kind="model", old_path=str(old), new_path=str(new)
    )
    plan = build_rewrite_plan(str(ws), str(ws), req)["payload"]["plan"]
    old.rename(new)

    real_atomic = atomic_write_text
    calls = {"n": 0}

    def flaky(path: Path, content: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("boom")
        real_atomic(path, content)

    monkeypatch.setattr(
        "fwasset.core.services.reference_service.atomic_write_text", flaky
    )
    res = apply_rewrite_plan(plan, str(ws))
    assert res["ok"] is False and res["code"] == "rolled_back"
    own = plan.files[0]
    assert own.pre_path != own.post_path
    # 恢复发生在当前物理位置（新根），旧根不重建
    assert own.post_path.read_bytes() == own.original_bytes
    assert not own.pre_path.exists()


# ---------------------------------------------------------------------------
# canonical 作用域与合并
# ---------------------------------------------------------------------------


def test_asset_level_canonical_conflict_blocks(ws: Path):
    """两个异值别名分别指向 A/B：操作 A 仍阻止（完整别名组参与判定）。"""
    save_platform_config(
        ws / "L36程序",
        [
            PlatformDefaults(
                "标准单机芯3D", {"快捷键程序": "贝乐", "快捷按键": "量产_默认"}
            )
        ],
    )
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is False and res["code"] == "canonical_conflict"


def test_same_value_alias_merged_to_single_canonical(ws: Path):
    save_platform_config(
        ws / "L36程序",
        [
            PlatformDefaults(
                "标准单机芯3D", {"快捷键程序": "贝乐", "快捷按键": "贝乐"}
            )
        ],
    )
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is True
    assert apply_rewrite_plan(res["payload"]["plan"], str(ws))["ok"] is True
    from fwasset.core.platform_config import load_platform_config

    defaults = load_platform_config(ws / "L36程序")[0].defaults
    assert list(defaults).count("快捷键程序") == 1
    assert "快捷按键" not in defaults
    assert defaults["快捷键程序"] == "贝乐改"


def test_unrelated_model_canonical_conflict_does_not_block(ws: Path):
    """无关型号的同 canonical 异值冲突不阻止本次命中其它型号的操作。"""
    save_platform_config(
        ws / "L50程序",
        [
            PlatformDefaults(
                "标准双2D", {"快捷键程序": "别的A", "快捷按键": "别的B"}
            )
        ],
    )
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is True


def test_unmatched_platform_block_untouched_by_dedupe(ws: Path):
    """块 A 命中并合并同值别名；块 B 未命中且含同值别名 → 键集合保持不变。"""
    save_platform_config(
        ws / "L36程序",
        [
            PlatformDefaults(
                "标准单机芯3D", {"快捷键程序": "贝乐", "快捷按键": "贝乐"}
            ),
            PlatformDefaults(
                "双2D", {"快捷键程序": "量产_默认", "快捷按键": "量产_默认"}
            ),
        ],
    )
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    new = ws / "L36程序" / "通用" / "快捷键" / "贝乐改"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is True
    assert apply_rewrite_plan(res["payload"]["plan"], str(ws))["ok"] is True
    from fwasset.core.platform_config import load_platform_config

    blocks = load_platform_config(ws / "L36程序")
    # 块 0（命中）：别名合并为唯一 canonical
    assert list(blocks[0].defaults) == ["快捷键程序"]
    assert blocks[0].defaults["快捷键程序"] == "贝乐改"
    # 块 1（未命中）：零改动
    assert list(blocks[1].defaults) == ["快捷键程序", "快捷按键"]
    assert blocks[1].defaults["快捷键程序"] == "量产_默认"
    assert blocks[1].defaults["快捷按键"] == "量产_默认"


# ---------------------------------------------------------------------------
# 迁移：来源阻断传播与继续处理
# ---------------------------------------------------------------------------


def test_migrate_source_blocked_skips_only_that_borrower(tmp_path: Path):
    root = tmp_path / "ws"
    src = root / "L36程序"
    b1 = root / "L50程序"
    b2 = root / "L60程序"
    src.mkdir(parents=True)
    b1.mkdir()
    b2.mkdir()
    save_model_id(src, "l36")
    save_model_id(b1, "l50")
    save_model_id(b2, "l60")
    _write(src / "通用" / "主板程序" / "v1" / "rom.bin")
    _borrow(b1, "主板程序", "L36程序/通用/主板程序", mode="follow_default")
    _borrow(b2, "主板程序", "L36程序/通用/主板程序", mode="follow_default")
    # 来源型号配置损坏 → b1、b2 的来源均为损坏型号，双双计入 failed
    (src / MODEL_CONFIG_FILENAME).write_text("[[broken\n", encoding="utf-8")
    res = migrate_follow_default_refs(str(root), str(root))
    assert res["ok"] is False
    assert res["payload"]["converted"] == 0
    # src 自身 + 两个借入方（来源侧阻断传播）各计一条 failed
    assert len(res["payload"]["failed"]) == 3
    assert res["payload"]["failed"][0]["reason"].startswith("model_config_parse_error")


def test_migrate_source_blocked_continues_other_borrower(tmp_path: Path):
    """来源侧阻断只跳过当前借入方：来源损坏但另一借入方条目可解析。"""
    root = tmp_path / "ws"
    src = root / "L36程序"
    b1 = root / "L50程序"
    b2 = root / "L60程序"
    src.mkdir(parents=True)
    b1.mkdir()
    b2.mkdir()
    save_model_id(src, "l36")
    save_model_id(b1, "l50")
    save_model_id(b2, "l60")
    _write(src / "通用" / "主板程序" / "v1" / "rom.bin")
    _write(src / "通用" / "快捷键" / "贝乐" / "k.hex")
    _borrow(b1, "主板程序", "L36程序/通用/主板程序", mode="follow_default")
    # b2 借用「快捷键程序」，其来源是另一个正常型号
    other = root / "L70程序"
    other.mkdir()
    save_model_id(other, "l70")
    _write(other / "通用" / "快捷键" / "量产_默认" / "k.hex")
    save_platform_config(other, [PlatformDefaults("标准单机芯3D", {"快捷键程序": "量产_默认"})])
    _borrow(b2, "快捷键程序", "L70程序/通用/快捷键", mode="follow_default", sid="l70")
    # 破坏 src（b1 的来源）
    (src / MODEL_CONFIG_FILENAME).write_text("[[broken\n", encoding="utf-8")
    res = migrate_follow_default_refs(str(root), str(root))
    assert res["ok"] is False
    # b2 迁移成功
    assert res["payload"]["converted"] == 1
    assert Path(res["payload"]["changed"][0]).name == "L60程序"


def test_migrate_unresolved_reported_per_entry(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/不存在模块",
        mode="follow_default",
    )
    res = migrate_follow_default_refs(str(ws), str(ws))
    assert res["payload"]["unresolved"], res["payload"]
    entry = res["payload"]["unresolved"][0]
    assert entry["raw_key"] == "快捷键程序"
    assert entry["reason"]


def test_single_model_layout_resolver_and_migrate(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    save_model_id(root, "l36")
    _write(root / "通用" / "主板程序" / "v1" / "rom.bin")
    save_platform_config(root, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    ref = SharedModuleRef(
        module_key="主板程序",
        source_model_id="l36",
        source_group="l36",
        source_module="主板程序",
        source_relative_path="通用/主板程序",
        mode="follow_default",
    )
    save_shared_module(root, ref)
    # 单型号布局：首段 通用 → source root = 工作区根
    assert resolve_shared_module(ref, root).status == "hit"
    res = migrate_follow_default_refs(str(root), str(root))
    assert res["ok"] is True and res["payload"]["converted"] == 1
    refs = {r.module_key: r for r in load_shared_modules(root)}
    assert refs["主板程序"].mode == "follow_asset"
    assert refs["主板程序"].source_relative_path == "通用/主板程序/v1"
    assert resolve_shared_module(refs["主板程序"], root).status == "hit"


# ---------------------------------------------------------------------------
# junction 与引用改写
# ---------------------------------------------------------------------------


def test_rename_rewrites_case_variant_ref(ws: Path):
    """存量引用大小写与磁盘目录不一致（normcase 身份命中）：rename 后精确落新路径。"""
    _borrow(ws / "L50程序", "主板程序", "L36程序/通用/主板程序/V1")  # 磁盘目录为 v1
    old = ws / "L36程序" / "通用" / "主板程序" / "v1"
    new = ws / "L36程序" / "通用" / "主板程序" / "v2"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is True
    old.rename(new)
    assert apply_rewrite_plan(res["payload"]["plan"], str(ws))["ok"] is True
    refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert refs["主板程序"].source_relative_path == "L36程序/通用/主板程序/v2"
    assert resolve_shared_module(refs["主板程序"], ws).status == "hit"


def test_rename_rewrites_backslash_ref_via_serializer(ws: Path):
    """反斜杠引用（经 tomli-w 合法转义落盘）：rename 后统一归一为 ``/``。"""
    _borrow(ws / "L50程序", "主板程序", "L36程序\\通用\\主板程序\\v1")
    old = ws / "L36程序" / "通用" / "主板程序" / "v1"
    new = ws / "L36程序" / "通用" / "主板程序" / "v2"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is True
    old.rename(new)
    assert apply_rewrite_plan(res["payload"]["plan"], str(ws))["ok"] is True
    refs = {r.module_key: r for r in load_shared_modules(ws / "L50程序")}
    assert refs["主板程序"].source_relative_path == "L36程序/通用/主板程序/v2"
    assert resolve_shared_module(refs["主板程序"], ws).status == "hit"


def test_rename_unplaceable_ref_blocked(ws: Path, tmp_path: Path):
    """``..`` 构造的锚点被越界检查拦下（invalid_shared_entry → lookup_blocked）。"""
    _borrow(ws / "L50程序", "快捷键程序", "L50程序/../L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "主板程序" / "v1"
    new = ws / "L36程序" / "通用" / "主板程序" / "v2"
    req = RewriteRequest(
        operation="rename", target_kind="asset", old_path=str(old), new_path=str(new)
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is False and res["code"] == "lookup_blocked"
    issues = res["payload"]["issues"]
    assert any(i["category"] == "invalid_shared_entry" for i in issues)


def test_junction_anchor_update_overlap_blocked(ws: Path, tmp_path: Path):
    """junction 物理指向 static 锚点之内：update overlap 双向判定阻止。"""
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link = link_parent / "alias"
    real = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)],
        check=True,
        capture_output=True,
    )
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    replacement = link  # junction：词法在 old 外、物理 == 锚点
    req = RewriteRequest(
        operation="update",
        target_kind="asset",
        old_path=str(real),
        replacement_path=str(replacement),
        old_semantics=ReferenceSemantics("l36", "快捷键程序", "l36"),
        new_semantics=ReferenceSemantics("l36", "快捷键程序", "l36"),
    )
    res = build_rewrite_plan(str(ws), str(ws), req)
    assert res["ok"] is False and res["code"] == "invalid_operation"


# ---------------------------------------------------------------------------
# D1.3 build_clear_defaults_plan（子任务 6a）
# ---------------------------------------------------------------------------


def test_clear_defaults_plan_change_type_clears_hit_entry(ws: Path):
    """改类型：清除旧 canonical 键命中的 defaults，计划可直接 apply。"""
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "change_type")
    assert res["ok"] is True, res
    plan = res["payload"]["plan"]
    assert [f.kind for f in plan.files] == ["platform_config"]
    assert apply_rewrite_plan(plan, str(ws))["ok"] is True
    from fwasset.core.platform_config import load_platform_config

    defaults = load_platform_config(ws / "L36程序")[0].defaults
    assert "快捷键程序" not in defaults
    assert defaults["主板程序"] == "v1"  # 同块其他条目不受影响


def test_clear_defaults_plan_general_to_custom_also_clears(ws: Path):
    """通用转定制同样清除精确命中，避免留下已知悬空 default。"""
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "general_to_custom")
    assert res["ok"] is True, res
    assert len(res["payload"]["plan"].files) == 1


def test_clear_defaults_plan_custom_to_general_is_empty(ws: Path):
    """定制转通用不自动设默认，返回空计划。"""
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "custom_to_general")
    assert res["ok"] is True, res
    assert res["payload"]["plan"].files == []


def test_clear_defaults_plan_custom_scheme_move_is_empty(ws: Path):
    """定制内换方案不涉及 defaults（defaults 只指向通用区），空计划。"""
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "custom_scheme_move")
    assert res["ok"] is True, res
    assert res["payload"]["plan"].files == []


def test_clear_defaults_plan_rejects_missing_target(ws: Path):
    old = ws / "L36程序" / "通用" / "快捷键" / "不存在"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "change_type")
    assert res["ok"] is False and res["code"] == "invalid_target"


def test_clear_defaults_plan_rejects_incomplete_lookup(ws: Path):
    """反查阻断级 issue → 命中集不完整，拒绝签发计划。"""
    _borrow(ws / "L50程序", "快捷键程序", "L50程序/../L36程序/通用/快捷键/贝乐")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "change_type")
    assert res["ok"] is False and res["code"] == "reference_incomplete"


def test_clear_defaults_plan_signature_has_no_hits_parameter():
    """硬约束：builder 自行派生命中集，签名内不得出现 hits。"""
    import inspect

    assert "hits" not in inspect.signature(build_clear_defaults_plan).parameters


def test_clear_defaults_plan_token_passes_apply_validation(ws: Path):
    """复用既有 RewritePlan / _plan_token，而非平行实现。"""
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    plan = build_clear_defaults_plan(str(ws), str(ws), str(old), "change_type")["payload"]["plan"]
    assert isinstance(plan, RewritePlan)
    assert apply_rewrite_plan(plan, str(ws))["code"] != "invalid_plan"


def test_clear_defaults_plan_reports_parse_error(ws: Path):
    """平台配置严格读取失败 → config_parse_error，不签发计划。"""
    atomic_write_text(ws / "L36程序" / "平台配置.toml", "这不是合法 TOML = = =\n")
    old = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    res = build_clear_defaults_plan(str(ws), str(ws), str(old), "change_type")
    assert res["ok"] is False and res["code"] == "config_parse_error"
