from pathlib import Path

import fwasset.core.asset_helpers as asset_helpers_module
from fwasset.core.asset_helpers import (
    asset_dir_path,
    asset_flash_mode,
    asset_primary_file_name,
    asset_primary_file_path,
    asset_rom_pkg_files,
    asset_usb_flow,
    open_path_in_explorer,
)


def _asset(**overrides) -> dict:
    base = {
        "series": "L36",
        "firmware_type": "mainboard",
        "firmware_label": "主板程序",
        "flash_mode": "auto_usb",
        "usb_flow": "",
        "model": "L36",
        "version": "V1.0.0",
        "model_directory_name": "L36配置",
        "model_directory_path": "D:/root/L36配置",
        "path": "D:/root/L36配置/主板程序/V1.0.0",
        "directory_name": "V1.0.0",
        "files": ["main.bin"],
        "modified_time": 0,
        "tool_name": "",
        "tool_path": "",
        "tool_dir": "",
        "label": "L36 V1.0.0",
    }
    base.update(overrides)
    return base


def test_asset_usb_flow_returns_configured_flow():
    asset = _asset(usb_flow="directory_copy")
    assert asset_usb_flow(asset) == "directory_copy"


def test_asset_usb_flow_infer_paired_files_from_type():
    assert (
        asset_usb_flow(_asset(firmware_type="handcontrol_ui", usb_flow=""))
        == "paired_files"
    )


def test_asset_usb_flow_segmented_screen_is_not_usb():
    """断码屏手控走烧录工具，不是U盘流程——区别于普通手控UI。"""
    assert asset_usb_flow(_asset(firmware_type="segmented_screen", usb_flow="")) == ""


def test_asset_flash_mode_forces_segmented_screen_to_tool_launch():
    """断码屏即便残留 auto_usb 配置，也强制走烧录工具。"""
    assert (
        asset_flash_mode(
            _asset(firmware_type="segmented_screen", flash_mode="auto_usb")
        )
        == "tool_launch"
    )


def test_asset_usb_flow_infer_directory_copy_from_music():
    assert (
        asset_usb_flow(_asset(firmware_type="music_files", usb_flow=""))
        == "directory_copy"
    )


def test_asset_usb_flow_never_uses_directory_copy_for_bluetooth():
    assert (
        asset_usb_flow(_asset(firmware_type="music_bt", usb_flow="directory_copy"))
        == ""
    )


def test_asset_flash_mode_forces_bluetooth_to_tool_launch():
    assert (
        asset_flash_mode(_asset(firmware_type="music_bt", flash_mode="auto_usb"))
        == "tool_launch"
    )
    assert (
        asset_flash_mode(_asset(firmware_type="music_files", flash_mode="auto_usb"))
        == "auto_usb"
    )


def test_asset_usb_flow_returns_empty_for_unknown():
    assert asset_usb_flow(_asset(firmware_type="mainboard", usb_flow="")) == ""


def test_asset_usb_flow_configured_overrides_inferred():
    asset = _asset(firmware_type="handcontrol_ui", usb_flow="directory_copy")
    assert asset_usb_flow(asset) == "directory_copy"


def test_asset_rom_pkg_files_extracts_rom_and_pkg():
    asset = _asset(files=["main.bin", "program.rom", "data.pkg", "readme.txt"])
    rom, pkg = asset_rom_pkg_files(asset)
    assert rom == "program.rom"
    assert pkg == "data.pkg"


def test_asset_rom_pkg_files_returns_empty_when_missing():
    asset = _asset(files=["main.bin"])
    rom, pkg = asset_rom_pkg_files(asset)
    assert rom == ""
    assert pkg == ""


def test_asset_dir_path_returns_path():
    asset = _asset(path="D:/root/L36/主板/V1.0")
    assert asset_dir_path(asset) == "D:/root/L36/主板/V1.0"


def test_asset_dir_path_returns_empty_when_missing():
    assert asset_dir_path(_asset(path="")) == ""


def test_asset_primary_file_path_prefers_known_extensions():
    asset = _asset(
        path="D:/root/L36/主板/V1.0",
        files=["readme.txt", "main.rom", "data.pkg"],
    )
    result = asset_primary_file_path(asset)
    assert "main.rom" in result


def test_asset_primary_file_path_falls_back_to_first():
    asset = _asset(
        path="D:/root/L36/主板/V1.0",
        files=["unknown.xyz", "other.dat"],
    )
    result = asset_primary_file_path(asset)
    assert "unknown.xyz" in result


def test_asset_primary_file_path_uses_dir_when_no_files():
    asset = _asset(path="D:/root/L36/主板/V1.0", files=[])
    result = asset_primary_file_path(asset)
    assert Path(result) == Path("D:/root/L36/主板/V1.0")


def test_asset_primary_file_name_returns_filename_only():
    asset = _asset(
        path="D:/root/L36/主板/V1.0", files=["readme.txt", "YJ_3DMain_L36_V40.bin"]
    )
    assert asset_primary_file_name(asset) == "YJ_3DMain_L36_V40.bin"


def test_asset_primary_file_name_prefers_rom_for_handcontrol():
    """手控 rom/pkg 配对：固定显示 rom 名称（用户要求）。"""
    asset = _asset(path="D:/root/L36/手控UI/A", files=["hc_v13.pkg", "hc_v13.rom"])
    assert asset_primary_file_name(asset) == "hc_v13.rom"


def test_asset_primary_file_name_prefers_img_over_boot_text():
    asset = _asset(
        path="D:/root/L36/手控UI/A",
        files=["bootcfg.txt", "d12x_mzkj_v1.0.0.img", "程序信息.toml"],
    )
    assert asset_primary_file_name(asset) == "d12x_mzkj_v1.0.0.img"


def test_handcontrol_copy_filenames_keeps_firmware_pair_only():
    from fwasset.core.asset_helpers import handcontrol_copy_filenames

    assert handcontrol_copy_filenames(
        ["a.pkg", "a.rom", "程序信息.toml", "note.pdf"]
    ) == ["a.rom", "a.pkg"]
    assert handcontrol_copy_filenames(
        ["程序信息.toml", "bootcfg.txt", "d12x.img", "readme.pdf"]
    ) == ["bootcfg.txt", "d12x.img"]
    assert handcontrol_copy_filenames(["only.rom", "程序信息.toml"]) == []


def test_asset_primary_file_name_empty_when_no_files():
    asset = _asset(path="D:/root/L36/主板/V1.0", files=[])
    assert asset_primary_file_name(asset) == ""


def test_open_path_in_explorer_rejects_missing_path():
    logs: list[str] = []
    assert open_path_in_explorer("D:/不存在的目录xyzzy", logs.append) is False
    assert open_path_in_explorer("", logs.append) is False
    assert any("路径不存在" in m for m in logs)


def test_open_path_in_explorer_opens_existing_dir(tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        asset_helpers_module.os,
        "startfile",
        lambda p: opened.append(str(p)),
        raising=False,
    )
    monkeypatch.setattr(
        asset_helpers_module.subprocess,
        "run",
        lambda *a, **k: opened.append(str(a[0])),
        raising=False,
    )
    assert open_path_in_explorer(str(tmp_path), lambda _m: None) is True
    assert opened, "should invoke the platform opener"


def test_open_path_in_explorer_logs_on_opener_failure(tmp_path, monkeypatch):
    def _boom(_p):
        raise OSError("no association")

    monkeypatch.setattr(asset_helpers_module.os, "startfile", _boom, raising=False)
    monkeypatch.setattr(asset_helpers_module.subprocess, "run", _boom, raising=False)
    logs: list[str] = []
    assert open_path_in_explorer(str(tmp_path), logs.append) is False
    assert any("打开目录失败" in m for m in logs)
