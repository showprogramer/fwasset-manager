"""Tests for resolve_shared_module — static / follow_default / follow_asset 解析。"""

from __future__ import annotations

from pathlib import Path

from fwasset.core.model_config import SharedModuleRef, save_model_id
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.shared_module_resolver import resolve_shared_module

# ---------------------------------------------------------------------------
# 辅助：static 模式
# ---------------------------------------------------------------------------


def _write(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _ref(
    path: str,
    sid: str = "l36",
    key: str = "快捷键程序",
) -> SharedModuleRef:
    return SharedModuleRef(
        module_key=key,
        source_model_id=sid,
        source_group="l36-single",
        source_module=key,
        source_relative_path=path,
    )


def _id_lookup(ws: Path) -> dict[str, Path]:
    """Build id → root from 型号配置.toml under workspace children + root."""
    mapping: dict[str, Path] = {}
    for child in [ws, *list(ws.iterdir())]:
        if not child.is_dir():
            continue
        from fwasset.core.model_config import load_model_config

        mid, status, _ = load_model_config(child)
        if status == "ok" and mid:
            mapping[mid] = child
    return mapping


# ---------------------------------------------------------------------------
# 辅助：follow_default 模式
# ---------------------------------------------------------------------------


def _make_source_root(ws: Path, dir_name: str, model_id: str) -> Path:
    root = ws / dir_name
    root.mkdir(parents=True, exist_ok=True)
    save_model_id(root, model_id)
    return root


def _make_variant(module_dir: Path, variant_name: str) -> Path:
    v = module_dir / variant_name
    v.mkdir(parents=True, exist_ok=True)
    (v / "fw.bin").write_bytes(b"X")
    return v


def _make_ref(
    *,
    source_root_dir: str,
    module_rel: str,
    source_model_id: str = "l50s",
    source_module: str = "手控UI",
    mode: str = "follow_default",
    source_platform: str = "",
) -> SharedModuleRef:
    return SharedModuleRef(
        module_key=source_module,
        source_model_id=source_model_id,
        source_group=source_model_id,
        source_module=source_module,
        source_relative_path=f"{source_root_dir}/{module_rel}",
        mode=mode,  # type: ignore[arg-type]
        source_platform=source_platform,
    )


# ---------------------------------------------------------------------------
# 辅助：follow_asset 模式
# ---------------------------------------------------------------------------


def _follow_asset_ref(
    rel: str, sid: str = "l36", key: str = "快捷键程序"
) -> SharedModuleRef:
    return SharedModuleRef(
        module_key=key,
        source_model_id=sid,
        source_group=sid,
        source_module=key,
        source_relative_path=rel,
        mode="follow_asset",
    )


# ---------------------------------------------------------------------------
# static 模式解析
# ---------------------------------------------------------------------------


def test_hit_leaf_variant(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    leaf = src / "通用" / "快捷键" / "贝乐"
    _write(leaf / "key.hex")
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键/贝乐"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "hit"
    assert res.resolved_path == leaf.resolve()
    assert res.variants == [leaf.resolve()]


def test_hit_module_multi_variant(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    mod = src / "通用" / "快捷键"
    _write(mod / "贝乐" / "a.hex")
    _write(mod / "量产_默认" / "b.hex")
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "hit"
    names = [p.name for p in res.variants]
    assert names == ["贝乐", "量产_默认"]


def test_hit_module_files_only(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    mod = src / "通用" / "腿部程序"
    _write(mod / "leg.hex")
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/腿部程序", key="腿部程序"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "hit"
    assert res.variants == [mod.resolve()]


def test_variants_skip_noise_dirs(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    mod = src / "通用" / "快捷键"
    _write(mod / "贝乐" / "a.hex")
    _write(mod / "backup" / "old.hex")
    _write(mod / "旧" / "old2.hex")
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键"),
        ws,
        lambda i: lookup.get(i),
    )
    assert [p.name for p in res.variants] == ["贝乐"]


def test_missing_path_not_found(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    src.mkdir(parents=True)
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/不存在"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "missing"
    assert res.reason == "path_not_found"


def test_missing_source_not_imported(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = resolve_shared_module(
        _ref("L50程序/通用/手控"),
        ws,
        lambda _i: None,
    )
    assert res.status == "missing"
    assert res.reason == "source_not_imported"


def test_missing_id_mismatch(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    leaf = src / "通用" / "快捷键" / "贝乐"
    _write(leaf / "a.hex")
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键/贝乐", sid="other-id"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "missing"
    assert res.reason == "id_mismatch"


def test_out_of_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    outside = tmp_path / "outside" / "secret"
    _write(outside / "x.bin")
    src = ws / "L36程序"
    src.mkdir(parents=True)
    save_model_id(src, "l36")
    lookup = _id_lookup(ws)
    res = resolve_shared_module(
        _ref("../outside/secret"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "missing"
    assert res.reason == "out_of_workspace"


def test_dotdot_cannot_bypass_source_model_id_check(tmp_path: Path):
    """P1：L36/../L50/... 不得因只校验首段 L36 的 id 而误 hit 到 L50 真身。"""
    ws = tmp_path / "ws"
    l36 = ws / "L36程序"
    l50 = ws / "L50程序"
    l36.mkdir(parents=True)
    _write(l50 / "通用" / "快捷键" / "贝乐" / "k.hex")
    save_model_id(l36, "l36")
    save_model_id(l50, "l50")
    lookup = _id_lookup(ws)

    # 声称源是 l36，路径却用 .. 跳到 L50 实物
    sneaky = "L36程序/../L50程序/通用/快捷键/贝乐"
    res = resolve_shared_module(
        _ref(sneaky, sid="l36"),
        ws,
        lambda i: lookup.get(i),
    )
    assert res.status == "missing", res
    assert res.reason == "out_of_workspace"
    assert res.resolved_path is None

    # 直接指 L50 且 id=l50 仍应 hit（正常路径）
    ok = resolve_shared_module(
        _ref("L50程序/通用/快捷键/贝乐", sid="l50", key="快捷键程序"),
        ws,
        lambda i: lookup.get(i),
    )
    assert ok.status == "hit"


def test_stale_id_map_does_not_force_mismatch(tmp_path: Path):
    """审查 #3：盘上 model_id 相符即 hit，不因 bind 映射滞后/异路径而误判 id_mismatch。"""
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    leaf = src / "通用" / "快捷键" / "贝乐"
    _write(leaf / "k.hex")
    save_model_id(src, "l36")
    # 映射函数故意返回一个与 source_dir 不同的路径（模拟滞后/异规范化）
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键/贝乐", sid="l36"),
        ws,
        lambda _i: tmp_path / "别处" / "L36程序",
    )
    assert res.status == "hit", res
    assert res.resolved_path == leaf.resolve()


def test_resolver_without_id_map(tmp_path: Path):
    """审查 #3：root_for_model_id 可省略，盘上校验仍成立。"""
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    leaf = src / "通用" / "快捷键" / "贝乐"
    _write(leaf / "k.hex")
    save_model_id(src, "l36")
    res = resolve_shared_module(_ref("L36程序/通用/快捷键/贝乐", sid="l36"), ws)
    assert res.status == "hit"


def test_no_cross_model_search(tmp_path: Path):
    """源段缺失时不落到另一型号同名路径。"""
    ws = tmp_path / "ws"
    other = ws / "L50程序" / "通用" / "快捷键" / "贝乐"
    _write(other / "a.hex")
    save_model_id(ws / "L50程序", "l50")
    # 引用声称 l36 但 L36 根不存在
    res = resolve_shared_module(
        _ref("L36程序/通用/快捷键/贝乐", sid="l36"),
        ws,
        lambda _i: None,
    )
    assert res.status == "missing"
    assert res.reason == "source_not_imported"
    assert res.resolved_path is None


# ---------------------------------------------------------------------------
# follow_default 模式
# ---------------------------------------------------------------------------
# 1. follow_default — 显式 source_platform，命中正确变体
# ---------------------------------------------------------------------------


def test_follow_default_explicit_platform_hit(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    _make_variant(module_dir, "v3.0")
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"手控UI": "v2.0"}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == (src / "通用" / "手控UI" / "v2.0").resolve()
    assert res.variants == [res.resolved_path]


# ---------------------------------------------------------------------------
# 2. follow_default — 空 source_platform，自动检测（第一个含该模块的块）
# ---------------------------------------------------------------------------


def test_follow_default_auto_detect_platform(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v3.0")
    save_platform_config(
        src,
        [
            PlatformDefaults("无关平台", {}),  # 无该模块，跳过
            PlatformDefaults("标准单机芯", {"手控UI": "v3.0"}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == (src / "通用" / "手控UI" / "v3.0").resolve()


def test_follow_default_prefers_canonical_shortcut_key(tmp_path: Path):
    """快捷键短键与规范键并存时，自动更新采用规范键的默认变体。"""
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "快捷键"
    _make_variant(module_dir, "量产_默认")
    _make_variant(module_dir, "贝乐")
    save_platform_config(
        src,
        [
            PlatformDefaults(
                "标准单机芯", {"快捷键": "量产_默认", "快捷键程序": "贝乐"}
            ),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/快捷键",
        source_module="快捷键程序",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == (module_dir / "贝乐").resolve()


# ---------------------------------------------------------------------------
# 3. follow_default — defaults[module] = ""（唯一变体），resolved = 模块目录本身
# ---------------------------------------------------------------------------


def test_follow_default_leaf_module(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "语音程序"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "voice.bin").write_bytes(b"V")
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"语音程序": ""}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/语音程序",
        source_module="语音程序",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == module_dir.resolve()


# ---------------------------------------------------------------------------
# 4. follow_default — 源无 平台配置.toml → missing, no_source_platform
# ---------------------------------------------------------------------------


def test_follow_default_no_platform_config(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    # 不写 平台配置.toml

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "no_source_platform"


# ---------------------------------------------------------------------------
# 5. follow_default — 显式 source_platform 不存在于平台配置 → missing, no_source_platform
# ---------------------------------------------------------------------------


def test_follow_default_explicit_platform_not_found(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"手控UI": "v2.0"}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="不存在的平台",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "no_source_platform"


# ---------------------------------------------------------------------------
# 6. follow_default — 平台块存在但模块不在 defaults → missing, no_source_default
# ---------------------------------------------------------------------------


def test_follow_default_no_source_default(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"主板程序": "v1.0"}),  # 没有 手控UI
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "no_source_default"


# ---------------------------------------------------------------------------
# 7. follow_default — 自动检测回落首块，但首块也无该模块 → no_source_default
# ---------------------------------------------------------------------------


def test_follow_default_auto_detect_falls_back_to_first_block(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"主板程序": "v1.0"}),  # 无 手控UI
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "no_source_default"


# ---------------------------------------------------------------------------
# 8. follow_default — defaults 指向的变体目录不存在 → missing, path_not_found
# ---------------------------------------------------------------------------


def test_follow_default_variant_dir_not_found(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    module_dir.mkdir(parents=True, exist_ok=True)
    # 不创建 v99 目录
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"手控UI": "v99"}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "path_not_found"


# ---------------------------------------------------------------------------
# 9. static 行为不退化
# ---------------------------------------------------------------------------


def test_static_behavior_unchanged(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "手控UI"
    v1 = _make_variant(module_dir, "v1.0")
    v2 = _make_variant(module_dir, "v2.0")

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/手控UI",
        mode="static",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == module_dir.resolve()
    assert set(res.variants) == {v1.resolve(), v2.resolve()}


# ---------------------------------------------------------------------------
# 10. follow_default — model_id 不符仍返回 id_mismatch
# ---------------------------------------------------------------------------


def test_follow_default_id_mismatch(tmp_path: Path):
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s-real")  # 盘上 id 是 l50s-real
    module_dir = src / "通用" / "手控UI"
    _make_variant(module_dir, "v2.0")
    save_platform_config(src, [PlatformDefaults("标准单机芯", {"手控UI": "v2.0"})])

    ref = SharedModuleRef(
        module_key="手控UI",
        source_model_id="l50s-wrong",  # 与盘上不符
        source_group="l50s-wrong",
        source_module="手控UI",
        source_relative_path="L50S程序/通用/手控UI",
        mode="follow_default",
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "missing"
    assert res.reason == "id_mismatch"


# ---------------------------------------------------------------------------
# 11. follow_default — 目录名（快捷键）与 catalog label（快捷键程序）匹配
# ---------------------------------------------------------------------------


def test_follow_default_catalog_label_vs_dir_name(tmp_path: Path):
    """默认键写「快捷键」但 source_module 是「快捷键程序」→ 规范化匹配命中。"""
    ws = tmp_path / "ws"
    src = _make_source_root(ws, "L50S程序", "l50s")
    module_dir = src / "通用" / "快捷键"
    _make_variant(module_dir, "量产_默认")
    # 键名用目录名「快捷键」
    save_platform_config(
        src,
        [
            PlatformDefaults("标准单机芯", {"快捷键": "量产_默认"}),
        ],
    )

    ref = _make_ref(
        source_root_dir="L50S程序",
        module_rel="通用/快捷键",
        source_module="快捷键程序",  # catalog label
        source_platform="标准单机芯",
    )
    res = resolve_shared_module(ref, ws)
    assert res.status == "hit"
    assert res.resolved_path == (module_dir / "量产_默认").resolve()


# ---------------------------------------------------------------------------
# follow_asset 模式
# ---------------------------------------------------------------------------


def test_follow_asset_hit_no_variant_expansion(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    variant = src / "通用" / "快捷键" / "贝乐"
    _write(variant / "key.hex")
    save_model_id(src, "l36")
    res = resolve_shared_module(_follow_asset_ref("L36程序/通用/快捷键/贝乐"), ws)
    assert res.status == "hit"
    assert res.resolved_path == variant.resolve()
    # 跟随指定程序：锚点本身，不展开子变体
    assert res.variants == [variant.resolve()]


def test_follow_asset_missing_when_anchor_gone(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    src.mkdir(parents=True)
    save_model_id(src, "l36")
    res = resolve_shared_module(_follow_asset_ref("L36程序/通用/快捷键/贝乐"), ws)
    assert res.status == "missing" and res.reason == "path_not_found"


def test_follow_asset_id_mismatch(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    variant = src / "通用" / "快捷键" / "贝乐"
    _write(variant / "key.hex")
    save_model_id(src, "l50")
    res = resolve_shared_module(_follow_asset_ref("L36程序/通用/快捷键/贝乐"), ws)
    assert res.status == "missing" and res.reason == "id_mismatch"


def test_follow_asset_out_of_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    src = ws / "L36程序"
    src.mkdir(parents=True)
    save_model_id(src, "l36")
    res = resolve_shared_module(_follow_asset_ref("L36程序/../其他/贝乐"), ws)
    assert res.status == "missing" and res.reason == "out_of_workspace"
