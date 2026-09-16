"""Tests for shared_module_service — set/clear 登记与 mode / source_platform 扩展。"""

from __future__ import annotations

from pathlib import Path

from fwasset.core.model_config import (
    load_model_config,
    load_shared_modules,
    save_model_id,
)
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.services import shared_module_service as sms
from fwasset.core.services.shared_module_service import (
    clear_shared_module,
    set_shared_module,
)
from fwasset.core.types import FirmwareAsset

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_asset(
    asset_dir: Path,
    *,
    firmware_type: str = "shortcut_key",
    firmware_label: str = "快捷键程序",
    model: str = "L36",
) -> FirmwareAsset:
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "firmware.bin").write_bytes(b"BIN")
    model_dir = (
        asset_dir.parents[1]
        if "通用" in asset_dir.parts or "定制" in asset_dir.parts
        else asset_dir.parent
    )
    return {
        "series": model,
        "firmware_type": firmware_type,  # type: ignore[typeddict-item]
        "firmware_label": firmware_label,
        "flash_mode": "tool_launch",
        "usb_flow": "",
        "model": model,
        "version": "V1.0.0",
        "model_directory_name": model_dir.name,
        "model_directory_path": str(model_dir),
        "path": str(asset_dir),
        "directory_name": asset_dir.name,
        "files": ["firmware.bin"],
        "modified_time": 123.0,
        "tool_name": "writer",
        "tool_path": "D:/tools/writer.exe",
        "tool_dir": "writer",
        "label": f"{model} V1.0.0 [{asset_dir.name}]",
        "category": "common",
        "platform": "",
        "scheme_name": "",
        "scheme_path": "",
    }


def _setup_multi_model_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, Path, FirmwareAsset]:
    """Multi-model root: ws / L36程序/通用/快捷键/贝乐  +  ws / 双机芯程序/."""
    ws = tmp_path / "ws"
    src_root = ws / "L36程序"
    src_variant = src_root / "通用" / "快捷键" / "贝乐"
    tgt_root = ws / "双机芯程序"
    src_variant.mkdir(parents=True, exist_ok=True)
    tgt_root.mkdir(parents=True, exist_ok=True)
    (src_variant / "key.hex").write_bytes(b"X")
    save_model_id(src_root, "l36")
    asset = make_asset(src_variant, model="L36")
    return ws, src_root, tgt_root, asset


def _make_asset(
    path: Path, *, firmware_label: str = "手控UI", model: str = "L50S"
) -> FirmwareAsset:
    path.mkdir(parents=True, exist_ok=True)
    (path / "fw.bin").write_bytes(b"X")
    return {
        "series": model,
        "model": model,
        "version": "V1.0",
        "firmware_type": "handcontrol_ui",  # type: ignore[typeddict-item]
        "firmware_label": firmware_label,
        "flash_mode": "tool_launch",
        "usb_flow": "",
        "model_directory_name": path.parents[1].name,
        "model_directory_path": str(path.parents[1]),
        "path": str(path),
        "directory_name": path.name,
        "files": ["fw.bin"],
        "modified_time": 0.0,
        "tool_name": "t",
        "tool_path": "t.exe",
        "tool_dir": "t",
        "label": "lbl",
        "category": "common",
        "platform": "",
        "scheme_name": "",
        "scheme_path": "",
    }


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """ws / L50S程序（src）/ 双机芯程序（tgt）"""
    ws = tmp_path / "ws"
    src = ws / "L50S程序"
    tgt = ws / "双机芯程序"
    src.mkdir(parents=True, exist_ok=True)
    tgt.mkdir(parents=True, exist_ok=True)
    save_model_id(src, "l50s")
    return ws, src, tgt


def _setup_with_platform(tmp_path: Path) -> tuple[Path, Path, Path]:
    """同上，并写入平台配置。"""
    ws, src, tgt = _setup(tmp_path)
    save_platform_config(src, [PlatformDefaults("标准单机芯", {})])
    return ws, src, tgt


def _asset(path: Path, label: str = "快捷键程序") -> FirmwareAsset:
    return FirmwareAsset(
        series="",
        firmware_type="shortcut_key",
        firmware_label=label,
        flash_mode="disabled",
        usb_flow="",
        model="",
        version="",
        model_directory_name="L36",
        model_directory_path=str(path.parent.parent.parent),
        path=str(path),
        directory_name=path.name,
        files=[],
        modified_time=0.0,
        tool_name="",
        tool_path="",
        tool_dir="",
        label="",
        category="common",
        platform="",
        scheme_name="",
        scheme_path="",
    )


# ---------- Task 1: set_shared_module ----------


def test_set_shared_module_writes_ref(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    result = set_shared_module(tgt_root, asset, ws)
    assert result["ok"] is True
    assert result["code"] == "ok"
    refs = load_shared_modules(tgt_root)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.module_key == "快捷键程序"
    assert ref.source_model_id == "l36"
    assert ref.source_module == "快捷键程序"
    assert ref.source_relative_path == "L36程序/通用/快捷键/贝乐"
    # target config preserves nothing else but exists
    assert (tgt_root / "型号配置.toml").exists()


def test_source_relative_path_format(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    set_shared_module(tgt_root, asset, ws)
    refs = load_shared_modules(tgt_root)
    assert refs[0].source_relative_path == "L36程序/通用/快捷键/贝乐"
    # 含源型号目录段；统一正斜杠
    assert "/" in refs[0].source_relative_path
    assert "\\" not in refs[0].source_relative_path


def test_source_no_id(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    # 删除源型号根的 model_id（删掉 型号配置.toml）
    (src_root / "型号配置.toml").unlink()
    result = set_shared_module(tgt_root, asset, ws)
    assert result["ok"] is False
    assert result["code"] == "source_no_id"
    # 不写
    assert not (tgt_root / "型号配置.toml").exists()


def test_out_of_workspace(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    # 资产路径在工作区外
    outside = tmp_path / "outside" / "L36程序" / "通用" / "快捷键" / "贝乐"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "key.hex").write_bytes(b"Y")
    bad_asset = make_asset(outside, model="L36")
    result = set_shared_module(tgt_root, bad_asset, ws)
    assert result["ok"] is False
    assert result["code"] == "out_of_workspace"
    assert not (tgt_root / "型号配置.toml").exists()


def test_conflict_no_overwrite(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    # 首次写入
    set_shared_module(tgt_root, asset, ws)
    # 第二次同模块、不同来源（改 path），overwrite=False → 冲突，不写
    other_variant = src_root / "通用" / "快捷键" / "量产_默认"
    other_variant.mkdir(parents=True, exist_ok=True)
    (other_variant / "key.hex").write_bytes(b"Z")
    other_asset = make_asset(other_variant, model="L36")
    result = set_shared_module(tgt_root, other_asset, ws, overwrite=False)
    assert result["ok"] is False
    assert result["code"] == "conflict"
    # payload 带现有引用
    existing = result["payload"].get("existing")
    assert existing is not None
    # 原引用不变
    refs = load_shared_modules(tgt_root)
    assert len(refs) == 1
    assert refs[0].source_relative_path == "L36程序/通用/快捷键/贝乐"


def test_conflict_overwrite(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    set_shared_module(tgt_root, asset, ws)
    other_variant = src_root / "通用" / "快捷键" / "量产_默认"
    other_variant.mkdir(parents=True, exist_ok=True)
    (other_variant / "key.hex").write_bytes(b"Z")
    other_asset = make_asset(other_variant, model="L36")
    result = set_shared_module(tgt_root, other_asset, ws, overwrite=True)
    assert result["ok"] is True
    refs = load_shared_modules(tgt_root)
    assert len(refs) == 1
    assert refs[0].source_relative_path == "L36程序/通用/快捷键/量产_默认"


def test_module_key_inferred_from_asset(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    result = set_shared_module(tgt_root, asset, ws, module_key="")
    assert result["ok"] is True
    refs = load_shared_modules(tgt_root)
    assert refs[0].module_key == "快捷键程序"  # 来自 firmware_label 规范化


def test_module_key_canonicalization(tmp_path: Path):
    """键规范化：传「机芯版」→ 落盘为「机芯板」。"""
    ws = tmp_path / "ws"
    src_root = ws / "L36程序"
    src_mod = src_root / "通用" / "机芯板" / "V1"
    tgt_root = ws / "双机芯程序"
    src_mod.mkdir(parents=True, exist_ok=True)
    tgt_root.mkdir(parents=True, exist_ok=True)
    (src_mod / "a.hex").write_bytes(b"X")
    save_model_id(src_root, "l36")
    asset = make_asset(
        src_mod, firmware_type="movement_3d", firmware_label="机芯版", model="L36"
    )
    result = set_shared_module(tgt_root, asset, ws, module_key="机芯版")
    assert result["ok"] is True
    refs = load_shared_modules(tgt_root)
    assert refs[0].module_key == "机芯板"


def test_rejects_source_module_mismatch_without_writing(tmp_path: Path):
    """来源模块与目标模块不同，不得登记或创建配置。"""
    ws, _src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)

    result = set_shared_module(tgt_root, asset, ws, module_key="蓝牙程序")

    assert result["ok"] is False
    assert result["code"] == "module_mismatch"
    assert result["payload"] == {
        "module_key": "蓝牙程序",
        "source_module": "快捷键程序",
    }
    assert not (tgt_root / "型号配置.toml").exists()


def test_rejects_source_without_recognizable_module(tmp_path: Path):
    """来源资产没有模块名时，不能由目标模块名补足。"""
    ws, _src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    asset["firmware_label"] = ""

    result = set_shared_module(tgt_root, asset, ws, module_key="快捷键程序")

    assert result["ok"] is False
    assert result["code"] == "invalid_args"
    assert not (tgt_root / "型号配置.toml").exists()


def test_write_failed_keeps_original(tmp_path: Path, monkeypatch):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    # 先正常写一次，建立原文件
    set_shared_module(tgt_root, asset, ws)
    original_text = (tgt_root / "型号配置.toml").read_text(encoding="utf-8")

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(sms, "save_shared_module", _boom)
    other_variant = src_root / "通用" / "快捷键" / "量产_默认"
    other_variant.mkdir(parents=True, exist_ok=True)
    (other_variant / "key.hex").write_bytes(b"Z")
    other_asset = make_asset(other_variant, model="L36")
    result = set_shared_module(tgt_root, other_asset, ws, overwrite=True)
    assert result["ok"] is False
    assert result["code"] == "write_failed"
    # 原文件不变
    assert (tgt_root / "型号配置.toml").read_text(encoding="utf-8") == original_text


def test_invalid_args_empty_target(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    result = set_shared_module("", asset, ws)
    assert result["ok"] is False
    assert result["code"] == "invalid_args"


def test_invalid_args_empty_asset(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    empty_asset = make_asset(src_root / "通用" / "快捷键" / "贝乐", model="L36")
    empty_asset["path"] = ""
    result = set_shared_module(tgt_root, empty_asset, ws)
    assert result["ok"] is False
    assert result["code"] == "invalid_args"


# ---------- Task 2: clear_shared_module ----------


def test_clear_shared_module_removes_ref(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    set_shared_module(tgt_root, asset, ws)
    # 预存 model_id 以验证删除后保留
    save_model_id(tgt_root, "dual")
    result = clear_shared_module(tgt_root, "快捷键程序", ws)
    assert result["ok"] is True
    assert result["code"] == "ok"
    refs = load_shared_modules(tgt_root)
    assert len(refs) == 0
    # model_id 保留
    mid, status, _ = load_model_config(tgt_root)
    assert status == "ok" and mid == "dual"


def test_clear_nonexistent_key_idempotent(tmp_path: Path):
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    # 无 型号配置.toml 的根
    result = clear_shared_module(tgt_root, "蓝牙程序", ws)
    assert result["ok"] is True
    # 不建空文件
    assert not (tgt_root / "型号配置.toml").exists()


def test_clear_does_not_delete_firmware_files(tmp_path: Path):
    """取消共享只删 toml 条目，不删固件文件。"""
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    set_shared_module(tgt_root, asset, ws)
    # 目标型号本地有同模块 bin 文件
    local_mod = tgt_root / "通用" / "快捷键" / "本地变体"
    local_mod.mkdir(parents=True, exist_ok=True)
    local_file = local_mod / "local.hex"
    local_file.write_bytes(b"LOCAL")
    clear_shared_module(tgt_root, "快捷键程序", ws)
    # 本地固件文件仍在磁盘
    assert local_file.exists()
    assert local_file.read_bytes() == b"LOCAL"


def test_set_shared_module_out_of_workspace_target(tmp_path: Path):
    """目标型号根在工作区外 → out_of_workspace 且不落盘。"""
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_外部"
    outside.mkdir(exist_ok=True)
    result = set_shared_module(outside, asset, ws)
    assert result["code"] == "out_of_workspace"
    assert not (outside / "型号配置.toml").exists()


def test_clear_shared_module_out_of_workspace_target(tmp_path: Path):
    """取消登记目标根在工作区外 → out_of_workspace。"""
    ws, src_root, tgt_root, asset = _setup_multi_model_workspace(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_外部"
    outside.mkdir(exist_ok=True)
    result = clear_shared_module(outside, "蓝牙程序", ws)
    assert result["code"] == "out_of_workspace"


# ---------- mode / source_platform 扩展 ----------


def test_follow_default_stores_module_dir(tmp_path: Path):
    ws, src, tgt = _setup_with_platform(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)

    result = set_shared_module(tgt, asset, ws, mode="follow_default")
    assert result["ok"] is True, result["message"]

    refs = load_shared_modules(tgt)
    assert refs[0].source_relative_path == "L50S程序/通用/手控UI"
    assert refs[0].mode == "follow_default"


def test_follow_default_leaf_asset_stores_asset_path(tmp_path: Path):
    ws, src, tgt = _setup_with_platform(tmp_path)
    module_dir = src / "通用" / "语音程序"
    asset = _make_asset(module_dir, firmware_label="语音程序")

    result = set_shared_module(tgt, asset, ws, mode="follow_default")
    assert result["ok"] is True, result["message"]

    refs = load_shared_modules(tgt)
    assert refs[0].source_relative_path == "L50S程序/通用/语音程序"
    assert refs[0].mode == "follow_default"


def test_follow_default_with_valid_source_platform(tmp_path: Path):
    ws, src, tgt = _setup(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)
    save_platform_config(src, [PlatformDefaults("标准单机芯", {"手控UI": "v2.0"})])

    result = set_shared_module(
        tgt, asset, ws, mode="follow_default", source_platform="标准单机芯"
    )
    assert result["ok"] is True, result["message"]

    refs = load_shared_modules(tgt)
    assert refs[0].source_platform == "标准单机芯"


def test_follow_default_invalid_source_platform(tmp_path: Path):
    ws, src, tgt = _setup(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)
    save_platform_config(src, [PlatformDefaults("标准单机芯", {})])

    result = set_shared_module(
        tgt, asset, ws, mode="follow_default", source_platform="不存在的平台"
    )
    assert result["ok"] is False
    assert result["code"] == "invalid_args"


def test_follow_default_auto_detect_with_platform_config(tmp_path: Path):
    ws, src, tgt = _setup_with_platform(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)

    result = set_shared_module(
        tgt, asset, ws, mode="follow_default", source_platform=""
    )
    assert result["ok"] is True


def test_follow_default_no_platform_config_rejected(tmp_path: Path):
    ws, src, tgt = _setup(tmp_path)  # 无平台配置
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)

    result = set_shared_module(
        tgt, asset, ws, mode="follow_default", source_platform=""
    )
    assert result["ok"] is False
    assert result["code"] == "no_platform_config"


def test_static_mode_stores_variant_path(tmp_path: Path):
    ws, src, tgt = _setup(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)

    result = set_shared_module(tgt, asset, ws, mode="static")
    assert result["ok"] is True

    refs = load_shared_modules(tgt)
    assert refs[0].source_relative_path == "L50S程序/通用/手控UI/v2.0"
    assert refs[0].mode == "static"


def test_static_ignores_source_platform(tmp_path: Path):
    ws, src, tgt = _setup(tmp_path)
    variant = src / "通用" / "手控UI" / "v2.0"
    asset = _make_asset(variant)

    result = set_shared_module(
        tgt, asset, ws, mode="static", source_platform="任意平台名"
    )
    assert result["ok"] is True

    refs = load_shared_modules(tgt)
    assert refs[0].source_platform == ""  # 未写入


# ---------- follow_asset 登记 ----------


def test_set_shared_module_follow_asset(tmp_path: Path):
    ws = tmp_path / "ws"
    src_root = ws / "L36程序"
    tgt_root = ws / "L50程序"
    src_root.mkdir(parents=True)
    tgt_root.mkdir(parents=True)
    save_model_id(src_root, "l36")
    save_model_id(tgt_root, "l50")
    variant = src_root / "通用" / "快捷键" / "贝乐"
    variant.mkdir(parents=True)

    res = set_shared_module(
        tgt_root,
        _asset(variant),
        ws,
        mode="follow_asset",
        log_fn=lambda _m: None,
    )
    assert res["ok"] is True, res["message"]
    refs = {r.module_key: r for r in load_shared_modules(tgt_root)}
    ref = refs["快捷键程序"]
    assert ref.mode == "follow_asset"
    assert ref.source_relative_path == "L36程序/通用/快捷键/贝乐"
    assert ref.source_platform == ""
    # 落盘格式：mode 键存在（非 static）
    text = (tgt_root / "型号配置.toml").read_text(encoding="utf-8")
    assert 'mode = "follow_asset"' in text


def test_set_shared_module_invalid_mode_falls_back_static(tmp_path: Path):
    ws = tmp_path / "ws"
    src_root = ws / "L36程序"
    tgt_root = ws / "L50程序"
    src_root.mkdir(parents=True)
    tgt_root.mkdir(parents=True)
    save_model_id(src_root, "l36")
    save_model_id(tgt_root, "l50")
    variant = src_root / "通用" / "快捷键" / "贝乐"
    variant.mkdir(parents=True)

    res = set_shared_module(
        tgt_root,
        _asset(variant),
        ws,
        mode="follow_nonsense",
        log_fn=lambda _m: None,
    )
    assert res["ok"] is True
    refs = {r.module_key: r for r in load_shared_modules(tgt_root)}
    assert refs["快捷键程序"].mode == "static"
