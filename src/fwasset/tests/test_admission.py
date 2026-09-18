"""新路径准入测试：D3 检查序列、D0.2 层级、排除关键词与锚点冲突。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fwasset.core.admission import (
    AdmissionError,
    _invalid_name_reason,
    validate_new_path,
)
from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    save_model_id,
    save_shared_module,
)
from fwasset.core.platform_config import PlatformDefaults, save_platform_config


def _write(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _validate(target: Path, kind: str, ws: Path) -> Path:
    return validate_new_path(
        target,
        kind=kind,  # type: ignore[arg-type]
        configured_root=str(ws),
        workspace_root=str(ws),
    )


def _reject(target: Path, kind: str, ws: Path, code: str) -> AdmissionError:
    with pytest.raises(AdmissionError) as exc_info:
        _validate(target, kind, ws)
    assert exc_info.value.code == code
    return exc_info.value


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
    save_platform_config(l36, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    _write(l36 / "通用" / "主板程序" / "v1" / "rom.bin")
    _write(l50 / "通用" / "主板程序" / "v1" / "rom.bin")
    return root


def _borrow(target_root: Path, key: str, source_rel: str, sid: str = "l36") -> None:
    save_shared_module(
        target_root,
        SharedModuleRef(
            module_key=key,
            source_model_id=sid,
            source_group=sid,
            source_module=key,
            source_relative_path=source_rel,
            mode="static",
            source_platform="",
        ),
    )


# ---------------------------------------------------------------------------
# 检查 1：工作区归属
# ---------------------------------------------------------------------------


def test_out_of_workspace_rejected(ws: Path, tmp_path: Path):
    _reject(tmp_path / "elsewhere" / "目标", "asset", ws, "out_of_workspace")


def test_relative_path_rejected(ws: Path):
    with pytest.raises(AdmissionError) as exc_info:
        validate_new_path(
            "L36程序/通用/主板程序/v2",
            kind="asset",
            configured_root=str(ws),
            workspace_root=str(ws),
        )
    assert exc_info.value.code == "out_of_workspace"


# ---------------------------------------------------------------------------
# 检查 2：目标不存在
# ---------------------------------------------------------------------------


def test_target_exists_rejected(ws: Path):
    _reject(ws / "L36程序" / "通用" / "主板程序" / "v1", "asset", ws, "path_exists")


def test_target_exists_case_insensitive(ws: Path):
    _reject(ws / "L36程序" / "通用" / "主板程序" / "V1", "asset", ws, "path_exists")


def test_target_junction_rejected(ws: Path):
    link = ws / "L36程序" / "通用" / "主板程序" / "链接"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(link.parent / "v1")],
        check=True,
        capture_output=True,
    )
    try:
        _reject(link, "asset", ws, "path_exists")
    finally:
        link.rmdir()


# ---------------------------------------------------------------------------
# 检查 3：领域归属（D0.2）
# ---------------------------------------------------------------------------


def test_model_ok_multi_model(ws: Path):
    assert _validate(ws / "L60程序", "model", ws).is_absolute()


def test_model_ok_empty_workspace(tmp_path: Path):
    root = tmp_path / "empty-ws"
    root.mkdir()
    _validate(root / "M1", "model", root)


def test_model_single_model_rejected(tmp_path: Path):
    root = tmp_path / "single-ws"
    (root / "通用").mkdir(parents=True)
    _reject(root / "新型号", "model", root, "migration_required")


def test_model_invalid_layout_rejected(tmp_path: Path):
    root = tmp_path / "invalid-ws"
    root.mkdir()
    (root / "散落文件.txt").write_text("x", encoding="utf-8")
    _reject(root / "M1", "model", root, "layout_invalid")


def test_model_not_direct_child_rejected(ws: Path):
    _reject(ws / "L36程序" / "子型号", "model", ws, "domain_violation")


def test_scheme_ok(ws: Path):
    _validate(ws / "L36程序" / "定制" / "新方案", "scheme", ws)


def test_scheme_wrong_parent_rejected(ws: Path):
    _reject(ws / "L36程序" / "通用" / "新方案", "scheme", ws, "domain_violation")
    _reject(ws / "新方案", "scheme", ws, "domain_violation")


def test_scheme_owner_not_model_rejected(tmp_path: Path):
    root = tmp_path / "bare-scheme"
    root.mkdir()
    _reject(root / "随便" / "定制" / "方案", "scheme", root, "domain_violation")


def test_asset_ok_common(ws: Path):
    _validate(ws / "L36程序" / "通用" / "主板程序" / "v2", "asset", ws)


def test_asset_ok_new_module(ws: Path):
    _validate(ws / "L36程序" / "通用" / "新手柄" / "v1", "asset", ws)


def test_asset_ok_custom(ws: Path):
    scheme = ws / "L36程序" / "定制" / "酒店版"
    scheme.mkdir(parents=True)
    _validate(scheme / "主板程序" / "v1", "asset", ws)


def test_asset_module_leaf_rejected(ws: Path):
    _reject(ws / "L36程序" / "通用" / "新手柄", "asset", ws, "domain_violation")


def test_asset_too_deep_rejected(ws: Path):
    _reject(
        ws / "L36程序" / "通用" / "主板程序" / "v1" / "子目录",
        "asset",
        ws,
        "domain_violation",
    )


def test_asset_custom_scheme_missing_rejected(ws: Path):
    _reject(
        ws / "L36程序" / "定制" / "不存在的方案" / "主板程序" / "v1",
        "asset",
        ws,
        "domain_violation",
    )


def test_asset_outside_domain_rejected(tmp_path: Path):
    root = tmp_path / "bare-asset"
    root.mkdir()
    _reject(root / "随机目录" / "v1", "asset", root, "domain_violation")


# ---------------------------------------------------------------------------
# 检查 4：Windows 文件名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["CON", "COM1.bin", "结尾点.", "结尾空格 ", "非法<字>", "控制\x01字符"],
)
def test_invalid_asset_name_rejected(ws: Path, bad_name: str):
    error = _reject(
        ws / "L36程序" / "通用" / "主板程序" / bad_name, "asset", ws, "invalid_name"
    )
    assert error.payload["segment"] == bad_name


def test_invalid_module_name_rejected(ws: Path):
    _reject(ws / "L36程序" / "通用" / "COM1" / "v1", "asset", ws, "invalid_name")


def test_invalid_name_reason_edges():
    assert _invalid_name_reason("") is not None
    assert _invalid_name_reason(".") is not None
    assert _invalid_name_reason("..") is not None
    assert _invalid_name_reason("正常名称.v2") is None


# ---------------------------------------------------------------------------
# 检查 5：排除判定
# ---------------------------------------------------------------------------


def test_path_excluded_by_keyword(ws: Path):
    _reject(ws / "L36程序" / "通用" / "旧手柄" / "v1", "asset", ws, "path_excluded")


def test_workspace_excluded_by_keyword(tmp_path: Path):
    root = tmp_path / "旧固件" / "ws"
    root.mkdir(parents=True)
    (root / "通用").mkdir()
    _reject(root / "通用" / "主板程序" / "v1", "asset", root, "workspace_excluded")


# ---------------------------------------------------------------------------
# 检查 6：锚点冲突
# ---------------------------------------------------------------------------


def test_anchor_conflict_static(ws: Path):
    _borrow(ws / "L50程序", "主板程序", "L36程序/通用/主板程序/v9")
    error = _reject(
        ws / "L36程序" / "通用" / "主板程序" / "v9", "asset", ws, "path_identity_conflict"
    )
    assert error.payload["hits"]


def test_anchor_conflict_platform_default(ws: Path):
    save_platform_config(
        ws / "L36程序",
        [PlatformDefaults("标准单机芯3D", {"主板程序": "v9"})],
    )
    _reject(
        ws / "L36程序" / "通用" / "主板程序" / "v9",
        "asset",
        ws,
        "path_identity_conflict",
    )


def test_blocking_issue_rejected(ws: Path):
    (ws / "L36程序" / MODEL_CONFIG_FILENAME).write_text("[[broken\n", encoding="utf-8")
    error = _reject(
        ws / "L36程序" / "通用" / "主板程序" / "v3",
        "asset",
        ws,
        "path_identity_conflict",
    )
    assert error.payload["blocking_issues"]


def test_gate_root_changed(ws: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(AdmissionError) as exc_info:
        validate_new_path(
            ws / "L36程序" / "通用" / "主板程序" / "v2",
            kind="asset",
            configured_root=str(other),
            workspace_root=str(ws),
        )
    assert exc_info.value.code == "root_changed"
