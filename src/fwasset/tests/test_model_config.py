"""Tests for model_config — slug / 型号配置.toml load-save 与 SharedModuleRef schema。"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from fwasset.core import config_io
from fwasset.core import model_config as model_config_mod
from fwasset.core.model_config import (
    SharedModuleRef,
    load_model_config,
    load_shared_modules,
    save_model_id,
    save_shared_module,
    slugify_model_id,
)


class TestSlugifyModelId:
    def test_common_program_suffix(self):
        assert slugify_model_id("L50程序") == "l50"
        assert slugify_model_id("L36程序") == "l36"

    def test_dual_core_chinese(self):
        assert slugify_model_id("L36双机芯-上3D-下2D程序") == "l36双机芯-上3d-下2d"

    def test_directory_suffix_and_no_suffix(self):
        assert slugify_model_id("L36目录") == "l36"
        assert slugify_model_id("L50") == "l50"

    def test_dangerous_chars_and_spaces(self):
        assert slugify_model_id("L36  程序") == "l36"
        assert ":" not in slugify_model_id("L36:坏名程序")
        assert (
            slugify_model_id("  --L36--程序  ").startswith("l36")
            or slugify_model_id("  --L36--程序  ") == "l36"
        )

    def test_empty_fallback(self):
        assert slugify_model_id("") == "model"
        assert slugify_model_id("///") == "model"
        assert slugify_model_id("程序") == "model"


class TestLoadSaveModelConfig:
    def test_missing(self, tmp_path: Path):
        mid, status, err = load_model_config(tmp_path)
        assert mid == ""
        assert status == "missing"
        assert err == ""

    def test_ok(self, tmp_path: Path):
        (tmp_path / "型号配置.toml").write_text('model_id = "l36"\n', encoding="utf-8")
        mid, status, err = load_model_config(tmp_path)
        assert mid == "l36"
        assert status == "ok"
        assert err == ""

    def test_no_id(self, tmp_path: Path):
        (tmp_path / "型号配置.toml").write_text("# only comment\n", encoding="utf-8")
        mid, status, err = load_model_config(tmp_path)
        assert mid == ""
        assert status == "no_id"

        (tmp_path / "型号配置.toml").write_text('model_id = ""\n', encoding="utf-8")
        mid, status, _ = load_model_config(tmp_path)
        assert status == "no_id"

    def test_parse_error(self, tmp_path: Path):
        (tmp_path / "型号配置.toml").write_text("[[broken\n", encoding="utf-8")
        mid, status, err = load_model_config(tmp_path)
        assert mid == ""
        assert status == "parse_error"
        assert err

    def test_parser_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / "型号配置.toml").write_text('model_id = "x"\n', encoding="utf-8")
        monkeypatch.setattr(model_config_mod, "tomllib", None)
        mid, status, err = load_model_config(tmp_path)
        assert status == "parser_missing"
        assert mid == ""
        assert err

    def test_save_and_reload(self, tmp_path: Path):
        save_model_id(tmp_path, "l50")
        mid, status, _ = load_model_config(tmp_path)
        assert status == "ok"
        assert mid == "l50"

    def test_save_preserves_shared_modules_section(self, tmp_path: Path):
        path = tmp_path / "型号配置.toml"
        path.write_text(
            "\n".join(
                [
                    'model_id = "old"',
                    "",
                    '[shared_modules."快捷键程序"]',
                    'source_model_id = "l36"',
                    'source_group = "l36-single"',
                    'source_module = "快捷键程序"',
                    'source_relative_path = "L36程序/通用/快捷键/贝乐"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        save_model_id(tmp_path, "new-id")
        text = path.read_text(encoding="utf-8")
        assert 'model_id = "new-id"' in text
        assert '[shared_modules."快捷键程序"]' in text
        assert 'source_model_id = "l36"' in text
        assert 'source_relative_path = "L36程序/通用/快捷键/贝乐"' in text
        mid, status, _ = load_model_config(tmp_path)
        assert status == "ok"
        assert mid == "new-id"

    def test_save_replace_failure_keeps_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "型号配置.toml"
        original = 'model_id = "keep"\n'
        path.write_text(original, encoding="utf-8")
        original_bytes = path.read_bytes()

        def boom(_src: str, _dst: str) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(config_io.os, "replace", boom)
        with pytest.raises(OSError):
            save_model_id(tmp_path, "other")
        assert path.read_bytes() == original_bytes
        assert list(tmp_path.glob(".型号配置.toml.*.tmp")) == []

    def test_chinese_model_root(self, tmp_path: Path):
        root = tmp_path / "L36双机芯-上3D-下2D程序"
        root.mkdir()
        save_model_id(root, "l36双机芯-上3d-下2d")
        mid, status, _ = load_model_config(root)
        assert status == "ok"
        assert mid == "l36双机芯-上3d-下2d"


# ---------------------------------------------------------------------------
# SharedModuleRef：mode / source_platform 可选字段兼容读写
# ---------------------------------------------------------------------------
# 辅助：直接落盘一份旧格式 toml（无 mode / source_platform 键）
# ---------------------------------------------------------------------------


def _write_legacy_toml(model_root: Path, key: str, entry: dict) -> None:
    model_root.mkdir(parents=True, exist_ok=True)
    data = {"model_id": "test-model", "shared_modules": {key: entry}}
    (model_root / "型号配置.toml").write_bytes(tomli_w.dumps(data).encode())


_LEGACY_ENTRY = {
    "source_model_id": "l50s",
    "source_group": "l50s",
    "source_module": "手控UI",
    "source_relative_path": "L50S程序/通用/手控UI/v2.0",
}


# ---------------------------------------------------------------------------
# 1. 旧格式 toml 无 mode 键 → 读出 mode="static", source_platform=""
# ---------------------------------------------------------------------------


def test_legacy_toml_without_mode_reads_as_static(tmp_path: Path):
    root = tmp_path / "model"
    _write_legacy_toml(root, "手控UI", _LEGACY_ENTRY)

    refs = load_shared_modules(root)
    assert len(refs) == 1
    assert refs[0].mode == "static"
    assert refs[0].source_platform == ""


# ---------------------------------------------------------------------------
# 2. follow_default + source_platform round-trip
# ---------------------------------------------------------------------------


def test_follow_default_round_trip(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    save_model_id(root, "l36-dual")
    ref = SharedModuleRef(
        module_key="手控UI",
        source_model_id="l50s",
        source_group="l50s",
        source_module="手控UI",
        source_relative_path="L50S程序/通用/手控UI",
        mode="follow_default",
        source_platform="标准单机芯",
    )
    save_shared_module(root, ref)
    refs = load_shared_modules(root)
    assert len(refs) == 1
    r = refs[0]
    assert r.mode == "follow_default"
    assert r.source_platform == "标准单机芯"
    assert r.source_relative_path == "L50S程序/通用/手控UI"


# ---------------------------------------------------------------------------
# 3. toml 中遗留 mode="pinned" → 容错回退为 "static"
# ---------------------------------------------------------------------------


def test_legacy_pinned_falls_back_to_static(tmp_path: Path):
    root = tmp_path / "model"
    entry = {**_LEGACY_ENTRY, "mode": "pinned"}
    _write_legacy_toml(root, "主板程序", entry)

    refs = load_shared_modules(root)
    assert len(refs) == 1
    assert refs[0].mode == "static"


# ---------------------------------------------------------------------------
# 4. mode="static" 写入时 toml 不含 mode 键（旧格式兼容）
# ---------------------------------------------------------------------------


def test_static_mode_not_written_to_toml(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    save_model_id(root, "l36")
    ref = SharedModuleRef(
        module_key="快捷键程序",
        source_model_id="l50s",
        source_group="l50s",
        source_module="快捷键程序",
        source_relative_path="L50S程序/通用/快捷键/贝乐",
        mode="static",
    )
    save_shared_module(root, ref)
    raw = (root / "型号配置.toml").read_text(encoding="utf-8")
    # 检查独立键 "mode ="，避免 model_id 中的子串误判
    assert "mode =" not in raw


# ---------------------------------------------------------------------------
# 5. source_platform 为空时不写入 toml
# ---------------------------------------------------------------------------


def test_empty_source_platform_not_written(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    save_model_id(root, "l36")
    ref = SharedModuleRef(
        module_key="语音程序",
        source_model_id="l50s",
        source_group="l50s",
        source_module="语音程序",
        source_relative_path="L50S程序/通用/语音/v1.0",
        mode="follow_default",
        source_platform="",
    )
    save_shared_module(root, ref)
    raw = (root / "型号配置.toml").read_text(encoding="utf-8")
    assert "source_platform" not in raw


# ---------------------------------------------------------------------------
# 6. toml 中 mode 为无效值 → 容错回退为 "static"
# ---------------------------------------------------------------------------


def test_invalid_mode_falls_back_to_static(tmp_path: Path):
    root = tmp_path / "model"
    entry = {**_LEGACY_ENTRY, "mode": "unknown_future_mode"}
    _write_legacy_toml(root, "手控UI", entry)

    refs = load_shared_modules(root)
    assert len(refs) == 1
    assert refs[0].mode == "static"


# ---------------------------------------------------------------------------
# 7. follow_default 不带 source_platform（空串）round-trip
# ---------------------------------------------------------------------------


def test_follow_default_without_source_platform(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    save_model_id(root, "l36-dual")
    ref = SharedModuleRef(
        module_key="手控UI",
        source_model_id="l50s",
        source_group="l50s",
        source_module="手控UI",
        source_relative_path="L50S程序/通用/手控UI",
        mode="follow_default",
        source_platform="",
    )
    save_shared_module(root, ref)
    refs = load_shared_modules(root)
    assert len(refs) == 1
    assert refs[0].mode == "follow_default"
    assert refs[0].source_platform == ""


# ---------------------------------------------------------------------------
# 8. 同一 toml 混合旧格式与新格式引用
# ---------------------------------------------------------------------------


def test_mixed_legacy_and_new_refs(tmp_path: Path):
    root = tmp_path / "model"
    root.mkdir()
    save_model_id(root, "l36-dual")

    # 旧格式手写入
    _write_legacy_toml(root, "手控UI", _LEGACY_ENTRY)

    # 追加一条新格式引用（save_shared_module 走 merge-write，不会删掉旧条目）
    ref_c = SharedModuleRef(
        module_key="主板程序",
        source_model_id="l50s",
        source_group="l50s",
        source_module="主板程序",
        source_relative_path="L50S程序/通用/主板程序",
        mode="follow_default",
        source_platform="标准单机芯",
    )
    save_shared_module(root, ref_c)

    refs = load_shared_modules(root)
    assert len(refs) == 2
    by_key = {r.module_key: r for r in refs}

    assert by_key["手控UI"].mode == "static"
    assert by_key["手控UI"].source_platform == ""
    assert by_key["主板程序"].mode == "follow_default"
    assert by_key["主板程序"].source_platform == "标准单机芯"
