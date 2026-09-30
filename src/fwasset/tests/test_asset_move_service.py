"""沿用原文件修改程序目录的服务边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

import fwasset.core.services.asset_move_service as asset_move_service
from fwasset.core.model_config import (
    SharedModuleRef,
    load_shared_modules,
    save_shared_module,
)
from fwasset.core.services.asset_move_service import move_asset_in_place
from fwasset.core.services.model_scheme_service import create_model, create_scheme
from fwasset.core.workspace_transaction import load_workspace_status


def _model(ws: Path, name: str) -> Path:
    result = create_model(ws, ws, name, "单3D")
    assert result["ok"], result
    return ws / name


def _asset(model: Path) -> Path:
    old = model / "通用" / "主板程序" / "误填名称"
    old.mkdir(parents=True)
    (old / "firmware.bin").write_bytes(b"original firmware")
    (old / "程序信息.toml").write_text('vendor = "摩众"\n', encoding="utf-8")
    (old / "旧版本" / "sample").mkdir(parents=True)
    (old / "旧版本" / "sample" / "old.bin").write_bytes(b"older")
    return old


def test_rename_moves_original_tree_and_rewrites_borrower(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36")
    borrower = _model(tmp_path, "L50")
    old = _asset(model)
    save_shared_module(
        borrower,
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path="L36/通用/主板程序/误填名称",
            mode="static",
        ),
    )
    target = old.parent / "正确名称"
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["ok"], result
    assert not old.exists()
    assert (target / "firmware.bin").read_bytes() == b"original firmware"
    assert (target / "旧版本" / "sample" / "old.bin").read_bytes() == b"older"
    assert load_shared_modules(borrower)[0].source_relative_path == (
        "L36/通用/主板程序/正确名称"
    )
    assert load_workspace_status(tmp_path).state == "clean"


def test_scope_move_keeps_original_files(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36")
    assert create_scheme(tmp_path, tmp_path, model, "西班牙")["ok"]
    old = _asset(model)
    target = model / "定制" / "西班牙" / "主板程序" / old.name
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["ok"], result
    assert not old.exists()
    assert (target / "firmware.bin").read_bytes() == b"original firmware"
    assert (target / "程序信息.toml").read_text(encoding="utf-8") == 'vendor = "摩众"\n'


def test_type_change_moves_original_files(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36")
    old = _asset(model)
    target = model / "通用" / "蓝牙程序" / old.name
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["ok"], result
    assert not old.exists()
    assert (target / "firmware.bin").read_bytes() == b"original firmware"


def test_semantic_change_with_borrower_is_blocked(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36")
    borrower = _model(tmp_path, "L50")
    old = _asset(model)
    save_shared_module(
        borrower,
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path="L36/通用/主板程序/误填名称",
            mode="static",
        ),
    )
    target = model / "通用" / "蓝牙程序" / old.name
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["code"] == "reference_conflict"
    assert old.exists()
    assert not target.exists()


def test_occupied_target_preserves_original(tmp_path: Path) -> None:
    model = _model(tmp_path, "L36")
    old = _asset(model)
    target = old.parent / "已存在"
    target.mkdir()
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["code"] == "path_exists"
    assert (old / "firmware.bin").read_bytes() == b"original firmware"


def test_rewrite_failure_moves_directory_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(tmp_path, "L36")
    old = _asset(model)
    target = old.parent / "正确名称"
    monkeypatch.setattr(
        asset_move_service,
        "apply_rewrite_plan",
        lambda *_args, **_kwargs: {
            "ok": False,
            "code": "rolled_back",
            "message": "改写已回滚",
            "payload": {},
        },
    )
    result = move_asset_in_place(tmp_path, tmp_path, old, target)
    assert result["code"] == "rewrite_failed"
    assert (old / "firmware.bin").read_bytes() == b"original firmware"
    assert not target.exists()
    assert load_workspace_status(tmp_path).state == "clean"
