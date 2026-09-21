"""D4.3③：legacy 泛化 ``"旧"`` 关键词退役 + 被排除目录报告入口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fwasset.core import file_scan, scheme_config, settings
from fwasset.core.admission import AdmissionError, validate_new_path
from fwasset.core.file_scan import scan_firmware_assets
from fwasset.core.legacy_exclusions import (
    LEGACY_GENERIC_KEYWORD,
    scan_legacy_excluded_dirs,
)
from fwasset.core.managed_paths import detect_workspace_layout
from fwasset.core.model_config import save_model_id
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.reference_lookup import enumerate_model_roots
from fwasset.core.scheme_config import discover_schemes


def _model_marker(root: Path, model_id: str = "l36") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "通用").mkdir(exist_ok=True)
    (root / "定制").mkdir(exist_ok=True)
    save_model_id(root, model_id)
    save_platform_config(root, [PlatformDefaults("单3D", {})])


class TestKeywordRetirement:
    def test_settings_no_longer_contains_generic_old_keyword(self) -> None:
        assert "旧" not in settings.SCAN_EXCLUDE_DIR_KEYWORDS
        assert settings.SCAN_EXCLUDE_DIR_KEYWORDS == [
            "CH341SER",
            "接线图",
            "新建文件夹",
            "照片",
        ]

    def test_scan_finds_assets_under_legacy_named_model_dir(self, tmp_path: Path) -> None:
        legacy = tmp_path / "旧款L36" / "通用" / "语音板" / "程序A"
        legacy.mkdir(parents=True)
        (legacy / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

        assets, errors = scan_firmware_assets(str(tmp_path))

        assert errors == []
        assert [item["path"] for item in assets] == [str(legacy)]

    def test_retired_versions_dir_still_excluded(self, tmp_path: Path) -> None:
        asset = tmp_path / "语音板" / "程序A"
        asset.mkdir(parents=True)
        (asset / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")
        retired = asset / "旧版本" / "副本"
        retired.mkdir(parents=True)
        (retired / "voice_v1.0.0.bin").write_text("old", encoding="utf-8")

        assets, errors = scan_firmware_assets(str(tmp_path))

        assert errors == []
        assert all("旧版本" not in str(item["path"]) for item in assets)

    def test_retired_versions_prefix_dir_not_excluded_without_monkeypatch(
        self, tmp_path: Path
    ) -> None:
        """``旧版本说明/`` 是普通用户目录；退役后无需再 monkeypatch 关键词表。"""
        target = tmp_path / "旧版本说明" / "语音板"
        target.mkdir(parents=True)
        (target / "voice_v2.0.0.bin").write_text("voice", encoding="utf-8")

        assets, errors = scan_firmware_assets(str(tmp_path))

        assert errors == []
        assert [item["path"] for item in assets] == [str(target)]

    def test_admission_allows_legacy_named_model_but_rejects_retired_versions(
        self, tmp_path: Path
    ) -> None:
        validate_new_path(
            tmp_path / "旧款L36",
            kind="model",
            configured_root=str(tmp_path),
            workspace_root=tmp_path,
        )
        with pytest.raises(AdmissionError):
            validate_new_path(
                tmp_path / "旧版本",
                kind="model",
                configured_root=str(tmp_path),
                workspace_root=tmp_path,
            )

    def test_layout_counts_legacy_named_model_dir(self, tmp_path: Path) -> None:
        _model_marker(tmp_path / "旧款L36")

        assert detect_workspace_layout(tmp_path) == "multi_model"

    def test_layout_invalid_when_legacy_named_dir_is_not_a_model(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "旧资料").mkdir()
        _model_marker(tmp_path / "L36程序")

        assert detect_workspace_layout(tmp_path) == "invalid"

    def test_enumerate_model_roots_includes_legacy_named_dir(self, tmp_path: Path) -> None:
        _model_marker(tmp_path / "旧款L36")

        roots = [p.name for p in enumerate_model_roots(tmp_path)]

        assert "旧款L36" in roots


class TestSchemeConfigKeyword:
    def test_is_excluded_dir_drops_old_keeps_others(self) -> None:
        assert scheme_config._is_excluded_dir("旧款L36") is False
        assert scheme_config._is_excluded_dir("backup") is True
        assert scheme_config._is_excluded_dir("temp") is True
        assert scheme_config._is_excluded_dir("tmp") is True
        assert scheme_config._is_excluded_dir("x-back") is True

    def test_discover_schemes_under_legacy_named_model_subdir(self, tmp_path: Path) -> None:
        """型号级子目录跳过（scheme_config.py:84）不再吞掉名含「旧」的型号。"""
        scheme = tmp_path / "旧款L36" / "定制" / "方案甲"
        scheme.mkdir(parents=True)
        (scheme / "方案配置.toml").write_text('platform = "标准单机芯3D"\n', encoding="utf-8")

        names = [s.name for s in discover_schemes(tmp_path)]

        assert names == ["方案甲"]

    def test_discover_schemes_for_legacy_named_scheme_dir(self, tmp_path: Path) -> None:
        scheme = tmp_path / "定制" / "旧方案"
        scheme.mkdir(parents=True)
        (scheme / "方案配置.toml").write_text('platform = "标准单机芯3D"\n', encoding="utf-8")

        names = [s.name for s in discover_schemes(tmp_path)]

        assert names == ["旧方案"]


class TestScanLegacyExcludedDirs:
    def test_reports_dirs_matching_generic_keyword(self, tmp_path: Path) -> None:
        (tmp_path / "旧款L36" / "通用").mkdir(parents=True)
        (tmp_path / "L36程序" / "通用").mkdir(parents=True)

        found = scan_legacy_excluded_dirs(tmp_path)

        assert [item["matched_name"] for item in found] == ["旧款L36"]
        assert found[0]["path"] == str(tmp_path / "旧款L36")
        assert found[0]["is_retired_versions"] is False

    def test_marks_retired_versions_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "语音板" / "程序A" / "旧版本").mkdir(parents=True)

        found = scan_legacy_excluded_dirs(tmp_path)

        assert len(found) == 1
        assert found[0]["matched_name"] == "旧版本"
        assert found[0]["is_retired_versions"] is True

    def test_does_not_descend_into_matched_dir(self, tmp_path: Path) -> None:
        (tmp_path / "旧款L36" / "旧通用").mkdir(parents=True)

        found = scan_legacy_excluded_dirs(tmp_path)

        assert [item["path"] for item in found] == [str(tmp_path / "旧款L36")]

    def test_other_managed_reasons_not_listed(self, tmp_path: Path) -> None:
        (tmp_path / ".fwasset" / "staging").mkdir(parents=True)
        (tmp_path / "L36程序").mkdir()

        assert scan_legacy_excluded_dirs(tmp_path) == []

    def test_does_not_descend_into_managed_roots(self, tmp_path: Path) -> None:
        """受管内部区域里名含「旧」的目录不是被泛化关键词吞掉的业务目录。"""
        (tmp_path / ".fwasset" / "staging" / "旧临时").mkdir(parents=True)
        (tmp_path / ".fwasset" / "quarantine" / "旧隔离").mkdir(parents=True)
        (tmp_path / "L36程序").mkdir()

        assert scan_legacy_excluded_dirs(tmp_path) == []

    @pytest.mark.parametrize("container", [".FWASSET", ".FwAsset"])
    def test_managed_container_match_is_case_insensitive(
        self, tmp_path: Path, container: str
    ) -> None:
        """Windows 文件系统大小写不敏感；容器名比较必须与 managed_paths 同口径。"""
        (tmp_path / container / "staging" / "旧临时").mkdir(parents=True)
        (tmp_path / "L36程序").mkdir()

        assert scan_legacy_excluded_dirs(tmp_path) == []

    def test_retired_versions_flag_matches_managed_reason(self, tmp_path: Path) -> None:
        """``is_retired_versions`` 由 managed_path_reason 派生，不另比较目录名。"""
        (tmp_path / "语音板" / "程序A" / "旧版本").mkdir(parents=True)
        (tmp_path / "语音板" / "程序A" / "旧资料").mkdir(parents=True)

        found = {item["matched_name"]: item["is_retired_versions"] for item in
                 scan_legacy_excluded_dirs(tmp_path)}

        assert found == {"旧版本": True, "旧资料": False}

    def test_custom_keyword_argument(self, tmp_path: Path) -> None:
        (tmp_path / "备份区").mkdir()

        found = scan_legacy_excluded_dirs(tmp_path, keyword="备份")

        assert [item["matched_name"] for item in found] == ["备份区"]

    def test_oserror_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "正常").mkdir()
        real_iterdir = Path.iterdir

        def _boom(self: Path):  # type: ignore[no-untyped-def]
            if self.name == "正常":
                raise OSError("denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _boom)

        assert scan_legacy_excluded_dirs(tmp_path) == []

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        assert scan_legacy_excluded_dirs(tmp_path / "不存在") == []

    def test_keyword_constant(self) -> None:
        assert LEGACY_GENERIC_KEYWORD == "旧"

    def test_file_scan_no_longer_excludes_by_old_keyword(self, tmp_path: Path) -> None:
        assert file_scan._is_excluded_dir(str(tmp_path / "旧款L36")) is False
