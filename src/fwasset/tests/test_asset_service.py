"""asset 层新增 / 删除 / 待补齐候选区服务测试（TASK-20260918-asset-crud-incomplete）。

覆盖规格「测试计划」全部用例：create_asset 三种来源、A1/A3/A4 候选区双盲区、
supplement_candidate、delete_asset D2.4a 八步、撤销（含容器重建）、
delete_candidate（D10.1c）以及 MSC-001 同构回归。工作区一律用 ``tmp_path``，
配置根与工作区根相同。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fwasset.core.asset_index import query_assets
from fwasset.core.asset_info import load_asset_info_with_status
from fwasset.core.file_scan import scan_firmware_assets
from fwasset.core.incomplete_scan import scan_incomplete_imports
from fwasset.core.managed_paths import managed_root
from fwasset.core.manifest import ManifestError
from fwasset.core.model_config import save_model_id
from fwasset.core.services.asset_service import (
    create_asset,
    delete_asset,
    delete_candidate,
    supplement_candidate,
    undo_asset_delete,
)
from fwasset.core.services.model_scheme_service import create_model, create_scheme
from fwasset.core.workspace_transaction import load_workspace_status


@pytest.fixture(autouse=True)
def _isolated_asset_index(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离索引数据库并预置 ``scan_meta`` 根，避免 ``bulk_reindex_subtree``
    因「无扫描记录」而返回 ``index_pending``（同 test_model_scheme_service
    MSC-016 用例的做法：``save_assets([], ...)`` 只写 ``scan_meta`` 一行，
    不影响后续真实写入）。db 文件必须落在工作区**之外**——放进 ``tmp_path``
    会被 ``detect_workspace_layout`` 当成无法归类的未知内容，误判
    ``layout_invalid``。
    """
    from fwasset.core.asset_index import save_assets

    db_dir = tmp_path_factory.mktemp("asset-index-db")
    db_path = db_dir / "fwasset-test-index.db"
    monkeypatch.setattr("fwasset.core.asset_index.ASSET_INDEX_PATH", db_path)
    save_assets([], str(tmp_path), db_path, scanned_at=0.0)


# ---------------------------------------------------------------------------
# 工作区脚手架
# ---------------------------------------------------------------------------


def _make_model(ws: Path, name: str = "L99程序") -> Path:
    r = create_model(str(ws), str(ws), name, "单3D")
    assert r["ok"] is True, r
    return ws / name


def _create_common_asset(
    ws: Path,
    model_root: Path,
    *,
    module_name: str = "主板",
    asset_name: str = "程序A",
    files: list[Path] | None = None,
    vendor: str = "",
) -> dict:
    return create_asset(
        str(ws),
        str(ws),
        source=[str(p) for p in files] if files is not None else [],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name=module_name,
        asset_name=asset_name,
        vendor=vendor,
    )


def _write_bin(tmp_path: Path, name: str = "fw.bin", content: bytes = b"x") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _source_dir(tmp_path: Path, name: str = "_src") -> Path:
    """工作区外的唯一来源目录（ACI-006 配套）。

    来源脚手架（散选文件 / 目录 / zip）落在工作区根会被
    ``detect_workspace_layout`` 判为无法归类内容 → ``invalid``。生产规则
    不放宽，测试来源统一放到 ``tmp_path`` 之外。
    """
    directory = tmp_path.parent / f"{tmp_path.name}-{name}"
    directory.mkdir(exist_ok=True)
    return directory


def _write_rom_only(tmp_path: Path, name: str = "fw.rom") -> Path:
    path = tmp_path / name
    path.write_bytes(b"rom-content")
    return path


# ---------------------------------------------------------------------------
# create_asset：三种 source_kind
# ---------------------------------------------------------------------------


def test_create_asset_files_success_common_scope(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    result = _create_common_asset(tmp_path, model_root, files=[bin_file], vendor="摩众")

    assert result["ok"] is True
    assert result["code"] == "ok"
    asset_path = Path(result["payload"]["asset_path"])
    assert asset_path == model_root / "通用" / "主板" / "程序A"
    assert asset_path.is_dir()
    assert (asset_path / "fw.bin").is_file()
    data, status, _err = load_asset_info_with_status(asset_path)
    assert status == "ok"
    assert data.get("vendor") == "摩众"
    # ACI-001 回归：创建后索引必须能查询到该资产（不是被清空的边界）。
    indexed_paths = {item["path"] for item in query_assets(path=None)}
    assert str(asset_path) in indexed_paths


def test_create_asset_directory_success_custom_scope(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    scheme_result = create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    assert scheme_result["ok"] is True

    src_dir = _source_dir(tmp_path, "_srcdir")
    _write_bin(src_dir, "fw.bin")

    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=str(src_dir),
        source_kind="directory",
        model_root=model_root,
        scope="定制",
        scheme_name="方案A",
        module_name="主板",
        asset_name="程序B",
    )

    assert result["ok"] is True
    asset_path = Path(result["payload"]["asset_path"])
    assert asset_path == model_root / "定制" / "方案A" / "主板" / "程序B"
    assert (asset_path / "fw.bin").is_file()


def test_create_asset_archive_success(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    archive = _source_dir(tmp_path) / "src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fw.bin", b"payload")

    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=str(archive),
        source_kind="archive",
        model_root=model_root,
        scope="通用",
        module_name="主板",
        asset_name="程序C",
    )

    assert result["ok"] is True
    asset_path = Path(result["payload"]["asset_path"])
    assert (asset_path / "fw.bin").is_file()


def test_create_asset_module_container_reused_on_second_asset(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)

    r1 = _create_common_asset(
        tmp_path, model_root, asset_name="程序A", files=[_write_bin(src_dir, "a.bin")]
    )
    assert r1["ok"] is True
    module_dir = model_root / "通用" / "主板"
    assert module_dir.is_dir()

    src_dir2 = _source_dir(tmp_path, "_src2")
    r2 = _create_common_asset(
        tmp_path, model_root, asset_name="程序B", files=[_write_bin(src_dir2, "b.bin")]
    )
    assert r2["ok"] is True
    assert (module_dir / "程序A").is_dir()
    assert (module_dir / "程序B").is_dir()


def test_create_asset_admission_out_of_workspace(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    src = _write_bin(outside, "fw.bin")

    result = _create_common_asset(tmp_path, model_root, files=[src])
    assert result["ok"] is True  # 复制来源在工作区外没问题，来源不是目标
    # 换一个真正越界的场景：目标路径本身通过 model_root 传入工作区外目录
    outside_model = outside / "L1程序"
    outside_model.mkdir(exist_ok=True)
    (outside_model / "通用").mkdir(exist_ok=True)
    (outside_model / "定制").mkdir(exist_ok=True)
    r2 = _create_common_asset(tmp_path, outside_model, files=[src])
    assert r2["ok"] is False
    assert r2["code"] == "out_of_workspace"


def test_create_asset_invalid_layout_rejected(tmp_path: Path) -> None:
    """ACI-006 回归：工作区存在无法归类内容时 create_asset 返回 layout_invalid。"""
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    # 用户在工作区根堆放了一份来源压缩包 → 布局无法归类。
    (tmp_path / "随机资料.zip").write_bytes(b"junk")

    result = _create_common_asset(tmp_path, model_root, files=[bin_file])

    assert result["ok"] is False
    assert result["code"] == "layout_invalid"
    assert not (model_root / "通用" / "主板" / "程序A").exists()


def test_create_asset_promote_failure_leaves_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    def _boom(*args: object, **kwargs: object) -> None:
        raise svc.StagingError("模拟提升失败")

    monkeypatch.setattr(svc, "promote_import", _boom)
    result = _create_common_asset(tmp_path, model_root, files=[bin_file])

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert not (model_root / "通用" / "主板" / "程序A").exists()
    assert load_workspace_status(tmp_path).state == "clean"


def test_create_asset_index_write_failure_is_ok_with_index_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟索引失败")

    # ACI-001：创建路径的索引同步已改为 reconcile_subtree（磁盘为真源）。
    monkeypatch.setattr(svc, "reconcile_subtree", _boom)
    result = _create_common_asset(tmp_path, model_root, files=[bin_file])

    assert result["ok"] is True
    assert result["code"] == "index_pending"
    assert (model_root / "通用" / "主板" / "程序A").is_dir()


# ---------------------------------------------------------------------------
# A1/A3/A4：待补齐候选区
# ---------------------------------------------------------------------------


def test_create_asset_stores_handcontrol_rom_without_pkg(tmp_path: Path) -> None:
    """导入不做内容校验：缺 .pkg 的手控来源直接落在业务目录。"""
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    rom_file = _write_rom_only(src_dir)

    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(rom_file)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="程序A",
        vendor="摩众",
    )

    assert result["ok"] is True
    assert result["code"] == "ok"
    business_path = model_root / "通用" / "手控" / "程序A"
    assert (business_path / rom_file.name).is_file()
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    if candidate_root.exists():
        assert [path for path in candidate_root.iterdir() if path.is_dir()] == []
    data, status, _err = load_asset_info_with_status(business_path)
    assert status == "ok"
    assert data.get("vendor") == "摩众"
    assert "import_state" not in data


def test_legacy_incomplete_directory_warns_and_is_not_an_asset(tmp_path: Path) -> None:
    """历史候选目录不进资产列表，扫描给出手动处理 warning。"""
    _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, tmp_path)
    candidate_path = managed_root(tmp_path, "incomplete_candidate") / candidate_id

    assets, issues = scan_firmware_assets(str(tmp_path))
    assert not any(Path(asset["path"]) == candidate_path for asset in assets)
    warnings = [
        issue
        for issue in issues
        if issue.get("severity") == "warning" and Path(str(issue.get("path"))) == candidate_path
    ]
    assert warnings
    assert "手动" in str(warnings[0]["message"])


def test_scan_incomplete_imports_ready_to_promote_after_manual_fix(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_path = managed_root(tmp_path, "incomplete_candidate") / candidate_id
    (candidate_path / "fw.pkg").write_bytes(b"pkg")

    candidates, issues = scan_incomplete_imports(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].ready_to_promote is True
    # 不自动提升：目录仍在候选区
    assert candidate_path.is_dir()


def test_scan_incomplete_imports_diagnoses_missing_metadata(tmp_path: Path) -> None:
    root = managed_root(tmp_path, "incomplete_candidate")
    from fwasset.core.workspace_transaction import WorkspaceTransaction

    with WorkspaceTransaction(tmp_path, operation="noop") as tx:
        tx.begin_product_write()
        tx.commit()
    candidate_dir = root / "orphan-1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "fw.rom").write_bytes(b"x")

    candidates, issues = scan_incomplete_imports(tmp_path)
    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "元数据" in issues[0]["message"] or "import_state" in issues[0]["message"]


def test_scan_incomplete_imports_diagnoses_empty_dir(tmp_path: Path) -> None:
    root = managed_root(tmp_path, "incomplete_candidate")
    from fwasset.core.workspace_transaction import WorkspaceTransaction

    with WorkspaceTransaction(tmp_path, operation="noop") as tx:
        tx.begin_product_write()
        tx.commit()
    candidate_dir = root / "empty-1"
    candidate_dir.mkdir(parents=True)

    candidates, issues = scan_incomplete_imports(tmp_path)
    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"


def test_scan_incomplete_imports_diagnoses_invalid_import_state(tmp_path: Path) -> None:
    from fwasset.core.workspace_transaction import WorkspaceTransaction

    with WorkspaceTransaction(tmp_path, operation="noop") as tx:
        tx.begin_product_write()
        tx.commit()
    root = managed_root(tmp_path, "incomplete_candidate")
    candidate_dir = root / "bad-state-1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "fw.rom").write_bytes(b"x")
    (candidate_dir / "程序信息.toml").write_text(
        'import_state = "bogus"\n', encoding="utf-8"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)
    assert candidates == []
    assert len(issues) == 1
    assert "非法" in issues[0]["message"]


def test_incomplete_candidate_not_indexed(tmp_path: Path) -> None:
    from fwasset.core.asset_index import query_assets

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    rom_file = _write_rom_only(src_dir)
    create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(rom_file)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="程序A",
    )

    rows = query_assets(str(tmp_path))
    assert rows == []


# ---------------------------------------------------------------------------
# supplement_candidate
# ---------------------------------------------------------------------------


def _make_incomplete_candidate(
    tmp_path: Path, model_root: Path, *, vendor: str = "摩众"
) -> str:
    """手工放一份历史候选。新建程序不再产生候选区。"""
    del model_root
    from fwasset.core.asset_info import save_candidate_metadata

    root = managed_root(tmp_path, "incomplete_candidate")
    candidate_id = "cand-rom"
    candidate_dir = root / candidate_id
    suffix = 2
    while candidate_dir.exists():
        candidate_id = f"cand-rom-{suffix}"
        candidate_dir = root / candidate_id
        suffix += 1
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    status, error = save_candidate_metadata(
        candidate_dir, vendor=vendor, intended_firmware_type="handcontrol_ui"
    )
    assert status == "ok", error
    return candidate_id


# ---------------------------------------------------------------------------
# A5b promote_candidate（ACI-003）
# ---------------------------------------------------------------------------


def test_promote_candidate_success_moves_to_business_path(tmp_path: Path) -> None:
    """ACI-003 回归：补齐完成的候选经 promote_candidate 提升为正式程序。"""
    from fwasset.core.asset_index import query_assets
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    # 用户补齐缺失的 .pkg（supplement_candidate 正常路径）。
    pkg_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = pkg_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")
    sup = supplement_candidate(str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file])
    assert sup["ok"] is True and sup["payload"]["complete"] is True

    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is True, result
    assert result["code"] == "ok"
    asset_path = Path(result["payload"]["asset_path"])
    assert asset_path == model_root / "通用" / "手控" / "手控程序A"
    assert (asset_path / "fw.rom").is_file()
    assert (asset_path / "fw.pkg").is_file()
    # 候选目录已消失；正式元数据无 import_state（Q1）。
    assert not candidate_path.exists()
    data, status, _err = load_asset_info_with_status(asset_path)
    assert status == "ok"
    assert "import_state" not in data
    # 索引可查询（ACI-001 同口径）。
    assert asset_path in {Path(item["path"]) for item in query_assets(path=None)}


def test_promote_candidate_incomplete_content_rejected(tmp_path: Path) -> None:
    """候选内容仍不完整 → invalid_candidate，候选保持原样。"""
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_candidate"
    assert candidate_path.is_dir()
    assert not (model_root / "通用" / "手控" / "手控程序A").exists()
    assert load_workspace_status(tmp_path).state == "clean"


def test_promote_candidate_target_exists_keeps_candidate(tmp_path: Path) -> None:
    """落点已存在 → 准入拒绝（path_exists），候选保持原样可重试。"""
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    pkg_dir = _source_dir(tmp_path, "_pkg")
    (pkg_dir / "fw.pkg").write_bytes(b"pkg")
    sup = supplement_candidate(str(tmp_path), str(tmp_path), candidate_id, files=[pkg_dir / "fw.pkg"])
    assert sup["ok"] is True

    # 预先占用落点。
    occupied = model_root / "通用" / "手控" / "手控程序A"
    occupied.mkdir(parents=True)

    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert candidate_path.is_dir()
    assert load_workspace_status(tmp_path).state == "clean"


def test_promote_candidate_cas_conflict_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锁内 CAS 复验失败（候选被并发改动）→ stale_candidate，候选保留。"""
    from fwasset.core.services import asset_service as svc
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    pkg_dir = _source_dir(tmp_path, "_pkg")
    (pkg_dir / "fw.pkg").write_bytes(b"pkg")
    sup = supplement_candidate(str(tmp_path), str(tmp_path), candidate_id, files=[pkg_dir / "fw.pkg"])
    assert sup["ok"] is True

    real_hash = svc.directory_manifest_hash
    calls = {"n": 0}

    def _flaky_hash(path: Path) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            (candidate_path / "concurrent.txt").write_bytes(b"race")
        return real_hash(path)

    monkeypatch.setattr(svc, "directory_manifest_hash", _flaky_hash)
    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is False
    assert result["code"] == "stale_candidate"
    assert candidate_path.is_dir()
    assert load_workspace_status(tmp_path).state == "clean"


def test_promote_candidate_keyword_type_content_succeeds(tmp_path: Path) -> None:
    """ACI-003a 回归：keyword 型内容（mainboard，dir_keywords 按 target 路径段
    匹配）在落点语境判定完整并可提升——候选区路径语境不含任何 keyword，
    会把同一份内容误判为「不完整」。"""
    from fwasset.core.asset_info import save_candidate_metadata
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = src_dir / "board.bin"
    bin_file.write_bytes(b"bin")
    created = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(bin_file)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="自定义模块",
        asset_name="程序A",
    )
    assert created["ok"] is True
    stored = model_root / "通用" / "自定义模块" / "程序A" / "board.bin"
    assert stored.is_file()

    candidate_id = "board-hist"
    candidate_path = managed_root(tmp_path, "incomplete_candidate") / candidate_id
    candidate_path.mkdir(parents=True)
    (candidate_path / "board.bin").write_bytes(b"bin")
    status, error = save_candidate_metadata(
        candidate_path, vendor="摩众", intended_firmware_type=""
    )
    assert status == "ok", error
    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="主板",
        asset_name="主板程序A",
    )

    assert result["ok"] is True, result
    asset_path = Path(result["payload"]["asset_path"])
    assert asset_path == model_root / "通用" / "主板" / "主板程序A"
    assert (asset_path / "board.bin").is_file()
    assert not candidate_path.exists()
    # ACI-003b：手工塞齐场景元数据仍带 incomplete 键——提升后正式程序不得
    # 再带候选期键（Q1），提升流程须清掉它。
    data, status, _err = load_asset_info_with_status(asset_path)
    assert status == "ok"
    assert "import_state" not in data


def test_promote_candidate_manually_completed_ready_to_promote_succeeds(
    tmp_path: Path,
) -> None:
    """ACI-003b 回归：用户手工塞齐缺失文件（scan 报 ready_to_promote=True、
    import_state 仍为 incomplete）→ 唯一显式提升入口必须放行，ACI-003 的
    孤儿影响在手工补齐场景不得复现。"""
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    # 用户绕过应用直接塞入缺失的 .pkg（不经过 supplement_candidate）。
    pkg_src = _source_dir(tmp_path, "_pkg")
    (pkg_src / "fw.pkg").write_bytes(b"pkg")
    (candidate_path / "fw.pkg").write_bytes(b"pkg")

    # scan 诊断该候选已可提升（A4 明文场景）。
    candidates, issues = scan_incomplete_imports(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].candidate_id == candidate_id
    assert candidates[0].ready_to_promote is True

    # 用户显式操作：提升。
    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is True, result
    asset_path = Path(result["payload"]["asset_path"])
    assert (asset_path / "fw.pkg").is_file()
    assert not candidate_path.exists()


def test_promote_candidate_vendor_write_failure_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-008a 回归：提升后 vendor 补写失败（含 IO 异常）→ 不 commit、
    返回 promote_failed + recovery_required，不得静默报成功。"""
    from fwasset.core.services import asset_service as svc
    from fwasset.core.services.asset_service import promote_candidate

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root, vendor="")

    pkg_src = _source_dir(tmp_path, "_pkg")
    (pkg_src / "fw.pkg").write_bytes(b"pkg")
    sup = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_src / "fw.pkg"]
    )
    assert sup["ok"] is True

    def _failing_vendor(asset_dir, vendor):
        return ("parse_error", "模拟提升后 vendor 写入失败")

    monkeypatch.setattr(svc, "save_vendor", _failing_vendor)
    result = promote_candidate(
        str(tmp_path),
        str(tmp_path),
        candidate_id,
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="手控程序A",
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert result["payload"]["recovery_required"] is True
    # 候选已移走、资产已落盘（非零产物），工作区保持待恢复现场。
    asset_path = model_root / "通用" / "手控" / "手控程序A"
    assert asset_path.is_dir()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_supplement_candidate_completes_and_clears_import_state(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)

    pkg_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = pkg_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg-content")

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is True
    assert result["payload"]["complete"] is True
    candidate_path = Path(result["payload"]["candidate_path"])
    data, status, _err = load_asset_info_with_status(candidate_path)
    assert status == "ok"
    assert "import_state" not in data
    assert "intended_firmware_type" not in data
    assert (candidate_path / "fw.pkg").is_file()


def test_supplement_candidate_rejects_same_name_file(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    existing_name = next(p.name for p in candidate_path.iterdir() if p.is_file())

    dup_dir = _source_dir(tmp_path, "_dup")
    dup_file = dup_dir / existing_name
    dup_file.write_bytes(b"dup")

    before = sorted(p.name for p in candidate_path.iterdir())
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[dup_file]
    )

    assert result["ok"] is False
    assert result["code"] == "file_exists"
    after = sorted(p.name for p in candidate_path.iterdir())
    assert before == after


def test_supplement_candidate_two_rounds_still_incomplete_then_complete(
    tmp_path: Path,
) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)

    extra_dir = _source_dir(tmp_path, "_extra")
    extra_file = extra_dir / "readme.txt"
    extra_file.write_bytes(b"note")

    r1 = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[extra_file]
    )
    assert r1["ok"] is True
    assert r1["payload"]["complete"] is False

    pkg_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = pkg_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")
    r2 = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )
    assert r2["ok"] is True
    assert r2["payload"]["complete"] is True


def test_supplement_candidate_stale_cas_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    pkg_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = pkg_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")

    real_hash = svc.directory_manifest_hash
    call_count = {"n": 0}

    def _flaky_hash(path: Path) -> str:
        call_count["n"] += 1
        if call_count["n"] == 2:
            # 第二次调用（提交前复验）之前，模拟并发在候选目录里新增文件。
            (candidate_path / "concurrent.txt").write_bytes(b"race")
        return real_hash(path)

    monkeypatch.setattr(svc, "directory_manifest_hash", _flaky_hash)

    # 第一次哈希（preimage）不受影响；第二次调用触发并发写入后计算的哈希会与
    # preimage 不一致。
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is False
    assert result["code"] == "stale_candidate"
    # 并发写入的文件仍在（我们没有清理它，只是模拟外部改动），候选目录本身未被破坏
    assert (candidate_path / "concurrent.txt").exists()
    assert not (candidate_path / "fw.pkg").exists()


def test_supplement_candidate_mid_failure_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002 回归：第二个文件写入失败 → 逆序回滚已写文件，无部分残留。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_pkg")
    f1 = src_dir / "readme1.txt"
    f1.write_bytes(b"note1")
    f2 = src_dir / "readme2.txt"
    f2.write_bytes(b"note2")

    real_copy2 = svc.shutil.copy2

    def _copy2_fail_into_candidate(src, dst, *args, **kwargs):
        # 目标进入候选目录的第二个文件失败：第一个已写入候选，须逆序回滚。
        if str(dst).startswith(str(candidate_path)) and Path(dst).name == "readme2.txt":
            raise OSError("模拟第二个文件写入失败")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(svc.shutil, "copy2", _copy2_fail_into_candidate)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[f1, f2]
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert not result.get("payload", {}).get("recovery_required")
    after = sorted(p.name for p in candidate_path.iterdir())
    assert before == after
    # 零产物失败：工作区收敛 clean，可重试。
    assert load_workspace_status(tmp_path).state == "clean"


# ---------------------------------------------------------------------------
# delete_asset（D2.4a）
# ---------------------------------------------------------------------------


def test_delete_asset_success_no_external_reference(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert not asset_path.exists()
    assert "quarantine_record_id" in result["payload"]


def test_delete_asset_index_failure_returns_index_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-004 回归：删除后边界索引失败 → ok=True, code=index_pending。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟索引失败")

    monkeypatch.setattr(svc, "bulk_reindex_subtree", _boom)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert result["code"] == "index_pending"
    # 磁盘删除已成功，只是索引未同步——不得误报普通成功。
    assert not asset_path.exists()
    assert "重新读取程序列表" in result["message"]
    assert "quarantine_record_id" in result["payload"]


def test_delete_asset_module_reconcile_failure_returns_index_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-004 回归：模块父级对账失败 → ok=True, code=index_pending。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟对账失败")

    monkeypatch.setattr(svc, "reconcile_subtree", _boom)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert result["code"] == "index_pending"
    assert not asset_path.exists()
    assert (asset_path.parent).exists() is False


def test_delete_asset_zero_toml_rewrite(tmp_path: Path) -> None:
    """R8 不变量：删除前后所有 TOML 逐字节一致。"""
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    toml_snapshots = {}
    for toml_path in tmp_path.rglob("*.toml"):
        if asset_path in toml_path.parents or toml_path.parent == asset_path:
            continue
        toml_snapshots[toml_path] = toml_path.read_bytes()

    delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    for toml_path, content in toml_snapshots.items():
        assert toml_path.read_bytes() == content


def test_delete_asset_retired_copies_counted(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    retired_dir = asset_path / "旧版本"
    retired_dir.mkdir()
    (retired_dir / "copy-20260101").mkdir()
    (retired_dir / "copy-20260102").mkdir()

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert result["payload"]["retired_copies"] == 2


def test_delete_asset_lookup_blocked_on_corrupt_config(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    (model_root / "型号配置.toml").write_text("not valid toml =====", encoding="utf-8")

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is False
    assert result["code"] == "lookup_blocked"


def test_delete_asset_lookup_runs_without_workspace_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-006 回归：反查（全工作区 TOML 扫描）在锁外执行，D8 纯预览不持锁。"""
    from fwasset.core.services import asset_service as svc
    from fwasset.core.workspace_transaction import workspace_lock_is_held

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    held_during_lookup: list[bool] = []
    real_find = svc.find_references_to

    def _tracking_find(*args: object, **kwargs: object):
        held_during_lookup.append(workspace_lock_is_held(tmp_path))
        return real_find(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "find_references_to", _tracking_find)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert held_during_lookup == [False]


def test_delete_asset_stale_preview_token_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-006 回归：锁外预览后工作区被并发改动 → stale_plan，零产物收敛 clean。"""
    from fwasset.core.services import asset_service as svc
    from fwasset.core.workspace_transaction import WorkspaceTransaction

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    real_find = svc.find_references_to

    def _mutating_find(*args: object, **kwargs: object):
        result = real_find(*args, **kwargs)  # type: ignore[arg-type]
        # 模拟并发写事务：预览 token 捕获后、删除执行前，另一个写操作完成
        # （begin_product_write 提升 generation，空提交无法体现真实变更）。
        with WorkspaceTransaction(tmp_path, operation="concurrent_op") as tx:
            tx.begin_product_write()
            tx.commit()
        return result

    monkeypatch.setattr(svc, "find_references_to", _mutating_find)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is False
    assert result["code"] == "stale_plan"
    assert asset_path.is_dir()
    assert load_workspace_status(tmp_path).state == "clean"


def test_delete_asset_external_target_mutation_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-006 复核回归：目标被外部改写（不提升 generation）→ stale_plan。

    generation token 只能发现走应用事务的并发变更；外部/绕过事务的改写
    要靠锁外记录的目标 manifest 与锁内复验的比对来拦截。
    """
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    # 反查完成后、进入锁内之前，外部直接改写目标内容（不经应用事务）。
    real_find = None
    from fwasset.core.services import asset_service as svc

    real_find = svc.find_references_to

    def _mutating_find(*args: object, **kwargs: object):
        result = real_find(*args, **kwargs)  # type: ignore[arg-type]
        (asset_path / "fw.bin").write_bytes(b"externally-rewritten")
        return result

    monkeypatch.setattr(svc, "find_references_to", _mutating_find)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is False
    assert result["code"] == "stale_plan"
    # 目标未被隔离删除，外部改写的内容原样保留；generation 未变（事务收敛 clean）。
    assert asset_path.is_dir()
    assert (asset_path / "fw.bin").read_bytes() == b"externally-rewritten"
    assert load_workspace_status(tmp_path).state == "clean"


def test_delete_asset_last_variant_rmdir_empty_module_container(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    module_dir = asset_path.parent
    assert module_dir.is_dir()

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert not module_dir.exists()


def test_delete_asset_module_container_with_retired_versions_kept(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    module_dir = asset_path.parent
    (module_dir / "旧版本").mkdir()
    (module_dir / "旧版本" / "copy-1").mkdir()

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert module_dir.is_dir()
    assert (module_dir / "旧版本").is_dir()


def test_delete_asset_container_removal_triggers_parent_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    module_dir = asset_path.parent

    calls: list[tuple[str, str]] = []
    real_reconcile = svc.reconcile_subtree

    def _tracking_reconcile(workspace_root: str, subtree_root: str, **kwargs: object) -> None:
        calls.append((workspace_root, subtree_root))
        real_reconcile(workspace_root, subtree_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "reconcile_subtree", _tracking_reconcile)

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert not module_dir.exists()
    assert (str(tmp_path), str(module_dir.parent)) in calls


# ---------------------------------------------------------------------------
# 撤销（A8）
# ---------------------------------------------------------------------------


def test_undo_asset_delete_restores_content_and_index(tmp_path: Path) -> None:

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    before_files = sorted(p.name for p in asset_path.rglob("*") if p.is_file())

    delete_result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)
    record_id = delete_result["payload"]["quarantine_record_id"]

    undo_result = undo_asset_delete(str(tmp_path), record_id)

    assert undo_result["ok"] is True
    assert asset_path.is_dir()
    after_files = sorted(p.name for p in asset_path.rglob("*") if p.is_file())
    assert before_files == after_files


def test_undo_asset_delete_reconcile_failure_returns_index_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-004 回归：撤销后对账失败 → ok=True, code=index_pending。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    delete_result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)
    record_id = delete_result["payload"]["quarantine_record_id"]

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟对账失败")

    monkeypatch.setattr(svc, "reconcile_subtree", _boom)

    undo_result = undo_asset_delete(str(tmp_path), record_id)

    assert undo_result["ok"] is True
    assert undo_result["code"] == "index_pending"
    # 撤销本身已成功：内容已回到原路径，只是索引未同步。
    assert asset_path.is_dir()
    assert "重新读取程序列表" in undo_result["message"]


def test_undo_asset_delete_rebuilds_module_container(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    module_dir = asset_path.parent

    delete_result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)
    assert not module_dir.exists()
    record_id = delete_result["payload"]["quarantine_record_id"]

    undo_result = undo_asset_delete(str(tmp_path), record_id)

    assert undo_result["ok"] is True
    assert module_dir.is_dir()
    assert asset_path.is_dir()


def test_undo_asset_delete_target_occupied_undo_conflict(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    delete_result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)
    record_id = delete_result["payload"]["quarantine_record_id"]

    # 撤销窗口内，用户/外部在原路径重新建了同名内容。
    asset_path.mkdir(parents=True)
    (asset_path / "conflict.txt").write_bytes(b"occupied")

    undo_result = undo_asset_delete(str(tmp_path), record_id)

    assert undo_result["ok"] is False
    assert undo_result["code"] == "undo_conflict"
    # 隔离内容仍保留（未被删除）
    from fwasset.core.quarantine import load_quarantine_manifest

    records = load_quarantine_manifest(tmp_path)
    assert any(rec["id"] == record_id for rec in records)


def test_undo_candidate_delete_rediscoverable(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)

    delete_result = delete_candidate(str(tmp_path), str(tmp_path), candidate_id)
    assert delete_result["ok"] is True
    record_id = delete_result["payload"]["quarantine_record_id"]

    candidates_after_delete, _issues = scan_incomplete_imports(tmp_path)
    assert candidates_after_delete == []

    undo_result = undo_asset_delete(str(tmp_path), record_id)
    assert undo_result["ok"] is True

    candidates_after_undo, _issues2 = scan_incomplete_imports(tmp_path)
    assert len(candidates_after_undo) == 1
    assert candidates_after_undo[0].candidate_id == candidate_id


# ---------------------------------------------------------------------------
# delete_candidate（D10.1c）
# ---------------------------------------------------------------------------


def test_delete_candidate_success_no_asset_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)

    called = {"hit": False}
    real_find = svc.find_references_to

    def _tracking_find(*args: object, **kwargs: object):
        called["hit"] = True
        return real_find(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "find_references_to", _tracking_find)

    result = delete_candidate(str(tmp_path), str(tmp_path), candidate_id)

    assert result["ok"] is True
    assert called["hit"] is False


def test_delete_candidate_invalid_state_rejected(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    (candidate_path / "程序信息.toml").write_text('vendor = "x"\n', encoding="utf-8")

    result = delete_candidate(str(tmp_path), str(tmp_path), candidate_id)

    assert result["ok"] is False
    assert result["code"] == "invalid_candidate"


def test_delete_candidate_path_outside_candidate_root_rejected(tmp_path: Path) -> None:
    result = delete_candidate(str(tmp_path), str(tmp_path), "../../etc")

    assert result["ok"] is False
    assert result["code"] == "invalid_candidate"


def test_delete_candidate_cas_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    real_hash = svc.directory_manifest_hash
    call_count = {"n": 0}

    def _flaky_hash(path: Path) -> str:
        call_count["n"] += 1
        if call_count["n"] == 2:
            (candidate_path / "concurrent.txt").write_bytes(b"race")
        return real_hash(path)

    monkeypatch.setattr(svc, "directory_manifest_hash", _flaky_hash)

    result = delete_candidate(str(tmp_path), str(tmp_path), candidate_id)

    assert result["ok"] is False
    assert result["code"] == "stale_candidate"
    assert candidate_path.is_dir()


# ---------------------------------------------------------------------------
# MSC-001 同构回归
# ---------------------------------------------------------------------------


def test_msc001_create_asset_admission_failure_workspace_stays_clean(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r1 = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    assert r1["ok"] is True

    src_dir2 = _source_dir(tmp_path, "_src2")
    r2 = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir2, "fw2.bin")]
    )

    assert r2["ok"] is False
    assert r2["code"] == "path_exists"
    assert load_workspace_status(tmp_path).state == "clean"


def test_msc001_delete_asset_confirmation_required_workspace_stays_clean(
    tmp_path: Path,
) -> None:
    model_a = _make_model(tmp_path, "L99程序")
    model_b = _make_model(tmp_path, "L98程序")
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_a, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    save_model_id(model_a, "shared-model-a")
    from fwasset.core.model_config import SharedModuleRef, save_shared_module

    save_shared_module(
        model_b,
        SharedModuleRef(
            module_key="蓝牙",
            source_model_id="shared-model-a",
            source_group="shared-model-a",
            source_module="蓝牙",
            source_relative_path="L99程序/通用/蓝牙/程序A",
            mode="static",
        ),
    )

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is False
    assert result["code"] == "confirmation_required"
    assert load_workspace_status(tmp_path).state == "clean"


def test_msc001_supplement_candidate_file_exists_workspace_stays_clean(
    tmp_path: Path,
) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    existing_name = next(p.name for p in candidate_path.iterdir() if p.is_file())

    dup_dir = _source_dir(tmp_path, "_dup")
    dup_file = dup_dir / existing_name
    dup_file.write_bytes(b"dup")

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[dup_file]
    )

    assert result["ok"] is False
    assert result["code"] == "file_exists"
    assert load_workspace_status(tmp_path).state == "clean"


# ---------------------------------------------------------------------------
# ACI-002：supplement_candidate 失败零改动契约
# ---------------------------------------------------------------------------


def test_supplement_candidate_empty_source_rejected(tmp_path: Path) -> None:
    """ACI-002：空来源 → empty_source，候选与工作区零变化。"""
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    result = supplement_candidate(str(tmp_path), str(tmp_path), candidate_id, files=[])

    assert result["ok"] is False
    assert result["code"] == "empty_source"
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_batch_duplicate_names_rejected(tmp_path: Path) -> None:
    """ACI-002：批内大小写等价重名 → duplicate_name（stage_import_files 口径）。"""
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_dupbatch")
    a = src_dir / "Readme.TXT"
    a.write_bytes(b"a")
    b = src_dir / "readme.txt"
    b.write_bytes(b"b")

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[a, b]
    )

    assert result["ok"] is False
    assert result["code"] == "duplicate_name"
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_managed_source_rejected(tmp_path: Path) -> None:
    """ACI-002：受管来源（候选区内部文件）→ source_managed，不进入流程。"""
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())
    source_inside_candidate = next(p for p in candidate_path.iterdir() if p.is_file())

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[source_inside_candidate]
    )

    assert result["ok"] is False
    assert result["code"] == "source_managed"
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_metadata_file_skipped(tmp_path: Path) -> None:
    """ACI-002：来源全部是元数据文件 → empty_source（元数据跳过口径）。"""
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_meta")
    meta = src_dir / "程序信息.toml"
    meta.write_text('vendor = "x"\n', encoding="utf-8")

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[meta]
    )

    assert result["ok"] is False
    assert result["code"] == "empty_source"
    assert sorted(p.name for p in candidate_path.iterdir()) == before


def test_supplement_candidate_cas_manifest_error_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002：CAS 复验 ManifestError 发生在首个产品写之前 → clean 可重试。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = src_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")

    real_hash = svc.directory_manifest_hash
    calls = {"n": 0}

    def _hash_error_on_recheck(path: Path) -> str:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ManifestError("模拟复验清单失败")
        return real_hash(path)

    monkeypatch.setattr(svc, "directory_manifest_hash", _hash_error_on_recheck)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is False
    assert result["code"] == "stale_candidate"
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_metadata_clear_failure_no_false_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002：补齐时 clear_candidate_metadata 非 ok → 回滚新增文件，
    不得误报 complete=True；候选保持 incomplete 原样，可重试。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = src_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")

    def _broken_clear(asset_dir):
        return ("write_error", "模拟元数据清除失败")

    monkeypatch.setattr(svc, "clear_candidate_metadata", _broken_clear)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert result.get("payload", {}).get("complete") is not True
    after = sorted(p.name for p in candidate_path.iterdir())
    assert before == after
    data, status, _err = load_asset_info_with_status(candidate_path)
    assert status == "ok"
    assert data.get("import_state") == "incomplete"
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_completing_batch_write_failure_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002a 回归：补齐批次（matched 非 None）copy 失败 → 经 ServiceResult
    返回错误，不裸抛 UnboundLocalError；候选保持原样、工作区 clean。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_pkg")
    pkg_file = src_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")

    real_copy2 = svc.shutil.copy2

    def _copy2_fail_into_candidate(src, dst, *args, **kwargs):
        if str(dst).startswith(str(candidate_path)):
            raise OSError("模拟补齐批次写入失败")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(svc.shutil, "copy2", _copy2_fail_into_candidate)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert not result.get("payload", {}).get("recovery_required")
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"


def test_supplement_candidate_partial_copy_cleans_half_written_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002b 回归：copy2 写入目标途中失败（半成品已创建）→ 半成品被清理，
    候选目录零残留；否则残留文件会让重试同批文件被 file_exists 拒绝。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    src_dir = _source_dir(tmp_path, "_pkg")
    f1 = src_dir / "readme.txt"
    f1.write_bytes(b"note")

    real_copy2 = svc.shutil.copy2

    def _copy2_half_write_then_fail(src, dst, *args, **kwargs):
        if not str(dst).startswith(str(candidate_path)):
            # staging 侧复制正常完成；只在候选目录目标上模拟半成品失败。
            return real_copy2(src, dst, *args, **kwargs)
        # 先创建半成品目标（模拟磁盘满前的部分写入），再抛错。
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(b"partial")
        raise OSError("模拟写入目标途中失败（磁盘满）")

    monkeypatch.setattr(svc.shutil, "copy2", _copy2_half_write_then_fail)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[f1]
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert sorted(p.name for p in candidate_path.iterdir()) == before
    assert load_workspace_status(tmp_path).state == "clean"

    # 可重试：残留清理干净后，同批文件补齐不再被 file_exists 拒绝。
    monkeypatch.undo()
    retry = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[f1]
    )
    assert retry["ok"] is True


def test_supplement_candidate_rollback_failure_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-002：新增文件回滚失败（unlink 失败）→ 不 commit，
    返回 recovery_required，不得报告普通成功。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    src_dir = _source_dir(tmp_path, "_pkg")
    f1 = src_dir / "readme1.txt"
    f1.write_bytes(b"note1")
    f2 = src_dir / "readme2.txt"
    f2.write_bytes(b"note2")

    real_copy2 = svc.shutil.copy2
    real_unlink = Path.unlink

    def _copy2_fail_into_candidate(src, dst, *args, **kwargs):
        if str(dst).startswith(str(candidate_path)) and Path(dst).name == "readme2.txt":
            raise OSError("模拟第二个文件写入失败")
        return real_copy2(src, dst, *args, **kwargs)

    def _unlink_fail(self, *args, **kwargs):
        # 回滚删除第一个已写入候选文件失败 → 补偿不完整。
        if self.parent == candidate_path and self.name == "readme1.txt":
            raise OSError("模拟回滚 unlink 失败")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(svc.shutil, "copy2", _copy2_fail_into_candidate)
    monkeypatch.setattr(Path, "unlink", _unlink_fail)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[f1, f2]
    )

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert result["payload"]["recovery_required"] is True
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_create_asset_vendor_write_failure_not_reported_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACI-008 回归：save_vendor 非 ok → 不得返回普通成功（契约字段缺失）。"""
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    def _failing_vendor(asset_dir, vendor):
        return ("write_error", "模拟 vendor 写入失败")

    monkeypatch.setattr(svc, "save_vendor", _failing_vendor)
    result = _create_common_asset(tmp_path, model_root, files=[bin_file], vendor="摩众")

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert result["payload"]["recovery_required"] is True
    # 磁盘侧资产已落盘（非零产物），工作区必须保持待恢复现场。
    assert (model_root / "通用" / "主板" / "程序A").is_dir()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_create_asset_stores_unknown_suffix(tmp_path: Path) -> None:
    """未知后缀同样直接入库，不进入候选区。"""
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    odd = src_dir / "notes.zzz"
    odd.write_bytes(b"x")
    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(odd)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="主板",
        asset_name="程序B",
    )
    assert result["ok"] is True
    assert (model_root / "通用" / "主板" / "程序B" / "notes.zzz").is_file()


# ---------------------------------------------------------------------------
# 中断恢复（阶段边界）
# ---------------------------------------------------------------------------


def test_create_asset_interrupted_after_promote_before_index_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    bin_file = _write_bin(src_dir)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟中断")

    monkeypatch.setattr(svc, "save_vendor", _boom)

    with pytest.raises(RuntimeError):
        _create_common_asset(tmp_path, model_root, files=[bin_file])

    asset_path = model_root / "通用" / "主板" / "程序A"
    assert asset_path.is_dir()
    assert (asset_path / "fw.bin").is_file()
    assert load_workspace_status(tmp_path).state == "recovery_required"

    from fwasset.core.workspace_transaction import (
        recover_interrupted_workspace,
    )

    recover_interrupted_workspace(tmp_path)
    from fwasset.core.asset_reconcile import reconcile_subtree

    # 恢复后补一次对账即可收敛（不要求自动清除 recovery_required——那是
    # 启动恢复流程的职责，本用例只断言磁盘侧资产完整且可补救）。
    reconcile_subtree(str(tmp_path), str(asset_path))


def test_delete_asset_interrupted_after_quarantine_before_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    def _boom(*args: object, **kwargs: object) -> None:
        # KeyboardInterrupt 不被 delete_asset 内 `except Exception`（索引写入
        # 失败降级为 index_pending）吞掉，能真实模拟隔离登记之后、rmdir 之前
        # 的进程中断（同 test_model_scheme_service MSC-016 用例手法）。
        raise KeyboardInterrupt("模拟隔离登记后、rmdir 前进程退出")

    monkeypatch.setattr(svc, "bulk_reindex_subtree", _boom)

    with pytest.raises(KeyboardInterrupt):
        delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert not asset_path.exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"

    from fwasset.core.quarantine import load_quarantine_manifest

    records = load_quarantine_manifest(tmp_path)
    assert len(records) == 1

    from fwasset.core.workspace_transaction import (
        _remove_file,
        _state_root,
        _write_generation,
        _write_state,
        recover_interrupted_workspace,
    )

    recover_interrupted_workspace(tmp_path)
    # recover_interrupted_workspace 只把现场收敛为可诊断的 recovery_required，
    # 不自动放行写入（真正的启动恢复编排属子任务 8，尚未实现）。本用例只
    # 断言「撤销仍可用」这条契约本身：隔离内容完整，人工确认现场无误后
    # （这里用 commit() 同款的落盘顺序模拟）收敛回 clean，撤销能正确把
    # 资产移回原路径。
    status = load_workspace_status(tmp_path)
    generation = status.generation + (status.generation % 2)
    _write_generation(tmp_path, generation)
    _write_state(tmp_path, "clean")
    _remove_file(_state_root(tmp_path) / "operation.json")

    undo_result = undo_asset_delete(str(tmp_path), records[0]["id"])
    assert undo_result["ok"] is True
    assert asset_path.is_dir()


# ---------------------------------------------------------------------------
# MSC-014 同判据回归：候选区提升的零产物失败不得锁死工作区
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [ManifestError("清单计算失败"), OSError("磁盘错误")],
    ids=["manifest_error", "os_error"],
)
def test_candidate_promote_zero_product_failure_keeps_workspace_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """新建不再走候选区提升。候选区 manifest 钩子损坏也不影响直接入库。"""
    model_root = _make_model(tmp_path)
    src_dir = _source_dir(tmp_path)
    rom_file = _write_rom_only(src_dir)

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr("fwasset.core.services.asset_service.manifest_hash", _boom)

    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(rom_file)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="程序A",
        vendor="摩众",
    )

    assert result["ok"] is True, result
    assert (model_root / "通用" / "手控" / "程序A" / rom_file.name).is_file()
    assert load_workspace_status(tmp_path).state == "clean"

    # 可重试：同一工作区随后仍能正常完成一次新增。
    retry = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(_write_bin(src_dir, "board.bin"))],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="主板程序",
        asset_name="程序B",
        vendor="摩众",
    )
    assert retry["ok"] is True
