"""Tests for SchemeWorkbenchModel: scheme module listing with platform fallback.

These tests build a fake L36-like directory tree on disk, scan it into a temp
SQLite index, then verify the view-model returns the correct custom + fallback
module lists. They are pure data-layer tests (no display), so not marked ui.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fwasset.core.asset_index import save_assets
from fwasset.core.file_scan import scan_firmware_assets
from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    save_model_id,
    save_shared_module,
)
from fwasset.core.types import FirmwareAsset
from fwasset.ui_common.view_models.scheme_workbench_model import SchemeWorkbenchModel


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def l36_tree(tmp_path: Path) -> Path:
    """Build a minimal L36-shaped firmware tree.

    通用 has 主板程序 (量产_默认 default + 防夹功能 variant), a single-variant
    3D机芯版程序 and 腿部程序 (no _默认 subdir → dir itself is the asset).
    定制/西班牙 only ships 主板程序 → everything else should fall back.
    """
    root = tmp_path / "L36程序"

    # platform config at the model root
    _write(
        root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"主板程序" = "量产_默认"',
                '"3D机芯版程序" = ""',
                '"腿部程序" = ""',
            ]
        ),
    )

    # 通用 modules
    _write(root / "通用" / "主板程序" / "量产_默认" / "YJ_3DMain_L36_V40.bin")
    _write(root / "通用" / "主板程序" / "防夹功能" / "YJ_3DMain_L36_V24.bin")
    _write(root / "通用" / "3D机芯版程序" / "YJ_ZD_3D_Core_L36_V20.mot")
    _write(root / "通用" / "腿部程序" / "Yj_Foot_L36_V5.hex")

    # 定制 scheme: only ships its own 主板程序
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "主板程序" / "YJ_3DMain_L36_西班牙_V40.bin")

    return root


def _bind_model(root: Path, tmp_path: Path) -> SchemeWorkbenchModel:
    db = tmp_path / "index.db"
    assets, _errors = scan_firmware_assets(str(root))
    save_assets(assets, str(root), path=db)
    model = SchemeWorkbenchModel()
    model.bind(db, root, root)
    return model


def test_platform_config_is_loaded_from_scan_root(
    l36_tree: Path, tmp_path: Path
) -> None:
    model = _bind_model(l36_tree, tmp_path)
    assert model._platforms, "平台配置.toml should be loaded from the scan root"
    names = {p.platform_name for p in model._platforms}
    assert "标准单机芯3D" in names


def test_scheme_includes_custom_exclusive_module(
    l36_tree: Path, tmp_path: Path
) -> None:
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    exclusive = [c for c in cards if c.source_type == "custom_exclusive"]
    labels = {c.asset["firmware_label"] for c in exclusive}
    assert "主板程序" in labels


def test_scheme_falls_back_to_common_default_variant(
    l36_tree: Path, tmp_path: Path
) -> None:
    """主板 is custom, but the scheme has no 3D机芯/腿部 → fall back to 通用."""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    fallback = [c for c in cards if c.is_fallback]
    fb_labels = {c.asset["firmware_label"] for c in fallback}
    # single-variant modules with empty default_dir must still fall back.
    # NOTE: firmware_label comes from the catalog ("3D机芯板程序", 板), while the
    # 通用/ directory and 平台配置.toml key are "3D机芯版程序" (版) — the matcher
    # tolerates this discrepancy, so we assert on the catalog label here.
    assert "3D机芯板程序" in fb_labels
    assert "腿部程序" in fb_labels


def test_load_all_models_filters_filename_noise(tmp_path: Path) -> None:
    """A file whose name contains 'L50' under the L36 tree must not create an L50 model.

    The dropdown should reflect the structural model root (L36), not noisy
    per-file parsed models.
    """
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n',
    )
    # 通用 mainboard whose filename mentions L50S → would parse model as L50S
    _write(root / "通用" / "主板程序" / "量产_默认" / "YJ_3DMain_L36_V40.bin")
    _write(root / "通用" / "主板程序" / "同L50S" / "YJ_3DMain_L50S_V9.bin")

    model = _bind_model(root, tmp_path)
    models = model.load_all_models()
    assert models == ["L36"], f"expected only structural model L36, got {models}"


def test_scheme_module_tree_groups_variants_under_one_row(tmp_path: Path) -> None:
    """手控UI 有多个变体时，应收成 ONE module row with children, not铺平."""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n',
    )
    # scheme with 3 手控UI variants + 1 主板
    scheme = root / "定制" / "马来" / "方案配置.toml"
    _write(scheme, 'name = "马来"\nplatform = "标准单机芯3D"\n')
    base = root / "定制" / "马来"
    _write(base / "主板程序" / "YJ_3DMain_L36_V40.bin")
    _write(base / "手控UI" / "L36 手控UI-A" / "a.rom")
    _write(base / "手控UI" / "L36 手控UI-A" / "a.pkg")
    _write(base / "手控UI" / "L36 手控UI-B" / "b.rom")
    _write(base / "手控UI" / "L36 手控UI-B" / "b.pkg")
    _write(base / "手控UI" / "L36 手控UI-C" / "c.rom")
    _write(base / "手控UI" / "L36 手控UI-C" / "c.pkg")

    model = _bind_model(root, tmp_path)
    tree = model.get_scheme_module_tree("L36", "马来")

    by_label = {row.label: row for row in tree}
    assert "主板程序" in by_label
    assert "手控UI" in by_label
    # 主板 single variant → 1 child (or treated as leaf)
    assert len(by_label["主板程序"].variants) == 1
    # 手控UI three variants grouped under ONE row
    assert len(by_label["手控UI"].variants) == 3


def test_scheme_module_tree_marks_custom_vs_common(
    l36_tree: Path, tmp_path: Path
) -> None:
    """每个模块行应标明是 定制专属 还是 通用默认（不含'回源'字样）。"""
    model = _bind_model(l36_tree, tmp_path)
    tree = model.get_scheme_module_tree("L36", "西班牙")
    by_label = {row.label: row for row in tree}
    # 西班牙 ships 主板程序 → 定制专属
    assert by_label["主板程序"].source_kind == "custom"
    # 西班牙 lacks 3D机芯/腿部 → 通用默认
    assert by_label["3D机芯板程序"].source_kind == "common"
    # the literal word "回源" must never appear in any user-facing label
    for row in tree:
        assert "回源" not in row.source_label
        for v in row.variants:
            assert "回源" not in v.source_label


def test_covered_module_is_not_duplicated_by_fallback(
    l36_tree: Path, tmp_path: Path
) -> None:
    """主板程序 is shipped by the scheme → it must NOT also appear as a fallback."""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    mainboard_fallbacks = [
        c for c in cards if c.is_fallback and c.asset["firmware_label"] == "主板程序"
    ]
    assert mainboard_fallbacks == []


# ---------------------------------------------------------------------------
# 多型号根：父文件夹下每个一级子目录是一个型号
# ---------------------------------------------------------------------------


@pytest.fixture()
def multi_model_tree(tmp_path: Path) -> Path:
    """父文件夹 按摩器程序/ 下两个型号目录：

    - L36程序：已整理（通用/定制/平台配置.toml）
    - L36双机芯-上3D-下2D程序：未整理（模块目录直接平铺，无 通用/定制）
    """
    parent = tmp_path / "按摩器程序"

    l36 = parent / "L36程序"
    _write(
        l36 / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n"主板程序" = "量产_默认"\n',
    )
    _write(l36 / "通用" / "主板程序" / "量产_默认" / "YJ_3DMain_L36_V40.bin")
    _write(l36 / "通用" / "主板程序" / "防夹功能" / "YJ_3DMain_L36_V24.bin")
    scheme = l36 / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "腿部程序" / "Yj_Foot_L36_西班牙_V6.hex")

    dual = parent / "L36双机芯-上3D-下2D程序"
    _write(dual / "主板程序" / "YJ_2CoreMain_L50S_V60.bin")

    return parent


def test_multi_model_root_lists_both_models(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    model = _bind_model(multi_model_tree, tmp_path)
    assert model.load_all_models() == ["L36", "L36双机芯-上3D-下2D"]


def test_multi_model_persistent_id_mapping(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    """B0：display / dir 解析到同一 id；型号配置落在型号根而非 通用/。"""
    model = _bind_model(multi_model_tree, tmp_path)
    id_l36 = model.resolve_model_id("L36")
    assert id_l36
    assert model.resolve_model_id("L36程序") == id_l36
    root = model.model_root_for_id(id_l36)
    assert root is not None and root.is_dir()
    assert (root / "型号配置.toml").is_file()
    assert not (root / "通用" / "型号配置.toml").exists()
    assert model.model_root_for_id("no-such-id") is None

    dual_id = model.resolve_model_id("L36双机芯-上3D-下2D")
    assert dual_id
    assert dual_id != id_l36
    dual_root = model.model_root_for_id(dual_id)
    assert dual_root is not None
    assert (dual_root / "型号配置.toml").is_file()


def test_single_model_root_gets_model_id(l36_tree: Path, tmp_path: Path) -> None:
    model = _bind_model(l36_tree, tmp_path)
    mid = model.resolve_model_id("L36")
    assert mid
    assert (l36_tree / "型号配置.toml").is_file()
    assert not (l36_tree / "通用" / "型号配置.toml").exists()
    assert (
        model.model_root_for_id(mid) == l36_tree.resolve()
        or model.model_root_for_id(mid) == l36_tree
    )


def test_bind_damaged_model_config_does_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "L36程序"
    _write(root / "型号配置.toml", "[[broken\n")
    _write(root / "通用" / "主板程序" / "量产_默认" / "a.bin")
    original = (root / "型号配置.toml").read_bytes()
    model = _bind_model(root, tmp_path)
    assert model.load_all_models() == ["L36"]
    assert model.resolve_model_id("L36") is None
    assert (root / "型号配置.toml").read_bytes() == original


def test_get_and_resolve_shared_modules_hit(tmp_path: Path) -> None:
    """B1：工作台读共享引用；源在同工作区则 hit。"""
    from fwasset.core.model_config import (
        SharedModuleRef,
        save_model_id,
        save_shared_module,
    )

    parent = tmp_path / "按摩器程序"
    l36 = parent / "L36程序"
    dual = parent / "L36双机芯-上3D-下2D程序"
    _write(l36 / "通用" / "快捷键" / "贝乐" / "k.hex")
    _write(dual / "主板程序" / "main.bin")
    save_model_id(l36, "l36")
    save_model_id(dual, "dual")
    save_shared_module(
        dual,
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l36",
            source_group="l36-single",
            source_module="快捷键程序",
            source_relative_path="L36程序/通用/快捷键/贝乐",
        ),
    )
    model = _bind_model(parent, tmp_path)
    refs = model.get_shared_modules("L36双机芯-上3D-下2D")
    assert len(refs) == 1
    assert refs[0].module_key == "快捷键程序"
    res = model.resolve_shared_module("L36双机芯-上3D-下2D", "快捷键程序")
    assert res is not None
    assert res.status == "hit"


def test_resolve_shared_source_not_imported(tmp_path: Path) -> None:
    from fwasset.core.model_config import (
        SharedModuleRef,
        save_model_id,
        save_shared_module,
    )

    # 单型号根需含 通用/ 以便布局检测
    root = tmp_path / "L36双机芯-上3D-下2D程序"
    _write(root / "通用" / "主板程序" / "main.bin")
    save_model_id(root, "dual")
    save_shared_module(
        root,
        SharedModuleRef(
            module_key="蓝牙程序",
            source_model_id="l50",
            source_group="l50s",
            source_module="蓝牙程序",
            source_relative_path="L50程序/通用/蓝牙/中文",
        ),
    )
    model = _bind_model(root, tmp_path)
    res = model.resolve_shared_module("L36双机芯-上3D-下2D", "蓝牙程序")
    assert res is not None
    assert res.status == "missing"
    assert res.reason == "source_not_imported"


def test_scheme_modules_ignore_shared_refs(tmp_path: Path) -> None:
    """B4：方案回源不吃 shared_modules。"""
    from fwasset.core.model_config import (
        SharedModuleRef,
        save_model_id,
        save_shared_module,
    )

    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n"主板程序" = "量产_默认"\n',
    )
    _write(root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    # 无腿部通用；共享指向不存在的源
    save_model_id(root, "l36")
    save_shared_module(
        root,
        SharedModuleRef(
            module_key="腿部程序",
            source_model_id="l50",
            source_group="x",
            source_module="腿部程序",
            source_relative_path="L50程序/通用/腿部程序",
        ),
    )
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "手控UI" / "ui.rom")
    _write(scheme / "手控UI" / "ui.pkg")  # 手控 UI 硬约束：.rom + .pkg 成对

    model = _bind_model(root, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    assert not any(c.asset.get("firmware_label") == "腿部程序" for c in cards)
    labels = {str(c.asset.get("firmware_label", "")) for c in cards}
    assert "手控UI" in labels or any("手控" in x for x in labels)


# ---------------- Phase B2: 共享登记入口 view-model API ----------------


def _setup_multi_model_workspace_for_registration(
    tmp_path: Path,
) -> tuple[Path, SchemeWorkbenchModel, dict]:
    """双机芯目标 + L36 来源（含两个已扫描的快捷键变体）。"""
    from fwasset.core.model_config import save_model_id

    parent = tmp_path / "按摩器程序"
    l36 = parent / "L36程序"
    dual = parent / "L36双机芯-上3D-下2D程序"
    src_variant = l36 / "通用" / "快捷键" / "贝乐"
    other_variant = l36 / "通用" / "快捷键" / "量产_默认"
    src_variant.mkdir(parents=True, exist_ok=True)
    other_variant.mkdir(parents=True, exist_ok=True)
    (src_variant / "k.hex").write_bytes(b"K")
    (other_variant / "k2.hex").write_bytes(b"K2")
    dual.mkdir(parents=True, exist_ok=True)
    # 双机芯根下一份可扫描固件，使该型号进入工作台位置型号列表
    _write(dual / "通用" / "主板程序" / "量产_默认" / "main.bin")
    save_model_id(l36, "l36")
    save_model_id(dual, "dual")
    model = _bind_model(parent, tmp_path)
    # 取出已扫描的来源资产
    asset = next(
        a
        for a in model._all_assets
        if str(a.get("path", "")) == str(src_variant.resolve())
    )
    other_asset = next(
        a
        for a in model._all_assets
        if str(a.get("path", "")) == str(other_variant.resolve())
    )
    return (
        parent,
        model,
        {
            "asset": asset,
            "other_asset": other_asset,
            "l36": l36,
            "dual": dual,
            "src_variant": src_variant,
        },
    )


def test_register_shared_module_writes_ref(tmp_path: Path) -> None:
    _parent, model, ctx = _setup_multi_model_workspace_for_registration(tmp_path)
    result = model.register_shared_module("L36双机芯-上3D-下2D", ctx["asset"])
    assert result["ok"] is True
    refs = model.get_shared_modules("L36双机芯-上3D-下2D")
    assert len(refs) == 1
    assert refs[0].module_key == "快捷键程序"
    assert refs[0].source_model_id == "l36"


def test_register_conflict_no_overwrite(tmp_path: Path) -> None:
    _parent, model, ctx = _setup_multi_model_workspace_for_registration(tmp_path)
    model.register_shared_module("L36双机芯-上3D-下2D", ctx["asset"])
    result = model.register_shared_module(
        "L36双机芯-上3D-下2D", ctx["other_asset"], overwrite=False
    )
    assert result["ok"] is False
    assert result["code"] == "conflict"
    # 原引用不变
    refs = model.get_shared_modules("L36双机芯-上3D-下2D")
    assert len(refs) == 1
    assert refs[0].source_relative_path.endswith("贝乐")


def test_register_conflict_overwrite(tmp_path: Path) -> None:
    _parent, model, ctx = _setup_multi_model_workspace_for_registration(tmp_path)
    model.register_shared_module("L36双机芯-上3D-下2D", ctx["asset"])
    result = model.register_shared_module(
        "L36双机芯-上3D-下2D", ctx["other_asset"], overwrite=True
    )
    assert result["ok"] is True
    refs = model.get_shared_modules("L36双机芯-上3D-下2D")
    assert len(refs) == 1
    assert refs[0].source_relative_path.endswith("量产_默认")


def test_unregister_shared_module_removes_ref_keeps_files(tmp_path: Path) -> None:
    _parent, model, ctx = _setup_multi_model_workspace_for_registration(tmp_path)
    model.register_shared_module("L36双机芯-上3D-下2D", ctx["asset"])
    # 双机芯本地有同模块文件
    local_file = ctx["dual"] / "通用" / "快捷键" / "本地变体" / "local.hex"
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_bytes(b"LOCAL")
    result = model.unregister_shared_module("L36双机芯-上3D-下2D", "快捷键程序")
    assert result["ok"] is True
    assert model.get_shared_modules("L36双机芯-上3D-下2D") == []
    # 本地文件仍在
    assert local_file.exists()


def test_register_does_not_break_scheme_isolation(tmp_path: Path) -> None:
    """B4 隔离回归：登记后 get_scheme_modules / get_scheme_module_tree 不含共享行。"""
    from fwasset.core.model_config import save_model_id

    parent = tmp_path / "按摩器程序"
    l36 = parent / "L36程序"
    dual = parent / "L36双机芯-上3D-下2D程序"
    _write(l36 / "通用" / "快捷键" / "贝乐" / "k.hex")
    _write(dual / "通用" / "主板程序" / "main.bin")
    _write(
        dual / "定制" / "方案A" / "方案配置.toml",
        'name = "方案A"\nplatform = "标准单机芯3D"\n',
    )
    _write(dual / "定制" / "方案A" / "主板程序" / "a.bin")
    save_model_id(l36, "l36")
    save_model_id(dual, "dual")
    model = _bind_model(parent, tmp_path)
    src_asset = next(
        a for a in model._all_assets if str(a.get("firmware_label", "")) == "快捷键程序"
    )
    model.register_shared_module("L36双机芯-上3D-下2D", src_asset)
    # 方案树 / 模块列表仍不含快捷键程序共享行
    tree = model.get_scheme_module_tree("L36双机芯-上3D-下2D", "方案A")
    all_modules = model.get_all_modules("L36双机芯-上3D-下2D")
    # 共享已登记但不应在方案树/全部模块中出现
    shared_refs = model.get_shared_modules("L36双机芯-上3D-下2D")
    assert len(shared_refs) == 1
    scheme_labels = {row.label for row in tree}
    all_labels = {c.asset.get("firmware_label", "") for c in all_modules}
    assert "快捷键程序" not in scheme_labels
    assert "快捷键程序" not in all_labels


def test_register_target_root_not_in_common(tmp_path: Path) -> None:
    """目标型号根不落 通用/（即使资产在 通用/ 下，写盘到型号根）。"""
    _parent, model, ctx = _setup_multi_model_workspace_for_registration(tmp_path)
    model.register_shared_module("L36双机芯-上3D-下2D", ctx["asset"])
    config_path = ctx["dual"] / "型号配置.toml"
    assert config_path.exists()
    # 通用 下不应出现 型号配置.toml
    assert not (ctx["dual"] / "通用" / "型号配置.toml").exists()


def test_multi_model_assets_do_not_leak_across_models(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    """双机芯主板不出现在 L36 视图里，反之亦然（文件名噪声不参与归属）。"""
    model = _bind_model(multi_model_tree, tmp_path)

    l36_paths = {str(c.asset["path"]) for c in model.get_all_modules("L36")}
    dual_paths = {
        str(c.asset["path"]) for c in model.get_all_modules("L36双机芯-上3D-下2D")
    }
    assert l36_paths, "L36 should have assets"
    assert dual_paths, "dual-core model should have assets"
    assert not (l36_paths & dual_paths), "assets must not appear under both models"
    assert all("L36双机芯" not in p for p in l36_paths)
    assert all("L36双机芯" in p for p in dual_paths)


def test_multi_model_platforms_are_scoped_per_model(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    """平台配置按型号隔离：L36 有标准单机芯3D，未整理的双机芯型号没有平台。"""
    model = _bind_model(multi_model_tree, tmp_path)
    assert model.platform_names("L36") == ["标准单机芯3D"]
    assert model.platform_names("L36双机芯-上3D-下2D") == []


def test_multi_model_sidebar_and_fallback_work_per_model(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    model = _bind_model(multi_model_tree, tmp_path)

    tree = model.build_sidebar_tree("L36")
    assert tree["common"].get("主板程序") == 2
    assert tree["custom"] == ["西班牙"]

    # 西班牙 缺主板 → 回源到 L36 自己的通用默认
    cards = model.get_scheme_modules("L36", "西班牙")
    fb = [c for c in cards if c.is_fallback and c.asset["firmware_label"] == "主板程序"]
    assert fb and fb[0].asset["directory_name"] == "量产_默认"

    # 未整理的双机芯型号没有 通用/定制 → 侧边树为空，但「全部」视图能看到资产
    dual_tree = model.build_sidebar_tree("L36双机芯-上3D-下2D")
    assert dual_tree == {"common": {}, "custom": []}


def test_multi_model_set_default_writes_into_model_dir(
    multi_model_tree: Path, tmp_path: Path
) -> None:
    """多型号根下设默认要写进该型号自己的 平台配置.toml。"""
    model = _bind_model(multi_model_tree, tmp_path)
    cards = model.get_common_modules("L36", "主板程序")
    fangjia = next(c.asset for c in cards if c.asset["directory_name"] == "防夹功能")

    result = model.set_default_variant("L36", fangjia, log_fn=lambda _m: None)
    assert result["ok"] is True, result["message"]

    from fwasset.core.platform_config import load_platform_config

    loaded = load_platform_config(multi_model_tree / "L36程序")
    assert loaded[0].defaults["主板程序"] == "防夹功能"
    # 父文件夹自身不应被写入配置
    assert not (multi_model_tree / "平台配置.toml").exists()


# ---------------------------------------------------------------------------
# 平台默认（设默认功能）：徽章、写回 平台配置.toml、回源联动
# ---------------------------------------------------------------------------


def test_default_badge_marks_configured_variant(l36_tree: Path, tmp_path: Path) -> None:
    """平台配置里指定的变体带 ★默认 徽章，其余变体不带。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_common_modules("L36", "主板程序")
    badges = {c.asset["directory_name"]: c.default_badge for c in cards}
    assert badges["量产_默认"] == "★默认"  # 单平台 → 不附平台名
    assert badges["防夹功能"] == ""


def test_shortcut_alias_duplicate_prefers_catalog_key_for_badge_and_fallback(
    l36_tree: Path,
    tmp_path: Path,
) -> None:
    """历史短键与规范键并存时，徽章和方案补齐都以规范键为准。"""
    _write(
        l36_tree / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"快捷键" = "量产_默认"',
                '"快捷键程序" = "贝乐"',
            ]
        ),
    )
    _write(l36_tree / "通用" / "快捷键" / "量产_默认" / "mass.hex")
    _write(l36_tree / "通用" / "快捷键" / "贝乐" / "belle.hex")

    model = _bind_model(l36_tree, tmp_path)
    shortcuts = model.get_common_modules("L36", "快捷键程序")
    badges = {card.asset["directory_name"]: card.default_badge for card in shortcuts}
    assert badges == {"量产_默认": "", "贝乐": "★默认"}

    fallback = next(
        card
        for card in model.get_scheme_modules("L36", "西班牙")
        if card.is_fallback and card.asset["firmware_label"] == "快捷键程序"
    )
    assert fallback.asset["directory_name"] == "贝乐"


def test_common_module_parts_resolves_variant_and_single_level(
    l36_tree: Path, tmp_path: Path
) -> None:
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_common_modules("L36", "主板程序")
    mainboard = next(c.asset for c in cards if c.asset["directory_name"] == "量产_默认")
    assert model._common_module_parts(mainboard) == ("主板程序", "量产_默认")

    leg_cards = model.get_common_modules("L36", "腿部程序")
    assert leg_cards, "腿部程序 should exist as a single-level common module"
    # 资产目录直接位于模块层 → 变体为空串（与 toml 空值语义一致）
    assert model._common_module_parts(leg_cards[0].asset) == ("腿部程序", "")


def test_set_default_variant_writes_config_and_moves_badge(
    l36_tree: Path, tmp_path: Path
) -> None:
    """设默认后：toml 落盘、平台配置就地重载、徽章移动，无需重新扫描。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_common_modules("L36", "主板程序")
    fangjia = next(c.asset for c in cards if c.asset["directory_name"] == "防夹功能")

    result = model.set_default_variant("L36", fangjia, log_fn=lambda _m: None)
    assert result["ok"] is True, result["message"]

    # toml 落盘
    from fwasset.core.platform_config import load_platform_config

    loaded = load_platform_config(l36_tree)
    assert loaded[0].defaults["主板程序"] == "防夹功能"

    # 徽章立即移动（平台已重载）
    badges = {
        c.asset["directory_name"]: c.default_badge
        for c in model.get_common_modules("L36", "主板程序")
    }
    assert badges["防夹功能"] == "★默认"
    assert badges["量产_默认"] == ""


def test_set_default_variant_changes_scheme_fallback(
    l36_tree: Path, tmp_path: Path
) -> None:
    """回源跟随新默认：缺主板的方案设默认后应回源到新变体。"""
    # 增加一个不带主板的方案，让主板走回源
    scheme = l36_tree / "定制" / "葡萄牙"
    _write(scheme / "方案配置.toml", 'name = "葡萄牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "腿部程序" / "Yj_Foot_L36_葡萄牙_V6.hex")

    model = _bind_model(l36_tree, tmp_path)

    def _mainboard_fallback_dir() -> str:
        cards = model.get_scheme_modules("L36", "葡萄牙")
        fb = [
            c
            for c in cards
            if c.is_fallback and c.asset["firmware_label"] == "主板程序"
        ]
        assert fb, "葡萄牙 lacks 主板程序 → must fall back to 通用"
        return str(fb[0].asset["directory_name"])

    assert _mainboard_fallback_dir() == "量产_默认"

    cards = model.get_common_modules("L36", "主板程序")
    fangjia = next(c.asset for c in cards if c.asset["directory_name"] == "防夹功能")
    result = model.set_default_variant("L36", fangjia, log_fn=lambda _m: None)
    assert result["ok"] is True, result["message"]

    assert _mainboard_fallback_dir() == "防夹功能"


def test_set_default_variant_rewrites_typo_module_key_to_board(
    l36_tree: Path, tmp_path: Path
) -> None:
    """磁盘/历史 toml 的「机芯版」笔误：设默认后统一为 catalog 规范「机芯板」，不留双键。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_all_modules("L36")
    core_3d = next(
        c.asset for c in cards if c.asset["firmware_label"] == "3D机芯板程序"
    )

    result = model.set_default_variant("L36", core_3d, log_fn=lambda _m: None)
    assert result["ok"] is True, result["message"]

    from fwasset.core.platform_config import load_platform_config

    defaults = load_platform_config(l36_tree)[0].defaults
    keys_3d = [k for k in defaults if "机芯" in k and "3D" in k]
    assert keys_3d == ["3D机芯板程序"]
    assert "3D机芯版程序" not in defaults


def test_set_default_variant_rejects_custom_asset(
    l36_tree: Path, tmp_path: Path
) -> None:
    """定制区资产不能设为平台默认。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    custom = next(c.asset for c in cards if c.source_type == "custom_exclusive")
    result = model.set_default_variant("L36", custom, log_fn=lambda _m: None)
    assert result["ok"] is False
    assert result["code"] == "invalid_args"


def test_set_default_variant_damaged_platform_config_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """损坏的平台配置：服务拒绝写入；view model 透传错误且不伪造 platforms。"""
    root = tmp_path / "L36程序"
    damaged = "[[platform\nbad"
    _write(root / "平台配置.toml", damaged)
    _write(root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    original = (root / "平台配置.toml").read_bytes()

    model = _bind_model(root, tmp_path)
    cards = model.get_common_modules("L36", "主板程序")
    asset = cards[0].asset
    result = model.set_default_variant("L36", asset, log_fn=lambda _m: None)
    assert result["ok"] is False
    assert result["code"] == "config_parse_error"
    assert (root / "平台配置.toml").read_bytes() == original
    assert model._platforms_for("L36") == []


def test_first_set_default_without_toml_uses_scheme_platform_names(
    tmp_path: Path,
) -> None:
    """验收场景 5：无 toml 但方案有 platform → 首次设默认建同名块，回源立即生效。"""
    root = tmp_path / "L36程序"
    _write(root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    _write(root / "通用" / "主板程序" / "防夹功能" / "fang.bin")
    _write(root / "通用" / "腿部程序" / "leg.hex")
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "手控UI" / "ui.rom")
    _write(scheme / "手控UI" / "ui.pkg")  # 手控 UI 硬约束；有定制手控，缺主板 → 回源主板

    model = _bind_model(root, tmp_path)
    assert model._platforms_for("L36") == []

    cards = model.get_common_modules("L36", "主板程序")
    fangjia = next(c.asset for c in cards if c.asset["directory_name"] == "防夹功能")
    # 无配置时菜单可点：未落盘不算 is_model_module_default
    assert model.is_model_module_default(fangjia) is False

    result = model.set_default_variant("L36", fangjia, log_fn=lambda _m: None)
    assert result["ok"] is True, result["message"]

    from fwasset.core.platform_config import load_platform_config

    loaded = load_platform_config(root)
    assert len(loaded) == 1
    assert loaded[0].platform_name == "标准单机芯3D"
    assert loaded[0].defaults["主板程序"] == "防夹功能"
    assert "默认" not in {p.platform_name for p in loaded}

    scheme_cards = model.get_scheme_modules("L36", "西班牙")
    fb = [
        c
        for c in scheme_cards
        if c.is_fallback and c.asset.get("firmware_label") == "主板程序"
    ]
    assert fb and fb[0].asset["directory_name"] == "防夹功能"


def test_unique_common_module_inferred_as_fallback_without_defaults_key(
    tmp_path: Path,
) -> None:
    """A4：配置块无该模块键时，唯一通用变体仍可回源到方案。"""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n"主板程序" = "量产_默认"\n',
    )
    _write(root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    _write(root / "通用" / "腿部程序" / "only_leg.hex")  # 唯一腿部，toml 无键
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "手控UI" / "ui.rom")
    _write(scheme / "手控UI" / "ui.pkg")  # 手控 UI 硬约束：.rom + .pkg 成对

    model = _bind_model(root, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    leg_fb = [
        c
        for c in cards
        if c.is_fallback and c.asset.get("firmware_label") == "腿部程序"
    ]
    assert leg_fb, "unique 腿部 should fall back without defaults key"
    assert leg_fb[0].asset["directory_name"] == "腿部程序" or "only" in str(
        leg_fb[0].asset.get("path", "")
    )


def test_empty_scheme_uses_platform_from_scheme_toml_not_asset(
    tmp_path: Path,
) -> None:
    """仅有方案配置、无定制固件：按 方案配置.toml 的 platform 选配置块，不扫全部块。"""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"蓝牙程序" = "中文版本"',
                "",
                "[[platform]]",
                'name = "双机芯"',
                "[platform.defaults]",
                '"蓝牙程序" = "英文版本"',
            ]
        ),
    )
    _write(root / "通用" / "蓝牙程序" / "中文版本" / "bt_zh.bin")
    _write(root / "通用" / "蓝牙程序" / "英文版本" / "bt_en.bin")
    # 空方案：只有 toml，没有任何定制固件 → 索引无 custom 资产
    scheme = root / "定制" / "纯回源方案"
    _write(scheme / "方案配置.toml", 'name = "纯回源方案"\nplatform = "双机芯"\n')

    model = _bind_model(root, tmp_path)
    cards = model.get_scheme_modules("L36", "纯回源方案")
    assert not any(c.source_type == "custom_exclusive" for c in cards)
    bt = [
        c
        for c in cards
        if c.is_fallback and c.asset.get("firmware_label") == "蓝牙程序"
    ]
    assert len(bt) == 1
    assert bt[0].asset["directory_name"] == "英文版本"
    assert not any(c.asset.get("directory_name") == "中文版本" for c in bt)


def test_scheme_platform_mismatch_blocks_all_common_fallback(
    tmp_path: Path,
) -> None:
    """方案声明 platform=B 但 TOML 只有 A：通用回源失败，A4 唯一变体也不得补入。"""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        '[[platform]]\nname = "标准单机芯3D"\n[platform.defaults]\n"蓝牙程序" = "中文版本"\n',
    )
    _write(root / "通用" / "蓝牙程序" / "中文版本" / "bt.bin")
    _write(root / "通用" / "腿部程序" / "only_leg.hex")  # 唯一变体，易被错误 A4 补入
    scheme = root / "定制" / "错配方案"
    _write(scheme / "方案配置.toml", 'name = "错配方案"\nplatform = "双机芯"\n')
    # 有一份定制固件，确保方案可进视图；platform 与 TOML 块名对不上
    _write(scheme / "主板程序" / "custom_main.bin")

    model = _bind_model(root, tmp_path)
    cards = model.get_scheme_modules("L36", "错配方案")
    assert any(c.source_type == "custom_exclusive" for c in cards)
    # 不得回源蓝牙（块 A）或唯一腿部（A4）
    assert not any(c.is_fallback for c in cards), [
        (c.asset.get("firmware_label"), c.asset.get("directory_name"))
        for c in cards
        if c.is_fallback
    ]


def test_platform_names_prefers_config(l36_tree: Path, tmp_path: Path) -> None:
    model = _bind_model(l36_tree, tmp_path)
    assert model.platform_names() == ["标准单机芯3D"]


# ---------------------------------------------------------------------------
# Asset cache (P1-2) — single-bind, no-repeated-SQLite discipline
# ---------------------------------------------------------------------------

from typing import Any  # noqa: E402  (kept near its only use)


def _make_asset(
    *,
    path: str,
    model: str = "L36",
    category: str = "common",
    firmware_label: str = "主板程序",
    firmware_type: str = "mainboard",
    scheme_name: str = "",
    directory_name: str = "量产_默认",
    label: str = "",
    model_directory_path: str = "/scan/L36程序",
    platform: str = "",
) -> dict[str, Any]:
    return {
        "series": "",
        "model": model,
        "version": "V40",
        "firmware_type": firmware_type,
        "firmware_label": firmware_label,
        "flash_mode": "tool_launch",
        "usb_flow": "",
        "path": path,
        "directory_name": directory_name,
        "model_directory_name": "L36程序",
        "model_directory_path": model_directory_path,
        "files": ["f.bin"],
        "modified_time": 0.0,
        "tool_name": "",
        "tool_path": "",
        "tool_dir": "",
        "label": label or directory_name,
        "category": category,
        "platform": platform,
        "scheme_name": scheme_name,
        "scheme_path": "",
        "chassis_type": "",
        "vendor": "",
    }


class _CountingQueryAssets:
    """Counting replacement for fwasset.core.asset_index.query_assets.

    Records every call (so tests can assert the call count) and returns a
    pre-supplied asset list. Tests monkeypatch the module-level binding
    `fwasset.core.asset_index.query_assets` to this instance.
    """

    def __init__(self, assets: list[dict[str, Any]]) -> None:
        self.assets = assets
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"args": args, "kwargs": kwargs})
        # Apply the same filters the real query_assets does, so the model
        # exercises the same code path as in production.
        keyword = (kwargs.get("keyword") or "").lower().strip()
        category = kwargs.get("category") or ""
        scheme_name = kwargs.get("scheme_name") or ""
        out: list[dict[str, Any]] = []
        for a in self.assets:
            if category and a.get("category") != category:
                continue
            if scheme_name and a.get("scheme_name") != scheme_name:
                continue
            if keyword:
                hay = " ".join(
                    str(a.get(k, ""))
                    for k in (
                        "label",
                        "directory_name",
                        "firmware_label",
                        "firmware_type",
                        "version",
                        "model",
                        "series",
                        "path",
                    )
                ).lower()
                if keyword not in hay:
                    continue
            out.append(a)
        return out


@pytest.fixture()
def cached_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[SchemeWorkbenchModel, _CountingQueryAssets]:
    """Build a SchemeWorkbenchModel whose query_assets is a counting fake.

    No real scan, no real SQLite — pure logic + cache discipline tests.
    """
    import fwasset.core.asset_index as asset_index_module
    import fwasset.ui_common.view_models.scheme_workbench_model as model_module

    assets = [
        # 通用 mainboard: 2 variants under 通用/主板程序/
        _make_asset(
            path="/scan/L36程序/通用/主板程序/量产_默认/YJ_3DMain_L36_V40.bin",
            directory_name="量产_默认",
            label="量产_默认",
        ),
        _make_asset(
            path="/scan/L36程序/通用/主板程序/防夹功能/YJ_3DMain_L36_V24.bin",
            directory_name="防夹功能",
            label="防夹功能",
        ),
        # 通用 single-variant module
        _make_asset(
            path="/scan/L36程序/通用/3D机芯版程序/YJ_ZD_3D_Core_L36_V20.mot",
            firmware_label="3D机芯板程序",
            directory_name="YJ_ZD_3D_Core_L36_V20",
            label="3D机芯版程序",
        ),
        # 定制 scheme: only ships its own mainboard
        _make_asset(
            path="/scan/L36程序/定制/西班牙/主板程序/YJ_3DMain_L36_西班牙_V40.bin",
            category="custom",
            scheme_name="西班牙",
            directory_name="西班牙_主板",
            label="西班牙_主板",
            platform="标准单机芯3D",
        ),
    ]
    counter = _CountingQueryAssets(assets)
    monkeypatch.setattr(asset_index_module, "query_assets", counter)
    # The model imports the symbol by name — patch that binding too.
    monkeypatch.setattr(model_module, "query_assets", counter)
    # Provide a minimal platform config so the 西班牙 fallback path still runs.
    from fwasset.core.platform_config import PlatformDefaults

    platform = PlatformDefaults(
        platform_name="标准单机芯3D", defaults={"3D机芯版程序": ""}
    )
    monkeypatch.setattr(model_module, "load_platform_config", lambda _dir: [platform])

    model = SchemeWorkbenchModel()
    model.bind(tmp_path / "index.db", tmp_path / "L36程序")
    return model, counter


def test_bind_populates_cache_without_repeated_query_assets(cached_model) -> None:
    """bind() may call query_assets once to load the model-root map, but never
    on the hot path. After bind, view methods must NOT call query_assets again.
    """
    model, counter = cached_model
    initial_calls = len(counter.calls)

    # Build the sidebar tree and inspect modules — both should read from the cache.
    model.build_sidebar_tree("L36")
    model.get_common_modules("L36", "主板程序")
    model.get_scheme_modules("L36", "西班牙")
    model.get_all_modules("L36")
    model.load_all_models()

    assert len(counter.calls) == initial_calls, (
        f"view methods must not hit query_assets when cache is populated; "
        f"got {len(counter.calls) - initial_calls} extra calls"
    )


def test_cache_preserves_keyword_filter_semantics(cached_model) -> None:
    """The cache must NOT weaken the keyword filter. A user search must still
    exclude assets whose label/directory_name does not contain the keyword.
    """
    model, _counter = cached_model

    # 量产_默认 matches "量产" (part of label + directory_name).
    matches = model.get_common_modules("L36", "主板程序", keyword="量产")
    assert len(matches) == 1
    assert "量产" in matches[0].asset["label"]

    # No mainboard variant matches "xyzzy" — must return empty.
    assert model.get_common_modules("L36", "主板程序", keyword="xyzzy") == []


def test_cache_keyword_supports_space_split_AND_with_OR_per_token(cached_model) -> None:
    """BUG-2: the cache must replicate query_assets' 「空格分词 AND 跨字段 OR」 semantics.

    Examples:
        "主板 防夹"  → (any field contains "主板") AND (any field contains "防夹")
        "主板 xyzzy" → empty (second token matches nothing)
        "xyzzy"      → empty (single token matches nothing)

    The previous implementation did a single substring match on a joined
    field string, so "主板 防夹" required the literal string "主板 防夹" to
    appear in the haystack — effectively killing the flexible multi-word
    search.
    """
    model, _counter = cached_model

    # 主板 mainboard variants: 量产_默认 (label=量产_默认, no 防夹) and 防夹功能 (label=防夹功能, no 主板)
    # The first token "主板" matches the 主板 mainboard row; the second token
    # "防夹" then narrows to the variant whose directory_name contains 防夹.
    cards = model.get_common_modules("L36", "主板程序", keyword="主板 防夹")
    dirs = [c.asset["directory_name"] for c in cards]
    assert dirs == ["防夹功能"], (
        f"expected only 防夹功能 to match '主板 防夹', got {dirs!r}"
    )

    # Reverse: a token that doesn't match anything must yield an empty list,
    # not silently return everything (the bug we are fixing).
    assert model.get_common_modules("L36", "主板程序", keyword="主板 xyzzy") == []
    assert model.get_common_modules("L36", "主板程序", keyword="xyzzy") == []

    # Single token still works.
    only_zhujiao = model.get_common_modules("L36", "主板程序", keyword="主板")
    assert len(only_zhujiao) >= 1


def test_cache_keyword_matches_across_fields_not_just_directory_name(
    cached_model,
) -> None:
    """A user search for a version string like V40 must hit assets whose
    'version' field contains V40 — not only those whose directory_name does.
    This is the '跨字段' half of the contract.
    """
    model, _counter = cached_model
    # Both mainboard assets have version V40; the custom 西班牙 one too.
    cards = model.get_all_modules("L36", keyword="V40")
    assert len(cards) >= 1
    for c in cards:
        assert "V40" in str(c.asset.get("version", "")).upper()


def test_cache_preserves_scheme_fallback_behavior(cached_model) -> None:
    """The 西班牙 scheme ships only 主板程序; 3D机芯 must still come from 通用
    via the platform-config fallback. Caching must not break this.
    """
    model, _counter = cached_model
    cards = model.get_scheme_modules("L36", "西班牙")
    by_label: dict[str, list] = {}
    for c in cards:
        by_label.setdefault(c.asset["firmware_label"], []).append(c)

    assert "主板程序" in by_label
    assert "3D机芯板程序" in by_label
    # 3D机芯 should be a fallback (no _默认 directory but platform_config
    # covered it).
    assert by_label["3D机芯板程序"][0].is_fallback is True
    # 主板 must be 定制专属, not fallback.
    assert all(c.source_type == "custom_exclusive" for c in by_label["主板程序"])
    by_label: dict[str, list] = {}
    for c in cards:
        by_label.setdefault(c.asset["firmware_label"], []).append(c)

    assert "主板程序" in by_label
    assert "3D机芯板程序" in by_label
    # 3D机芯 should be a fallback (no _默认 directory but platform_config
    # covered it).
    assert by_label["3D机芯板程序"][0].is_fallback is True
    # 主板 must be 定制专属, not fallback.
    assert all(c.source_type == "custom_exclusive" for c in by_label["主板程序"])


def test_common_module_cards_carry_source_kind_common(cached_model) -> None:
    """BUG-1: a 通用 mainboard variant must report source_kind='common' so the
    workbench can mark it 通用默认 — not 定制专属.

    The previous code inferred source_kind from is_fallback only, so any
    non-fallback card (which all 通用 cards are) was mis-labeled as custom.
    """
    model, _counter = cached_model
    cards = model.get_common_modules("L36", "主板程序")
    assert cards, "fixture should yield at least one mainboard card"
    for c in cards:
        assert getattr(c, "source_kind", None) == "common", (
            f"mainboard variant {c.asset.get('directory_name')!r} should be "
            f"common but got source_kind={getattr(c, 'source_kind', None)!r}"
        )


def test_scheme_module_tree_marks_common_assets_as_common_default(cached_model) -> None:
    """BUG-1: when viewing a 西班牙 scheme that ships its own 主板, the OTHER
    mainboard variants under 通用 (防夹功能, 量产_默认 etc.) shown by get_all_modules
    must be tagged 通用默认 — never 定制专属.

    The test is end-to-end through the public method that the UI consumes.
    """
    model, _counter = cached_model
    all_cards = model.get_all_modules("L36")
    common_only = [c for c in all_cards if c.source_type.startswith("common_")]
    assert common_only, "fixture should yield common cards"
    for c in common_only:
        assert c.source_kind == "common", (
            f"common card {c.asset.get('directory_name')!r} leaked into custom: "
            f"source_kind={c.source_kind!r} source_type={c.source_type!r}"
        )


def test_all_modules_ownership_label_includes_scheme_name(
    l36_tree: Path, tmp_path: Path
) -> None:
    """全部视图归属为「通用」或「定制专属 · 方案名」。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_all_modules("L36")
    assert cards
    for c in cards:
        assert "回源" not in c.source_label
        if c.source_kind == "common":
            assert c.source_label == "通用"
        else:
            scheme = str(c.asset.get("scheme_name", "")).strip()
            if scheme:
                assert c.source_label == f"定制专属 · {scheme}"
            else:
                assert c.source_label == "定制专属"


def test_scheme_modules_never_expose_huanyuan_in_card_label(
    l36_tree: Path, tmp_path: Path
) -> None:
    """Issue 6：get_scheme_modules 卡片层也不得出现「回源」。"""
    model = _bind_model(l36_tree, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    assert cards
    for c in cards:
        assert "回源" not in c.source_label
        if c.is_fallback:
            assert c.source_label == "通用"
        else:
            assert c.source_label == "定制专属"


def test_scheme_module_tree_fallback_carries_default_badge(
    l36_tree: Path, tmp_path: Path
) -> None:
    """Issue 13：方案视图回源变体应带 ★默认（含 toml 空默认 = 模块唯一变体）。"""
    model = _bind_model(l36_tree, tmp_path)
    tree = model.get_scheme_module_tree("L36", "西班牙")
    # 西班牙缺 3D / 腿部 → 回源；平台配置二者均为 "" 空默认
    row_3d = next((r for r in tree if r.label == "3D机芯板程序"), None)
    assert row_3d is not None and row_3d.variants
    assert row_3d.variants[0].default_badge == "★默认"

    row_leg = next((r for r in tree if r.label == "腿部程序"), None)
    assert row_leg is not None and row_leg.variants
    assert row_leg.variants[0].default_badge == "★默认"

    # 通用区直接列腿部也应有徽章（空默认语义）
    leg_cards = model.get_common_modules("L36", "腿部程序")
    assert leg_cards
    assert leg_cards[0].default_badge == "★默认"


def test_empty_default_badge_for_unique_nested_variant(tmp_path: Path) -> None:
    """空默认 "" 覆盖「唯一嵌套变体」，不要求文件直接位于模块目录。

    结构：通用/语音程序/中文唯一版/voice.bin + defaults 语音程序 = ""
    """
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"语音程序" = ""',
            ]
        ),
    )
    _write(root / "通用" / "语音程序" / "中文唯一版" / "voice.bin")
    # 多变体主板：空默认不应误标（此处不配置主板空默认；另建对照）
    _write(root / "通用" / "主板程序" / "量产_默认" / "a.bin")
    _write(root / "通用" / "主板程序" / "防夹功能" / "b.bin")
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "主板程序" / "custom.bin")

    model = _bind_model(root, tmp_path)
    voice = model.get_common_modules("L36", "语音程序")
    assert len(voice) == 1
    assert voice[0].asset["directory_name"] == "中文唯一版"
    assert voice[0].default_badge == "★默认"

    # 方案回源语音也应带徽章
    tree = model.get_scheme_module_tree("L36", "西班牙")
    row_voice = next((r for r in tree if r.label == "语音程序"), None)
    assert row_voice is not None and row_voice.variants
    assert row_voice.variants[0].default_badge == "★默认"
    assert row_voice.variants[0].name == "中文唯一版"


def test_empty_default_does_not_badge_when_multiple_variants(tmp_path: Path) -> None:
    """空默认但模块下有多份通用变体 → 配置异常，谁都不标 ★默认。"""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"语音程序" = ""',
            ]
        ),
    )
    _write(root / "通用" / "语音程序" / "中文版" / "a.bin")
    _write(root / "通用" / "语音程序" / "英文版" / "b.bin")

    model = _bind_model(root, tmp_path)
    voice = model.get_common_modules("L36", "语音程序")
    assert len(voice) == 2
    assert all(c.default_badge == "" for c in voice)


def test_empty_default_multi_variant_does_not_scheme_fallback(tmp_path: Path) -> None:
    """空默认 + 多变体：方案页不得猜测回源第一份（与徽章契约一致）。"""
    root = tmp_path / "L36程序"
    _write(
        root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"语音程序" = ""',
                '"主板程序" = "量产_默认"',
            ]
        ),
    )
    _write(root / "通用" / "语音程序" / "中文版" / "a.bin")
    _write(root / "通用" / "语音程序" / "英文版" / "b.bin")
    _write(root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    scheme = root / "定制" / "西班牙"
    _write(scheme / "方案配置.toml", 'name = "西班牙"\nplatform = "标准单机芯3D"\n')
    # 方案自带主板，缺语音 → 若错误回源会冒出中文版/英文版
    _write(scheme / "主板程序" / "custom.bin")

    model = _bind_model(root, tmp_path)
    cards = model.get_scheme_modules("L36", "西班牙")
    voice_any = [c for c in cards if c.asset.get("firmware_label") == "语音程序"]
    assert voice_any == [], (
        "empty default with multiple voice variants must not scheme-fallback; "
        f"got {[c.asset.get('directory_name') for c in voice_any]}"
    )
    # 主板为定制专属，不因语音异常而整树失败
    assert any(
        c.source_type == "custom_exclusive"
        and c.asset.get("firmware_label") == "主板程序"
        for c in cards
    )


def test_scheme_modules_empty_keyword_returns_full_tree(
    l36_tree: Path, tmp_path: Path
) -> None:
    """Issue 19-A：无 keyword 时方案满树（定制 + 回源），不因路径名关键字而缩水。"""
    model = _bind_model(l36_tree, tmp_path)
    full = model.get_scheme_modules("L36", "西班牙")
    labels = {str(c.asset.get("firmware_label", "")) for c in full}
    assert "主板程序" in labels
    # 西班牙缺 3D → 回源
    assert "3D机芯板程序" in labels


def test_scheme_modules_keyword_uses_tokenized_multi_field_filter(
    l36_tree: Path, tmp_path: Path
) -> None:
    """Issue 7/19：方案内 keyword 走分词多字段，不是仅 label/directory 整串。"""
    model = _bind_model(l36_tree, tmp_path)
    # 单 token 类型名应能命中
    by_type = model.get_scheme_modules("L36", "西班牙", keyword="主板")
    assert by_type
    assert all("主板" in str(c.asset.get("firmware_label", "")) for c in by_type)

    # 残留「以色列」类方案名词不应只靠文件名；西班牙方案无「以色列」→ 空
    residual = model.get_scheme_modules("L36", "西班牙", keyword="以色列")
    assert residual == []

    # 空串 = 满树
    assert len(model.get_scheme_modules("L36", "西班牙", keyword="")) >= len(by_type)


def test_scheme_keyword_does_not_fake_fallback_for_filtered_custom(
    l36_tree: Path, tmp_path: Path
) -> None:
    """搜仅命中回源模块的词时，不得把已有定制主板当「未覆盖」再回源一份。"""
    model = _bind_model(l36_tree, tmp_path)
    # 用足够长的类型标签，避免路径里偶然出现的短 token「3D」
    cards = model.get_scheme_modules("L36", "西班牙", keyword="3D机芯板")
    mainboards = [c for c in cards if c.asset.get("firmware_label") == "主板程序"]
    assert mainboards == [], (
        "filtered-out custom mainboard must not reappear as fallback"
    )
    assert any(c.asset.get("firmware_label") == "3D机芯板程序" for c in cards)


def test_unbound_model_falls_back_to_query_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If someone calls a view method before bind() (or with an empty cache),
    the model must still work by going to the DB. This protects against the
    'cache missed' regression where the view would silently return empty.
    """
    import fwasset.core.asset_index as asset_index_module
    import fwasset.ui_common.view_models.scheme_workbench_model as model_module

    assets = [
        _make_asset(
            path="/scan/X/通用/主板程序/量产_默认/f.bin",
            model="X",
            directory_name="量产_默认",
        )
    ]
    counter = _CountingQueryAssets(assets)
    monkeypatch.setattr(asset_index_module, "query_assets", counter)
    monkeypatch.setattr(model_module, "query_assets", counter)

    model = SchemeWorkbenchModel()  # never bound
    tree = model.build_sidebar_tree("X")
    assert tree["common"] == {"主板程序": 1}
    assert counter.calls, "unbound model must hit query_assets to stay correct"


def test_cache_invalidates_on_rebind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling bind() again must reload the cache (e.g. after a rescan added
    new assets). A stale cache from a previous bind is a silent bug — a
    rescan would not show up in the UI.
    """
    import fwasset.core.asset_index as asset_index_module
    import fwasset.ui_common.view_models.scheme_workbench_model as model_module

    first_assets = [_make_asset(path="/scan/L36程序/通用/主板程序/量产_默认/f.bin")]
    second_assets = first_assets + [
        _make_asset(
            path="/scan/L36程序/通用/语音程序/默认/v.bin",
            firmware_label="语音程序",
            directory_name="默认",
        )
    ]

    first = _CountingQueryAssets(first_assets)
    second = _CountingQueryAssets(second_assets)

    # Initial bind uses the first counter.
    monkeypatch.setattr(asset_index_module, "query_assets", first)
    monkeypatch.setattr(model_module, "query_assets", first)
    monkeypatch.setattr(model_module, "load_platform_config", lambda _dir: [])

    model = SchemeWorkbenchModel()
    model.bind(tmp_path / "index.db", tmp_path / "L36程序")
    assert "语音程序" not in model.build_sidebar_tree("L36")["common"]

    # Rebind against a new DB that has the new asset.
    monkeypatch.setattr(asset_index_module, "query_assets", second)
    monkeypatch.setattr(model_module, "query_assets", second)
    model.bind(tmp_path / "index2.db", tmp_path / "L36程序")
    assert "语音程序" in model.build_sidebar_tree("L36")["common"]


# ---------------------------------------------------------------------------
# 共享模块展示（shared_hit / shared_missing）
# ---------------------------------------------------------------------------


def _asset(path: Path, *, model_root: Path, model_name: str) -> FirmwareAsset:
    return {
        "series": "L36",
        "firmware_type": "shortcut_key",
        "firmware_label": "快捷键程序",
        "flash_mode": "auto_usb",
        "usb_flow": "directory_copy",
        "model": model_name,
        "version": "V1",
        "model_directory_name": model_root.name,
        "model_directory_path": str(model_root),
        "path": str(path),
        "directory_name": path.name,
        "files": [path.name + ".bin"],
        "modified_time": 0.0,
        "tool_name": "",
        "tool_path": "",
        "tool_dir": "",
        "label": "快捷键程序",
        "category": "common",
        "platform": "",
        "scheme_name": "",
        "scheme_path": "",
        "chassis_type": "",
        "vendor": "",
    }


def _bind_workspace(root: Path, db_path: Path) -> SchemeWorkbenchModel:
    assets, errors = scan_firmware_assets(str(root))
    assert not errors
    save_assets(assets, str(root), path=db_path)
    model = SchemeWorkbenchModel()
    model.bind(db_path, root)
    return model


def test_all_modules_decorate_existing_row_with_shared_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_root = workspace / "L36程序"
    target_root = workspace / "L36双机芯-上3D-下2D程序"
    _write(source_root / "型号配置.toml", 'model_id = "l36"\n')
    _write(target_root / "型号配置.toml", 'model_id = "l36-dual"\n')
    source_path = source_root / "通用" / "快捷键程序"
    target_path = target_root / "通用" / "快捷键程序"
    _write(source_path / "shortcut.bin")
    _write(target_path / "shortcut-local.bin")
    save_shared_module(
        target_root,
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l36",
            source_group="l36-common",
            source_module="快捷键程序",
            source_relative_path="L36程序/通用/快捷键程序",
        ),
    )

    model = _bind_workspace(workspace, tmp_path / "index.db")
    cards = model.get_all_modules("L36双机芯-上3D-下2D")
    shortcut = [card for card in cards if card.asset["firmware_label"] == "快捷键程序"]

    assert len(shortcut) == 1
    card = shortcut[0]
    assert card.shared_state == "shared_hit"
    assert card.shared_source_label == "\u6765\u81eaL36"
    assert card.asset["path"] == str(target_path)
    assert card.effective_asset is not None
    assert card.effective_asset["path"] == str(source_path)


def test_missing_shared_source_does_not_fall_back_to_local(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target_root = workspace / "L36双机芯-上3D-下2D程序"
    _write(target_root / "型号配置.toml", 'model_id = "l36-dual"\n')
    target_path = target_root / "通用" / "快捷键程序"
    _write(target_path / "shortcut-local.bin")
    save_shared_module(
        target_root,
        SharedModuleRef(
            module_key="快捷键程序",
            source_model_id="l36",
            source_group="l36-common",
            source_module="快捷键程序",
            source_relative_path="L36程序/通用/快捷键程序",
        ),
    )

    model = _bind_workspace(workspace, tmp_path / "index.db")
    cards = model.get_all_modules("L36双机芯-上3D-下2D")
    card = next(card for card in cards if card.asset["firmware_label"] == "快捷键程序")

    assert card.shared_state == "shared_missing"
    assert card.shared_reason == "source_not_imported"
    assert card.effective_asset is None


# ---------------------------------------------------------------------------
# 方案回源与共享边界
# ---------------------------------------------------------------------------


def _bind(root: Path, db_path: Path) -> SchemeWorkbenchModel:
    assets, errors = scan_firmware_assets(str(root))
    assert not errors
    save_assets(assets, str(root), path=db_path)
    model = SchemeWorkbenchModel()
    model.bind(db_path, root)
    return model


def _setup_dual_with_shared_shortcut(
    tmp_path: Path,
    *,
    with_local_copy: bool,
    with_source: bool = True,
    with_scheme_custom_shortcut: bool = False,
) -> tuple[SchemeWorkbenchModel, Path, Path]:
    """双机芯目标 + 可选 L36 来源；平台 defaults 含快捷键，方案无定制快捷键（除非指定）。"""
    workspace = tmp_path / "workspace"
    source_root = workspace / "L36程序"
    target_root = workspace / "L36双机芯-上3D-下2D程序"

    _write(
        target_root / "平台配置.toml",
        "\n".join(
            [
                "[[platform]]",
                'name = "标准单机芯3D"',
                "[platform.defaults]",
                '"主板程序" = "量产_默认"',
                '"快捷键程序" = ""',
            ]
        ),
    )
    _write(target_root / "通用" / "主板程序" / "量产_默认" / "main.bin")
    if with_local_copy:
        _write(target_root / "通用" / "快捷键" / "本地变体" / "local.hex")

    scheme = target_root / "定制" / "方案A"
    _write(scheme / "方案配置.toml", 'name = "方案A"\nplatform = "标准单机芯3D"\n')
    _write(scheme / "主板程序" / "custom-main.bin")
    if with_scheme_custom_shortcut:
        _write(scheme / "快捷键" / "custom-key.hex")

    save_model_id(target_root, "l36-dual")
    if with_source:
        _write(source_root / "通用" / "快捷键" / "贝乐" / "k.hex")
        save_model_id(source_root, "l36")
        save_shared_module(
            target_root,
            SharedModuleRef(
                module_key="快捷键程序",
                source_model_id="l36",
                source_group="l36-common",
                source_module="快捷键程序",
                source_relative_path="L36程序/通用/快捷键/贝乐",
            ),
        )
    else:
        save_shared_module(
            target_root,
            SharedModuleRef(
                module_key="快捷键程序",
                source_model_id="l36",
                source_group="l36-common",
                source_module="快捷键程序",
                source_relative_path="L36程序/通用/快捷键/贝乐",
            ),
        )

    model = _bind(workspace, tmp_path / "index.db")
    return model, target_root, source_root


def _scheme_labels(
    model: SchemeWorkbenchModel, model_name: str, scheme: str
) -> set[str]:
    tree = model.get_scheme_module_tree(model_name, scheme)
    labels = {row.label for row in tree}
    for row in tree:
        labels.update(str(v.asset.get("firmware_label", "")) for v in row.variants)
        labels.update(v.name for v in row.variants)
    cards = model.get_scheme_modules(model_name, scheme)
    labels.update(str(c.asset.get("firmware_label", "")) for c in cards)
    return {x for x in labels if x}


def test_scheme_skips_local_fallback_when_shared_hit_with_local_copy(
    tmp_path: Path,
) -> None:
    """有共享 + 有本地副本 + 方案无定制 → 方案树无该模块。"""
    model, _target, _source = _setup_dual_with_shared_shortcut(
        tmp_path, with_local_copy=True
    )
    labels = _scheme_labels(model, "L36双机芯-上3D-下2D", "方案A")
    assert not any("快捷键" in x for x in labels)
    assert not any(
        c.shared_state != "local"
        for c in model.get_scheme_modules("L36双机芯-上3D-下2D", "方案A")
    )


def test_scheme_skips_module_when_shared_without_local(tmp_path: Path) -> None:
    """有共享 + 无本地 + 方案无定制 → 方案树仍无该模块。"""
    model, _target, _source = _setup_dual_with_shared_shortcut(
        tmp_path, with_local_copy=False
    )
    labels = _scheme_labels(model, "L36双机芯-上3D-下2D", "方案A")
    assert not any("快捷键" in x for x in labels)


def test_scheme_keeps_custom_exclusive_despite_shared(tmp_path: Path) -> None:
    """有共享 + 方案有定制专属 → 仍显示定制专属。"""
    model, _target, _source = _setup_dual_with_shared_shortcut(
        tmp_path, with_local_copy=True, with_scheme_custom_shortcut=True
    )
    cards = model.get_scheme_modules("L36双机芯-上3D-下2D", "方案A")
    shortcut = [c for c in cards if "快捷键" in str(c.asset.get("firmware_label", ""))]
    assert len(shortcut) == 1
    assert shortcut[0].source_kind == "custom"
    assert shortcut[0].source_label == "定制专属"
    assert shortcut[0].shared_state == "local"


def test_shared_missing_does_not_fall_back_in_list_or_scheme(tmp_path: Path) -> None:
    """共享 missing + 有本地 → 列表 shared_missing；方案亦不回落本地。"""
    model, _target, _source = _setup_dual_with_shared_shortcut(
        tmp_path, with_local_copy=True, with_source=False
    )
    cards = model.get_all_modules("L36双机芯-上3D-下2D")
    shortcut = next(
        c for c in cards if "快捷键" in str(c.asset.get("firmware_label", ""))
    )
    assert shortcut.shared_state == "shared_missing"
    assert shortcut.effective_asset is None

    labels = _scheme_labels(model, "L36双机芯-上3D-下2D", "方案A")
    assert not any("快捷键" in x for x in labels)


def test_clear_shared_restores_scheme_fallback_and_local_list(tmp_path: Path) -> None:
    """取消共享后：列表恢复 local；方案可再回源本地通用。"""
    model, target_root, _source = _setup_dual_with_shared_shortcut(
        tmp_path, with_local_copy=True
    )
    local_file = target_root / "通用" / "快捷键" / "本地变体" / "local.hex"
    assert local_file.exists()

    result = model.unregister_shared_module("L36双机芯-上3D-下2D", "快捷键程序")
    assert result["ok"] is True
    assert local_file.exists()

    cards = model.get_all_modules("L36双机芯-上3D-下2D")
    shortcut = next(
        c for c in cards if "快捷键" in str(c.asset.get("firmware_label", ""))
    )
    assert shortcut.shared_state == "local"
    assert shortcut.effective_asset is None

    scheme_cards = model.get_scheme_modules("L36双机芯-上3D-下2D", "方案A")
    scheme_shortcut = [
        c for c in scheme_cards if "快捷键" in str(c.asset.get("firmware_label", ""))
    ]
    assert len(scheme_shortcut) == 1
    assert scheme_shortcut[0].is_fallback is True
    assert scheme_shortcut[0].source_kind == "common"
    assert "快捷键" in str(scheme_shortcut[0].asset.get("path", ""))


# ---------------------------------------------------------------------------
# _load_model_ids 工作区门闩
# ---------------------------------------------------------------------------


def test_load_model_ids_skips_mapping_on_out_of_workspace(tmp_path: Path):
    root = tmp_path / "L36程序"
    root.mkdir(parents=True)
    _write(root / "通用" / "主板程序" / "v1" / "rom.bin")
    other = tmp_path.parent / f"{tmp_path.name}_外"
    other.mkdir(exist_ok=True)
    model = SchemeWorkbenchModel()
    model.bind(tmp_path / "index.db", root, other)  # 配置根与扫描根不同
    model._load_model_ids()
    # 门闩拒绝：不建立映射
    assert model._model_id_by_dir == {}
    assert not (root / MODEL_CONFIG_FILENAME).exists()
