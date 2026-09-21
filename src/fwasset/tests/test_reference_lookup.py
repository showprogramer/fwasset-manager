"""R8 反查测试：命中矩阵、kind 验证、悬空锚点、严格加载 issue。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    save_model_id,
    save_shared_module,
)
from fwasset.core.platform_config import (
    PlatformDefaults,
    canonical_module_dir,
    save_platform_config,
)
from fwasset.core.reference_lookup import (
    BLOCKING_ISSUE_CATEGORIES,
    _default_program_dir,
    check_reference_gate,
    find_dangling_anchors,
    find_references_to,
    owner_model_root_for,
)


def _write(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _result(payload: dict):
    return payload["result"]


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """多型号布局工作区：L36/L50 两型号，各含 id、平台配置与通用程序。"""
    root = tmp_path / "ws"
    l36 = root / "L36程序"
    l50 = root / "L50程序"
    l36.mkdir(parents=True)
    l50.mkdir(parents=True)
    save_model_id(l36, "l36")
    save_model_id(l50, "l50")
    save_platform_config(
        l36,
        [PlatformDefaults("标准单机芯3D", {"快捷键程序": "贝乐", "主板程序": "量产_默认"})],
    )
    _write(l36 / "通用" / "快捷键" / "贝乐" / "key.hex")
    _write(l36 / "通用" / "快捷键" / "量产_默认" / "key.hex")
    _write(l36 / "通用" / "主板程序" / "v1" / "rom.bin")
    _write(l50 / "通用" / "主板程序" / "v1" / "rom.bin")
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


# ---------------------------------------------------------------------------
# gate 与 kind 验证
# ---------------------------------------------------------------------------


def test_gate_not_configured(ws: Path):
    res = find_references_to(None, ws, ws / "L36程序", "model")
    assert res["ok"] is False and res["code"] == "not_configured"


def test_gate_root_changed(ws: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    res = find_references_to(str(other), ws, ws / "L36程序", "model")
    assert res["ok"] is False and res["code"] == "root_changed"


def test_check_reference_gate_ok(ws: Path):
    assert check_reference_gate(str(ws), ws) is None


def test_target_out_of_workspace(ws: Path):
    res = find_references_to(str(ws), ws, Path("C:/outside"), "asset")
    assert res["ok"] is False and res["code"] == "out_of_workspace"


def test_target_missing_is_invalid_target(ws: Path):
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "nope", "asset")
    assert res["ok"] is False and res["code"] == "invalid_target"


@pytest.mark.parametrize(
    ("target_rel", "kind"),
    [
        ("L36程序", "asset"),  # 型号根不是程序
        ("L36程序/通用/快捷键/贝乐", "module"),  # 程序不是模块
        ("L36程序/通用/快捷键/贝乐", "scheme"),  # 方案必须直接在定制下
        ("L36程序/通用", "model"),  # 无型号标志
    ],
)
def test_kind_mismatch_invalid_target(ws: Path, target_rel: str, kind: str):
    res = find_references_to(str(ws), ws, ws / target_rel, kind)  # type: ignore[arg-type]
    assert res["ok"] is False and res["code"] == "invalid_target"


def test_kind_module_ok(ws: Path):
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# 命中矩阵
# ---------------------------------------------------------------------------


def test_asset_hit_static_and_cross_not_hit(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    assert res["ok"] is True
    hits = _result(res["payload"]).hits
    assert any(h.kind == "shared_static" for h in hits)


def test_asset_same_module_ab_no_cross_hit(ws: Path):
    """默认=A 时：查 A 命中 follow_default、查同模块 B 不命中。"""
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
    )
    res_a = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    hits_a = [h for h in _result(res_a["payload"]).hits if h.kind == "shared_follow_default"]
    assert len(hits_a) == 1
    res_b = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "量产_默认", "asset"
    )
    hits_b = [h for h in _result(res_b["payload"]).hits if h.kind == "shared_follow_default"]
    assert hits_b == []


def test_module_hit_static_and_defaults(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    hits = _result(res["payload"]).hits
    kinds = {h.kind for h in hits}
    assert kinds == {"shared_static", "platform_default"}
    defaults_hit = next(h for h in hits if h.kind == "platform_default")
    assert defaults_hit.raw_key == "快捷键程序"
    assert defaults_hit.block_index == 0


def test_defaults_alias_key_hits_canonical_module(ws: Path, tmp_path: Path):
    """defaults 用别名键「快捷键」也能命中实际目录「快捷键」。"""
    l36 = ws / "L36程序"
    save_platform_config(l36, [PlatformDefaults("标准单机芯3D", {"快捷键": "贝乐"})])
    res = find_references_to(str(ws), ws, l36 / "通用" / "快捷键", "module")
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1 and hits[0].raw_key == "快捷键"


def test_scheme_subtree_hit(ws: Path):
    _write(ws / "L36程序" / "定制" / "以色列-Royal-Z9" / "主板程序" / "v2" / "rom.bin")
    _borrow(ws / "L50程序", "主板程序", "L36程序/定制/以色列-Royal-Z9/主板程序/v2")
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "定制" / "以色列-Royal-Z9", "scheme"
    )
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_static"]
    assert len(hits) == 1


def test_model_level_cross_model_ref_hit(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    res = find_references_to(str(ws), ws, ws / "L36程序", "model")
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_static"]
    assert len(hits) == 1


def test_model_level_own_defaults_not_hit(ws: Path):
    res = find_references_to(str(ws), ws, ws / "L36程序", "model")
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert hits == []


def test_multi_platform_blocks_precise(ws: Path):
    save_platform_config(
        ws / "L36程序",
        [
            PlatformDefaults("标准单机芯3D", {"主板程序": "v1"}),
            PlatformDefaults("双2D", {"主板程序": "v1"}),
        ],
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "主板程序", "module"
    )
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert {h.block_index for h in hits} == {0, 1}
    assert {h.platform_name for h in hits} == {"标准单机芯3D", "双2D"}


def test_single_model_root_layout(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    save_model_id(root, "l36")
    _write(root / "通用" / "快捷键" / "贝乐" / "k.hex")
    _borrow(root, "快捷键程序", "通用/快捷键/贝乐")
    res = find_references_to(
        str(root), root, root / "通用" / "快捷键" / "贝乐", "asset"
    )
    assert res["ok"] is True
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_static"]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# 严格加载 issue
# ---------------------------------------------------------------------------


def test_plain_dir_ignored_no_issue(ws: Path):
    (ws / "说明文档").mkdir()
    (ws / "工具").mkdir()
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    assert res["ok"] is True
    assert _result(res["payload"]).issues == []


def test_model_without_id_reports_missing_model_id(ws: Path):
    bare = ws / "L70程序"
    _write(bare / "通用" / "主板程序" / "v1" / "rom.bin")
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    issues = _result(res["payload"]).issues
    assert any(i.category == "missing_model_id" for i in issues)


def test_parse_error_reported_not_silent(ws: Path):
    bad = ws / "L50程序"
    (bad / "型号配置.toml").write_text("[[broken\n", encoding="utf-8")
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    issues = _result(res["payload"]).issues
    assert any(i.category == "model_config_parse_error" for i in issues)


def test_invalid_shared_entry_reported(ws: Path):
    cfg = ws / "L50程序" / "型号配置.toml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + '\n[shared_modules."主板程序"]\nsource_model_id = "l36"\n',
        encoding="utf-8",
    )
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    issues = _result(res["payload"]).issues
    assert any(i.category == "invalid_shared_entry" for i in issues)


def test_duplicate_model_id_reported(ws: Path):
    save_model_id(ws / "L50程序", "l36")  # 与 L36 重复
    res = find_references_to(str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module")
    issues = _result(res["payload"]).issues
    assert any(i.category == "duplicate_model_id" for i in issues)


# ---------------------------------------------------------------------------
# find_dangling_anchors
# ---------------------------------------------------------------------------


def test_anchor_shared_missing_path(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    (ws / "L36程序" / "通用" / "快捷键" / "贝乐").rename(
        ws / "L36程序" / "通用" / "快捷键" / "_gone"
    )
    res = find_dangling_anchors(str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐")
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_static"]
    assert len(hits) == 1


def test_anchor_platform_default_variant_reuse(ws: Path):
    """删除默认变体后复用该路径前仍命中 platform_default。"""
    victim = ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    victim.rename(ws / "_trash")
    res = find_dangling_anchors(str(ws), ws, victim)
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1 and hits[0].raw_value == "贝乐"


def test_anchor_platform_default_module_leaf_alias(tmp_path: Path):
    """模块叶子（value=""）整体删除后，用历史 alias 路径复用仍按 canonical 命中。"""
    root = tmp_path / "ws"
    model = root / "L36程序"
    model.mkdir(parents=True)
    save_model_id(model, "l36")
    save_platform_config(model, [PlatformDefaults("标准单机芯3D", {"快捷键": ""})])
    leaf = model / "通用" / "快捷键"
    _write(leaf / "k.hex")
    leaf_removed = model / "通用" / "快捷按键"  # TOML 已归一为「快捷键程序」，历史 alias
    # 删除真实目录后，用另一个 alias 名复用该路径
    res = find_dangling_anchors(str(root), root, leaf_removed)
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1 and hits[0].raw_value == ""


def test_anchor_follow_default_not_fixed_anchor(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
    )
    res = find_dangling_anchors(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐"
    )
    hits = [h for h in _result(res["payload"]).hits if h.kind.startswith("shared_")]
    assert hits == []


def test_anchor_overlapping_candidate_under_anchor(ws: Path):
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    res = find_dangling_anchors(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐" / "子目录"
    )
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_static"]
    assert len(hits) == 1


def test_follow_asset_registered_and_resolved(ws: Path):
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键/贝乐",
        mode="follow_asset",
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    hits = [h for h in _result(res["payload"]).hits if h.kind == "shared_follow_asset"]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# owner 限定、语义命中与领域校验
# ---------------------------------------------------------------------------


def test_defaults_module_hit_not_cross_model(ws: Path):
    """L50 有同名 canonical defaults，查 L36 模块不得命中 L50。"""
    save_platform_config(
        ws / "L50程序", [PlatformDefaults("标准双2D", {"快捷键程序": "别的"})]
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module"
    )
    hits = [h for h in res["payload"]["result"].hits if h.kind == "platform_default"]
    assert {Path(h.owner_root).name for h in hits} == {"L36程序"}


def test_dangling_follow_default_semantic_hit_on_module(ws: Path):
    """follow_default 默认缺失时，module 级仍按来源根+canonical 语义命中。"""
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module"
    )
    hits = [h for h in res["payload"]["result"].hits if h.kind == "shared_follow_default"]
    assert len(hits) == 1


def test_follow_default_semantic_hit_requires_source_model_id(ws: Path):
    """路径首段指向 L36 但 id 写成 L50 的损坏引用 → 不算命中。"""
    _borrow(
        ws / "L50程序",
        "快捷键程序",
        "L36程序/通用/快捷键",
        mode="follow_default",
        sid="l50",
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module"
    )
    hits = [h for h in res["payload"]["result"].hits if h.kind == "shared_follow_default"]
    assert hits == []


def test_variant_container_rejected_as_asset(ws: Path):
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "asset"
    )
    assert res["ok"] is False and res["code"] == "invalid_target"


def test_variant_accepted_as_asset(ws: Path):
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    assert res["ok"] is True


def test_strict_loader_flags_illegal_mode_and_platform(ws: Path):
    cfg = ws / "L50程序" / MODEL_CONFIG_FILENAME
    text = cfg.read_text(encoding="utf-8")
    cfg.write_text(
        text
        + '\n[shared_modules."主板程序"]\n'
        'source_model_id = "l36"\n'
        'source_group = "l36"\n'
        'source_module = "主板程序"\n'
        'source_relative_path = "L36程序/通用/主板程序/v1"\n'
        'mode = "follow_asst"\n',
        encoding="utf-8",
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module"
    )
    issues = res["payload"]["result"].issues
    assert any(i.category == "invalid_shared_entry" for i in issues)


def test_strict_loader_flags_follow_asset_with_platform(ws: Path):
    _borrow(
        ws / "L50程序",
        "主板程序",
        "L36程序/通用/主板程序/v1",
        mode="follow_asset",
        source_platform="标准单机芯3D",
    )
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键", "module"
    )
    issues = res["payload"]["result"].issues
    assert any(i.category == "invalid_shared_entry" for i in issues)


# ---------------------------------------------------------------------------
# junction 与 catalog 判定
# ---------------------------------------------------------------------------


def test_junction_physical_descendant_hit(ws: Path, tmp_path: Path):
    """锚点经 junction 指向真实模块子目录：module 反查仍命中。"""
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link = link_parent / "快捷键alias"
    real = ws / "L36程序" / "通用" / "快捷键"
    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)],
        check=True,
        capture_output=True,
    )
    _borrow(ws / "L50程序", "快捷键程序", "L36程序/通用/快捷键/贝乐")
    # 通过 junction 的路径作为锚点（词法上与真实模块无前缀关系）
    junction_rel = f"../{link.parent.name}/{link.name}/贝乐"
    cfg = ws / "L50程序" / MODEL_CONFIG_FILENAME
    text = cfg.read_text(encoding="utf-8").replace(
        "L36程序/通用/快捷键/贝乐", junction_rel
    )
    cfg.write_text(text, encoding="utf-8")
    res = find_references_to(str(ws), ws, real, "module")
    hits = [h for h in res["payload"]["result"].hits if h.kind.startswith("shared_")]
    assert len(hits) == 1


def test_unknown_dir_under_module_rejected_as_asset(ws: Path, monkeypatch):
    """合法 label 下无匹配文件的子目录 → invalid_target（不猜 asset 身份）。"""
    empty = ws / "L36程序" / "通用" / "快捷键" / "空目录"
    empty.mkdir(parents=True)
    res = find_references_to(str(ws), ws, empty, "asset")
    assert res["ok"] is False and res["code"] == "invalid_target"


def test_variant_with_matching_file_accepted(ws: Path):
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    assert res["ok"] is True


def test_dir_keyword_module_accepted(ws: Path):
    """`主板` 是 mainboard 的 dir_keywords：scanner 与反查都接受该模块。"""
    variant = ws / "L36程序" / "通用" / "主板" / "v3"
    _write(variant / "rom.bin")
    res = find_references_to(str(ws), ws, variant, "asset")
    assert res["ok"] is True


def test_ascii_case_keyword_module_accepted(ws: Path):
    """`MP3` 是 music_files 的 dir_keywords：ASCII 大小写不影响判定。"""
    from fwasset.core.file_scan import _match_catalog_type
    from fwasset.core.firmware_catalog import enabled_firmware_types

    types = enabled_firmware_types()
    variant = ws / "L36程序" / "通用" / "MP3" / "v1"
    _write(variant / "song.mp3")
    # scanner 层接受
    assert (
        _match_catalog_type(str(variant), ["song.mp3"], types) is not None
    )
    # 反查层同样接受（keyword 小写 vs 模块名原大写不得误拒）
    res = find_references_to(str(ws), ws, variant, "asset")
    assert res["ok"] is True


def test_handcontrol_requires_rom_and_pkg(ws: Path):
    """仓库硬约束：handcontrol_ui 必须同时有 .rom 与 .pkg。"""
    from fwasset.core.file_scan import _match_catalog_type
    from fwasset.core.firmware_catalog import enabled_firmware_types

    types = enabled_firmware_types()
    rom_only = ws / "L36程序" / "通用" / "手控UI" / "只有rom"
    _write(rom_only / "fw.rom")
    pkg_only = ws / "L36程序" / "通用" / "手控UI" / "只有pkg"
    _write(pkg_only / "fw.pkg")
    both = ws / "L36程序" / "通用" / "手控UI" / "齐全"
    _write(both / "fw.rom")
    _write(both / "fw.pkg")

    # file_scan 层：缺任一不误认，齐全才命中
    assert _match_catalog_type(str(rom_only), ["fw.rom"], types) is None
    assert _match_catalog_type(str(pkg_only), ["fw.pkg"], types) is None
    assert _match_catalog_type(str(both), ["fw.rom", "fw.pkg"], types) is not None

    # 反查层：缺任一 invalid_target，齐全接受
    for p, ok in ((rom_only, False), (pkg_only, False), (both, True)):
        res = find_references_to(str(ws), ws, p, "asset")
        assert res["ok"] is ok, (p, res["code"] if not ok else "")


def test_catalog_unavailable_fail_closed(ws: Path, monkeypatch):
    """catalog 不可用 → invalid_target（fail-closed，不降级为纯结构判断）。"""
    import fwasset.core.reference_lookup as rl

    monkeypatch.setattr(rl, "_catalog_context", lambda: None)
    res = find_references_to(
        str(ws), ws, ws / "L36程序" / "通用" / "快捷键" / "贝乐", "asset"
    )
    assert res["ok"] is False and res["code"] == "invalid_target"


# ---------------------------------------------------------------------------
# CSC-001：owner_model_root_for 最深匹配（最近祖先）语义
# enumerate_model_roots 真实布局下型号根互不嵌套，这里人工构造嵌套根直接单测。
# ---------------------------------------------------------------------------


def test_owner_model_root_for_prefers_deepest_nested_root(tmp_path: Path):
    """roots=[R, R/N] 时 R/N 下的路径归属最深的 R/N（最近祖先）。"""
    root = tmp_path / "ws"
    nested = root / "Nested"
    deep = nested / "x" / "mod"
    deep.mkdir(parents=True)
    # 无论迭代顺序如何，最深根都胜出
    assert owner_model_root_for(deep, [root, nested]) == nested
    assert owner_model_root_for(deep, [nested, root]) == nested
    # 候选就是型号根本身：归属更深的 R/N（rel.parts 为空）
    assert owner_model_root_for(nested, [root, nested]) == nested


def test_owner_model_root_for_sibling_under_outer_root(tmp_path: Path):
    """R 下非 N 子路径归属 R；单根行为不变。"""
    root = tmp_path / "ws"
    nested = root / "Nested"
    nested.mkdir(parents=True)
    other = root / "Other" / "mod"
    other.mkdir(parents=True)
    assert owner_model_root_for(other, [root, nested]) == root
    # 单根：候选在根下即归属该根
    assert owner_model_root_for(other, [root]) == root


def test_owner_model_root_for_outside_all_roots_returns_none(tmp_path: Path):
    """不属于任何型号根 → None。"""
    root = tmp_path / "ws"
    (root / "x").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "a"
    outside.mkdir(parents=True)
    assert owner_model_root_for(outside, [root]) is None
    assert owner_model_root_for(outside, []) is None


# ---------------------------------------------------------------------------
# defaults 反查：catalog dir_keywords 目录名
# rename（find_references_to）与删除预检走同一条 _default_program_dir 路径。
# ---------------------------------------------------------------------------


def _keyword_mainboard_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """仅含关键词目录 通用/主板 的工作区（无 canonical 名 主板程序）。"""
    root = tmp_path / "ws"
    model = root / "L36程序"
    model.mkdir(parents=True)
    save_model_id(model, "l36")
    save_platform_config(model, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    variant = model / "通用" / "主板" / "v1"
    _write(variant / "rom.bin")
    return root, variant


def test_defaults_keyword_named_dir_hit_on_asset_lookup(tmp_path: Path):
    """通用/主板（mainboard dir_keywords）上的 defaults，find_references_to 能命中。

    rename 与删除预检都走这条反查路径。
    """
    root, variant = _keyword_mainboard_workspace(tmp_path)
    res = find_references_to(str(root), root, variant, "asset")
    assert res["ok"] is True
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1
    assert hits[0].raw_key == "主板程序"
    assert hits[0].raw_value == "v1"
    assert Path(hits[0].current_target).resolve() == variant.resolve()


def test_defaults_canonical_named_dir_hit_on_asset_lookup(ws: Path):
    """通用/主板程序（canonical 名）defaults 反查行为不变。"""
    l36 = ws / "L36程序"
    save_platform_config(l36, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    variant = l36 / "通用" / "主板程序" / "v1"
    res = find_references_to(str(ws), ws, variant, "asset")
    assert res["ok"] is True
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1
    assert hits[0].raw_key == "主板程序"
    assert hits[0].raw_value == "v1"
    assert Path(hits[0].current_target).resolve() == variant.resolve()


def test_defaults_keyword_dir_catalog_unavailable_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """catalog 不可用时 _default_program_dir 不崩，不把关键词目录静默当命中。"""
    import fwasset.core.reference_lookup as rl

    root, variant = _keyword_mainboard_workspace(tmp_path)
    model = variant.parents[2]  # L36程序
    monkeypatch.setattr(rl, "_catalog_context", lambda: None)
    got = _default_program_dir(model, "主板程序", "v1")
    assert got == model / "通用" / "主板程序" / "v1"


def test_defaults_canonical_dir_dangling_anchor_still_hits(ws: Path):
    """find_dangling_anchors 对 canonical 变体的 platform_default 命中不变。"""
    l36 = ws / "L36程序"
    save_platform_config(l36, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    variant = l36 / "通用" / "主板程序" / "v1"
    res = find_dangling_anchors(str(ws), ws, variant)
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1
    assert hits[0].raw_key == "主板程序"
    assert hits[0].raw_value == "v1"


def test_canonical_module_dir_does_not_alias_mainboard_keyword() -> None:
    """主板 不得写入 canonical 别名表；关键词命中只发生在 _default_program_dir 回退。"""
    assert canonical_module_dir("主板") == "主板"
    assert canonical_module_dir("主板程序") == "主板程序"


def test_defaults_canonical_dir_preferred_over_keyword_dir(tmp_path: Path) -> None:
    """canonical 名与关键词目录并存时以 canonical 为准，不再看 dir_keywords。"""
    root, _keyword = _keyword_mainboard_workspace(tmp_path)
    model = root / "L36程序"
    canonical = model / "通用" / "主板程序" / "v1"
    _write(canonical / "rom.bin")

    got = _default_program_dir(model, "主板程序", "v1")
    assert got.resolve() == canonical.resolve()

    res = find_references_to(str(root), root, canonical, "asset")
    assert res["ok"] is True
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1
    assert Path(hits[0].current_target).resolve() == canonical.resolve()


def test_defaults_non_unique_keyword_dirs_synthesize_canonical(tmp_path: Path) -> None:
    """通用区有两个均可匹配同一 module_key 的关键词目录 → 合成路径，不静默命中。"""
    root, variant = _keyword_mainboard_workspace(tmp_path)
    model = variant.parents[2]
    other = model / "通用" / "L36主板" / "v1"
    _write(other / "rom.bin")

    got = _default_program_dir(model, "主板程序", "v1")
    assert got == model / "通用" / "主板程序" / "v1"

    res = find_references_to(str(root), root, variant, "asset")
    assert res["ok"] is True
    hits = [h for h in _result(res["payload"]).hits if h.kind == "platform_default"]
    assert hits == []


def test_delete_asset_precheck_sees_keyword_dir_default_hit(tmp_path: Path) -> None:
    """delete_asset 预检走同一条 find_references_to：关键词目录 defaults 不得显示无人引用。"""
    from fwasset.core.services.asset_service import delete_asset

    root, variant = _keyword_mainboard_workspace(tmp_path)
    lookup = find_references_to(str(root), root, variant, "asset")
    assert lookup["ok"] is True
    hits = [h for h in _result(lookup["payload"]).hits if h.kind == "platform_default"]
    assert len(hits) == 1
    assert hits[0].raw_key == "主板程序"

    result = delete_asset(
        str(root), root, variant, confirm_shared=True
    )
    assert result["ok"] is True, result
    assert not variant.exists()


def test_retired_anchor_not_in_blocking_categories() -> None:
    assert "retired_anchor" not in BLOCKING_ISSUE_CATEGORIES
