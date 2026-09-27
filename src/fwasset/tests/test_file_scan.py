import sys
from pathlib import Path

import pytest

import fwasset.core.file_scan as file_scan
from fwasset.core.file_scan import (
    find_handcontrol_folders,
    guess_model_from_path,
    guess_series_from_model_or_path,
    guess_version_from_path,
    parse_rom_filename,
    scan_firmware_assets,
)


@pytest.mark.parametrize(
    ("filename", "expected_model", "expected_version"),
    [
        ("YJ-L50S-3122MG-791_UI_118.3.10.ROM", "L50S", "V118.3.10"),
        ("ITE_NOR_yj_massage_4d_music_L36_v34.3.2.ROM", "L36", "V34.3.2"),
        ("ITE_NOR.ROM", "", ""),
        ("abc_L88_pro_v1.2.3.rom", "L88", "V1.2.3"),
        ("ITE_NOR_yj_massage_4d_music_L50_heat_38.3.1_003.ROM", "L50", "V38.3.1_003"),
        ("ITE_NOR_yj_Smassage_4d_music_L50_heat_38.3.1_019.ROM", "L50", "V38.3.1_019"),
        ("L35A手控UI.ROM", "L35A", ""),
        ("ITE_NOR_yj_massage_L26A_Max_music_72.33.ROM", "L26A", "V72.33"),
        ("YJ-L39S-2122XN-K032_UI_126.3.1.ROM", "L39S", "V126.3.1"),
        ("ITE_NOR 120.3.1.ROM", "", "V120.3.1"),
        ("ITE_NOR_yj_massage__NULL_s_V50.3.6.ROM", "", "V50.3.6"),
        ("NULLLOG_16.3.7_FY.ROM", "", "V16.3.7"),
        ("ITE_NOR_yj_Smassage_L36_4d_Beelogo_103.3.1.ROM", "L36", "V103.3.1"),
        ("ITE_NOR_yj_Smassage_L36_H530_62.3.2.ROM", "L36", "V62.3.2"),
        ("ITE_NOR_yj_massage_4d_l65_46_002.ROM", "L65", "V46_002"),
        ("zey_standard_project_l50s_47_005.ROM", "L50S", "V47_005"),
    ],
)
def test_parse_rom_filename(filename: str, expected_model: str, expected_version: str):
    model, version = parse_rom_filename(filename)
    assert model == expected_model
    assert version == expected_version


@pytest.mark.parametrize(
    ("dirpath", "expected"),
    [
        (r"D:\\workspace\\release\\L36\\pkg", "L36"),
        (r"D:\\workspace\\L50Smax\\test", "L50SMAX"),
        (r"D:\\workspace\\L35A手控UI\\pkg", "L35A"),
        (r"D:\\workspace\\foo\\bar", "未知型号"),
    ],
)
def test_guess_model_from_path(dirpath: str, expected: str):
    assert guess_model_from_path(dirpath) == expected


@pytest.mark.parametrize(
    ("dirpath", "expected"),
    [
        pytest.param(
            r"D:\\workspace\\NOLLLOG_V17.3.2\\pkg",
            "V17.3.2",
            marks=pytest.mark.skipif(
                sys.platform != "win32",
                reason="依赖 Windows 路径分段（段尾反斜杠边界），POSIX 下单段无法匹配",
            ),
        ),
        (r"D:\\workspace\\release\\38.3.1_003\\pkg", "V38.3.1_003"),
        (r"D:\\workspace\\foo\\bar", ""),
    ],
)
def test_guess_version_from_path(dirpath: str, expected: str):
    assert guess_version_from_path(dirpath) == expected


@pytest.mark.parametrize(
    ("model", "dirpath", "expected"),
    [
        ("L36A", r"D:\\workspace\\L36A手控\\release", "L36"),
        ("L50SMAX", r"D:\\workspace\\L50Smax\\release", "L50"),
        ("", r"D:\\workspace\\L39-零重力\\主板程序", "L39"),
        ("未知型号", r"D:\\workspace\\foo\\bar", "未知系列"),
    ],
)
def test_guess_series_from_model_or_path(model: str, dirpath: str, expected: str):
    assert guess_series_from_model_or_path(model, dirpath) == expected


def test_find_handcontrol_folders_filters_and_sorts(tmp_path: Path):
    a = tmp_path / "A_folder"
    a.mkdir()
    (a / "YJ-L50S-3122MG-791_UI_118.3.10.ROM").write_text("rom", encoding="utf-8")
    (a / "firmware.pkg").write_text("pkg", encoding="utf-8")

    b = tmp_path / "B_folder"
    b.mkdir()
    (b / "ITE_NOR.ROM").write_text("rom", encoding="utf-8")
    (b / "ITE_NOR.PKG").write_text("pkg", encoding="utf-8")

    ignored1 = tmp_path / "CH341SER" / "skip1"
    ignored1.mkdir(parents=True)
    (ignored1 / "L99_v1.0.0.ROM").write_text("rom", encoding="utf-8")
    (ignored1 / "x.pkg").write_text("pkg", encoding="utf-8")

    ignored2 = tmp_path / "接线图" / "skip2"
    ignored2.mkdir(parents=True)
    (ignored2 / "L100_v2.0.0.ROM").write_text("rom", encoding="utf-8")
    (ignored2 / "x.pkg").write_text("pkg", encoding="utf-8")

    not_match = tmp_path / "C_only_rom"
    not_match.mkdir()
    (not_match / "L77_v1.0.0.ROM").write_text("rom", encoding="utf-8")

    results = find_handcontrol_folders(str(tmp_path))

    assert len(results) == 2
    assert [Path(item["path"]).name for item in results] == ["A_folder", "B_folder"]

    first = results[0]
    assert first["model"] == "L50S"
    assert first["version"] == "V118.3.10"
    assert first["rom_file"].endswith(".ROM")
    assert first["pkg_file"].lower().endswith(".pkg")
    assert "A_folder" in first["label"]

    second = results[1]
    assert second["model"] == "未知型号"
    assert second["version"] == ""
    assert "B_folder" in second["label"]


def test_find_handcontrol_folders_falls_back_to_path_for_model_and_version(
    tmp_path: Path,
):
    target = tmp_path / "L35A手控UI" / "NOLLLOG_V17.3.2"
    target.mkdir(parents=True)
    (target / "ITE_NOR.ROM").write_text("rom", encoding="utf-8")
    (target / "ITE_NOR.PKG").write_text("pkg", encoding="utf-8")

    results = find_handcontrol_folders(str(tmp_path))

    assert len(results) == 1
    assert results[0]["model"] == "L35A"
    assert results[0]["version"] == "V17.3.2"


def test_parse_rom_filename_uses_configurable_patterns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(file_scan, "SCAN_MODEL_PATTERNS", [r"MODEL-(X\d+)"])
    monkeypatch.setattr(file_scan, "SCAN_VERSION_PATTERNS", [r"REV-(\d+\.\d+)"])

    model, version = file_scan.parse_rom_filename("firmware_MODEL-X55_REV-2.5.ROM")

    assert model == "X55"
    assert version == "V2.5"


def test_guess_model_from_path_uses_configurable_patterns(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(file_scan, "SCAN_PATH_MODEL_PATTERNS", [r"(HC\d{2})"])

    assert (
        file_scan.guess_model_from_path(r"D:\\workspace\\release\\HC88\\pkg") == "HC88"
    )


def test_guess_version_from_path_uses_configurable_patterns(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        file_scan, "SCAN_PATH_VERSION_PATTERNS", [r"REL-(\d+\.\d+_\d+)"]
    )

    assert (
        file_scan.guess_version_from_path(r"D:\\workspace\\REL-9.8_007\\pkg")
        == "V9.8_007"
    )


def test_find_handcontrol_folders_uses_configurable_extensions_and_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(file_scan, "SCAN_ROM_EXTENSIONS", [".bin"])
    monkeypatch.setattr(file_scan, "SCAN_PKG_EXTENSIONS", [".pack"])
    monkeypatch.setattr(file_scan, "SCAN_EXCLUDE_DIR_KEYWORDS", ["ignore_me"])
    monkeypatch.setattr(file_scan, "SCAN_MODEL_PATTERNS", [r"FW-(Z\d+)"])
    monkeypatch.setattr(file_scan, "SCAN_VERSION_PATTERNS", [r"VER-(\d+\.\d+)"])

    good = tmp_path / "ok"
    good.mkdir()
    (good / "FW-Z88_VER-3.7.bin").write_text("rom", encoding="utf-8")
    (good / "bundle.pack").write_text("pkg", encoding="utf-8")

    ignored = tmp_path / "ignore_me" / "child"
    ignored.mkdir(parents=True)
    (ignored / "FW-Z99_VER-9.9.bin").write_text("rom", encoding="utf-8")
    (ignored / "bundle.pack").write_text("pkg", encoding="utf-8")

    results = file_scan.find_handcontrol_folders(str(tmp_path))

    assert len(results) == 1
    assert results[0]["model"] == "Z88"
    assert results[0]["version"] == "V3.7"
    assert results[0]["rom_file"].endswith(".bin")
    assert results[0]["pkg_file"].endswith(".pack")


def test_scan_firmware_assets_uses_catalog_types(tmp_path: Path):
    handcontrol_dir = tmp_path / "L35A手控UI" / "release"
    handcontrol_dir.mkdir(parents=True)
    (handcontrol_dir / "YJ-L35A_UI_1.2.3.ROM").write_text("rom", encoding="utf-8")
    (handcontrol_dir / "firmware.pkg").write_text("pkg", encoding="utf-8")

    voice_dir = tmp_path / "语音板"
    voice_dir.mkdir()
    (voice_dir / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 2
    assert [item["firmware_type"] for item in assets] == ["handcontrol_ui", "voice"]
    assert assets[0]["series"] == "L35"
    assert assets[0]["model_directory_name"] == "L35A手控UI"
    assert assets[0]["flash_mode"] == "auto_usb"
    assert assets[0]["usb_flow"] == "paired_files"
    assert assets[1]["firmware_label"] == "语音程序"
    assert "tool_dir" in assets[1]


def test_scan_firmware_assets_uses_expanded_catalog_keywords_and_excludes(
    tmp_path: Path,
):
    mainboard_dir = tmp_path / "L36配置" / "L36主板"
    mainboard_dir.mkdir(parents=True)
    (mainboard_dir / "main_v1.0.0.bin").write_text("main", encoding="utf-8")

    bluetooth_dir = tmp_path / "L36配置" / "蓝牙—语音"
    bluetooth_dir.mkdir(parents=True)
    (bluetooth_dir / "bt_v2.0.0.hex").write_text("bt", encoding="utf-8")

    ignored_photo_dir = tmp_path / "L36配置" / "照片" / "语音板"
    ignored_photo_dir.mkdir(parents=True)
    (ignored_photo_dir / "voice_v3.0.0.bin").write_text("voice", encoding="utf-8")

    ignored_old_dir = tmp_path / "L36配置" / "新建文件夹" / "快捷键程序"
    ignored_old_dir.mkdir(parents=True)
    (ignored_old_dir / "shortcut_v4.0.0.bin").write_text("shortcut", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert [item["firmware_type"] for item in assets] == ["mainboard", "music_bt"]
    assert assets[1]["flash_mode"] == "tool_launch"
    assert assets[1]["usb_flow"] == ""
    assert [item["model_directory_name"] for item in assets] == ["L36配置", "L36配置"]
    assert all(Path(item["model_directory_path"]).name == "L36配置" for item in assets)


def test_scan_uses_nearest_model_directory_inside_mixed_parent(tmp_path: Path):
    bundle = tmp_path / "L26AmaxL50S葡萄牙"
    l26_dir = bundle / "L26A手控葡文"
    l50_dir = bundle / "L50S手控葡文"
    l26_dir.mkdir(parents=True)
    l50_dir.mkdir(parents=True)
    (l26_dir / "ITE_NOR.ROM").write_text("rom", encoding="utf-8")
    (l26_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")
    (l50_dir / "ITE_NOR.ROM").write_text("rom", encoding="utf-8")
    (l50_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 2
    by_model = {item["model"]: item for item in assets}
    assert by_model["L26A"]["model_directory_name"] == l26_dir.name
    assert by_model["L26A"]["directory_name"] == l26_dir.name
    assert by_model["L50S"]["model_directory_name"] == l50_dir.name
    assert by_model["L50S"]["directory_name"] == l50_dir.name
    assert bundle.name not in {item["model_directory_name"] for item in assets}


def test_handcontrol_scan_prefers_model_from_handcontrol_directory_when_rom_differs(
    tmp_path: Path,
):
    bundle = tmp_path / "L26Amax-L50S-portugal"
    l26_dir = bundle / "L26A-handcontrol-portuguese"
    l50_dir = bundle / "L50S-handcontrol-portuguese"
    l26_dir.mkdir(parents=True)
    l50_dir.mkdir(parents=True)
    (l26_dir / "ITE_NOR_yj_Smassage_2d_L39_v111.3.5.ROM").write_text(
        "rom", encoding="utf-8"
    )
    (l26_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")
    (l50_dir / "ITE_NOR_yj_Smassage_L50_4d_music_108.3.3.ROM").write_text(
        "rom", encoding="utf-8"
    )
    (l50_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    by_dir = {Path(item["path"]).name: item for item in assets}
    assert by_dir[l26_dir.name]["model"] == "L26A"
    assert by_dir[l26_dir.name]["version"] == "V111.3.5"
    assert by_dir[l50_dir.name]["model"] == "L50S"
    assert by_dir[l50_dir.name]["version"] == "V108.3.3"


def test_segmented_screen_before_handcontrol_ui_in_catalog(tmp_path: Path):
    """P0-3: '断码屏' directory with ROM+PKG should match segmented_screen, not handcontrol_ui."""
    seg_dir = tmp_path / "L39MAX" / "断码屏亚克力手控"
    seg_dir.mkdir(parents=True)
    (seg_dir / "YJ-L39max_UI_125.3.2.ROM").write_text("rom", encoding="utf-8")
    (seg_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "segmented_screen", (
        f"Expected segmented_screen, got {assets[0]['firmware_type']}"
    )


def test_handcontrol_img_requires_explicit_handcontrol_module(tmp_path: Path) -> None:
    for module in ("主板程序", "断码屏亚克力手控"):
        folder = tmp_path / "L36" / "通用" / module / "v1"
        folder.mkdir(parents=True)
        (folder / "firmware.img").write_bytes(b"firmware")
    misleading_root = tmp_path / "手控UI" / "L50" / "通用" / "主板程序" / "v1"
    misleading_root.mkdir(parents=True)
    (misleading_root / "firmware.img").write_bytes(b"firmware")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert not issues
    assert not any(asset["firmware_type"] == "handcontrol_ui" for asset in assets)


def test_handcontrol_img_version_comes_from_directory_not_image_name(
    tmp_path: Path,
) -> None:
    """厂商文件夹名才是程序版本；镜像文件名里的芯片版本不能覆盖它。"""
    folder = (
        tmp_path
        / "L36"
        / "通用"
        / "手控UI"
        / "YJ_d12x_massage_lcd_L50S_V21.07"
    )
    folder.mkdir(parents=True)
    (folder / "bootcfg.txt").write_text("boot", encoding="utf-8")
    (folder / "d12x_mzkj_v1.0.0.img").write_bytes(b"firmware")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert not issues
    assert len(assets) == 1
    assert assets[0]["version"] == "V21.07"
    assert assets[0]["model"] == "L50S"
    assert Path(assets[0]["model_directory_path"]) == tmp_path / "L36"


def test_handcontrol_rom_version_still_comes_from_rom_filename(tmp_path: Path) -> None:
    folder = tmp_path / "L36" / "通用" / "手控UI" / "包装名_V1.0"
    folder.mkdir(parents=True)
    (folder / "ITE_NOR_yj_massage_4d_music_L36_v34.3.2.ROM").write_text(
        "rom", encoding="utf-8"
    )
    (folder / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert not issues
    assert len(assets) == 1
    assert assets[0]["version"] == "V34.3.2"
    assert assets[0]["model"] == "L36"


def test_handcontrol_img_under_dual_core_platform_is_scanned(tmp_path: Path) -> None:
    folder = (
        tmp_path
        / "L36"
        / "通用"
        / "双机芯-上3D-下2D"
        / "手控UI"
        / "镜像版本"
    )
    folder.mkdir(parents=True)
    (folder / "firmware.img").write_bytes(b"firmware")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert not issues
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "handcontrol_ui"
    assert assets[0]["platform"] == "双机芯-上3D-下2D"
    assert assets[0]["flash_mode"] == "auto_usb"
    assert assets[0]["usb_flow"] == "paired_files"


def test_music_bt_detects_mot_files(tmp_path: Path):
    """P0-2: Bluetooth directories with .mot files should be detected as music_bt."""
    bt_dir = tmp_path / "L36" / "蓝牙板"
    bt_dir.mkdir(parents=True)
    (bt_dir / "YJ_Bt_Eng_Massage_R5F104BC_Pro_V15.mot").write_text(
        "bt", encoding="utf-8"
    )

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "music_bt", (
        f"Expected music_bt, got {assets[0]['firmware_type']}"
    )
    assert assets[0]["flash_mode"] == "tool_launch"
    assert assets[0]["usb_flow"] == ""


def test_music_files_detects_mp3_as_directory_copy_asset(tmp_path: Path):
    music_dir = tmp_path / "L36" / "音乐文件"
    music_dir.mkdir(parents=True)
    (music_dir / "welcome.mp3").write_text("music", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "music_files"
    assert assets[0]["flash_mode"] == "auto_usb"
    assert assets[0]["usb_flow"] == "directory_copy"


def test_movement_3d_detects_mot_files(tmp_path: Path):
    """P0-2: 3D机芯 directories with .mot files should be detected as movement_3d."""
    motor_dir = tmp_path / "L36" / "3d机芯板"
    motor_dir.mkdir(parents=True)
    (motor_dir / "motor_v1.mot").write_text("motor", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "movement_3d", (
        f"Expected movement_3d, got {assets[0]['firmware_type']}"
    )


def test_version_extracted_from_space_separated_rom(tmp_path: Path):
    """P0-1: 'ITE_NOR 120.3.1.ROM' should extract version V120.3.1."""
    hand_dir = tmp_path / "L36" / "手控"
    hand_dir.mkdir(parents=True)
    (hand_dir / "ITE_NOR 120.3.1.ROM").write_text("rom", encoding="utf-8")
    (hand_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["model"] == "L36"
    assert assets[0]["version"] == "V120.3.1", (
        f"Expected V120.3.1, got '{assets[0]['version']}'"
    )


def test_scan_with_cancel_event(tmp_path: Path):
    """Scanning with cancel_event already set should return empty results with a cancellation error."""
    hand_dir = tmp_path / "L36" / "手控"
    hand_dir.mkdir(parents=True)
    (hand_dir / "ITE_NOR.ROM").write_text("rom", encoding="utf-8")
    (hand_dir / "ITEPKG03.PKG").write_text("pkg", encoding="utf-8")

    import threading

    cancel_event = threading.Event()
    cancel_event.set()

    assets, issues = scan_firmware_assets(str(tmp_path), cancel_event=cancel_event)

    assert len(assets) == 0
    assert any("取消" in issue["message"] for issue in issues)
    assert all(issue["severity"] == "error" for issue in issues)


def test_scan_with_last_scan_at_skips_unchanged(tmp_path: Path):
    """Directories unchanged since last_scan_at should be skipped."""
    model_dir = tmp_path / "L36" / "主板程序"
    model_dir.mkdir(parents=True)
    (model_dir / "main.bin").write_text("firmware", encoding="utf-8")

    import time

    future_time = time.time() + 10000

    assets, errors = scan_firmware_assets(str(tmp_path), last_scan_at=future_time)
    assert len(assets) == 0

    past_time = time.time() - 10000
    assets_past, errors_past = scan_firmware_assets(
        str(tmp_path), last_scan_at=past_time
    )
    assert len(assets_past) == 1


def test_scan_incremental_still_finds_child_when_parent_mtime_is_old(tmp_path: Path):
    """Issue 12：父目录 mtime 旧时不得剪枝子树，否则漏扫已更新的子目录资产。"""
    import os
    import time

    parent = tmp_path / "L36" / "外壳"
    child = parent / "主板程序"
    child.mkdir(parents=True)
    (child / "main.bin").write_text("firmware", encoding="utf-8")

    # 把父目录 mtime 拨到很早；子目录保持较新
    old = time.time() - 100_000
    os.utime(parent, (old, old))
    # 刷新子目录 mtime 为现在
    now = time.time()
    os.utime(child, (now, now))

    # last_scan_at 介于 parent 旧与 child 新之间
    mid = old + 50_000
    assets, _errors = scan_firmware_assets(str(tmp_path), last_scan_at=mid)
    assert len(assets) == 1
    assert assets[0]["firmware_type"] == "mainboard"


def test_scan_without_last_scan_at_finds_all(tmp_path: Path):
    """Without last_scan_at, all directories are scanned."""
    model_dir = tmp_path / "L36" / "主板程序"
    model_dir.mkdir(parents=True)
    (model_dir / "main.bin").write_text("firmware", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))
    assert len(assets) == 1


# ---------------------------------------------------------------------------
# D4.3：受管路径统一判定接入扫描（TASK-20260905）
# ---------------------------------------------------------------------------


def test_scan_excludes_retired_versions_directory(tmp_path: Path):
    """旧版本/ 内的程序不进索引：它是备用副本，不是可选中的资产。"""
    current = tmp_path / "语音板"
    current.mkdir()
    (current / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

    retired = tmp_path / "语音板" / "旧版本" / "语音-V1.0"
    retired.mkdir(parents=True)
    (retired / "voice_v1.0.0.bin").write_text("old", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    paths = [item["path"] for item in assets]
    assert str(current) in paths
    assert all("旧版本" not in path for path in paths)


def test_scan_does_not_exclude_retired_versions_prefix_directory(tmp_path: Path):
    """旧版本说明/ 是普通用户目录，精确段比较不得误排。

    泛化关键词 "旧" 已按 D4.3③ 退役，不再需要 monkeypatch 把它移走。
    """
    target = tmp_path / "旧版本说明" / "语音板"
    target.mkdir(parents=True)
    (target / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert [item["path"] for item in assets] == [str(target)]


def test_scan_filters_asset_metadata_from_files(tmp_path: Path):
    """程序信息.toml 是内部元数据，不得作为程序文件展示在详情里。"""
    asset_dir = tmp_path / "语音板"
    asset_dir.mkdir()
    (asset_dir / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")
    (asset_dir / "程序信息.toml").write_text("vendor = 'x'", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert len(assets) == 1
    assert assets[0]["files"] == ["voice_v2.0.0.bin"]


def test_generic_old_keyword_retired_so_real_model_dir_is_scanned(tmp_path: Path):
    """D4.3③ 退役后，旧款L36 这类真实型号目录不再被泛化 "旧" 静默排除。

    退役前此处固化的是相反行为；完整退役断言见 test_legacy_exclusions.py。
    """
    legacy = tmp_path / "旧款L36" / "语音板"
    legacy.mkdir(parents=True)
    (legacy / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert errors == []
    assert [item["path"] for item in assets] == [str(legacy)]


def test_scan_excludes_internal_managed_roots(tmp_path: Path):
    """staging / 候选区 / 状态目录里的固件不得被扫描成正式资产。

    Codex 审查 P1：helper 未传 workspace_root 时内部区域规则完全失效。
    """
    from fwasset.core.managed_paths import managed_root

    normal = tmp_path / "语音板"
    normal.mkdir()
    (normal / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

    for kind in ("staging", "incomplete_candidate", "workspace_state"):
        hidden = managed_root(tmp_path, kind) / "语音板"
        hidden.mkdir(parents=True)
        (hidden / "voice_v9.9.9.bin").write_text("hidden", encoding="utf-8")

    assets, errors = scan_firmware_assets(str(tmp_path))

    assert [item for item in errors if item.get("severity") == "error"] == []
    warnings = [item for item in errors if item.get("severity") == "warning"]
    assert len(warnings) == 1
    assert "历史待补齐" in str(warnings[0]["message"])
    assert [item["path"] for item in assets] == [str(normal)]
