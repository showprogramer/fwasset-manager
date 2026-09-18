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


def _write_rom_only(tmp_path: Path, name: str = "fw.rom") -> Path:
    path = tmp_path / name
    path.write_bytes(b"rom-content")
    return path


# ---------------------------------------------------------------------------
# create_asset：三种 source_kind
# ---------------------------------------------------------------------------


def test_create_asset_files_success_common_scope(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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


def test_create_asset_directory_success_custom_scope(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    scheme_result = create_scheme(str(tmp_path), str(tmp_path), str(model_root), "方案A")
    assert scheme_result["ok"] is True

    src_dir = tmp_path / "_srcdir"
    src_dir.mkdir()
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
    archive = tmp_path / "_src.zip"
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()

    r1 = _create_common_asset(
        tmp_path, model_root, asset_name="程序A", files=[_write_bin(src_dir, "a.bin")]
    )
    assert r1["ok"] is True
    module_dir = model_root / "通用" / "主板"
    assert module_dir.is_dir()

    src_dir2 = tmp_path / "_src2"
    src_dir2.mkdir()
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


def test_create_asset_promote_failure_leaves_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
    bin_file = _write_bin(src_dir)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟索引失败")

    monkeypatch.setattr(svc, "bulk_reindex_subtree", _boom)
    result = _create_common_asset(tmp_path, model_root, files=[bin_file])

    assert result["ok"] is True
    assert result["code"] == "index_pending"
    assert (model_root / "通用" / "主板" / "程序A").is_dir()


# ---------------------------------------------------------------------------
# A1/A3/A4：待补齐候选区
# ---------------------------------------------------------------------------


def test_create_asset_incomplete_handcontrol_goes_to_candidate(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    assert result["code"] == "created_incomplete"
    business_path = model_root / "通用" / "手控" / "程序A"
    assert not business_path.exists()

    candidate_path = Path(result["payload"]["candidate_path"])
    assert candidate_path.is_dir()
    data, status, _err = load_asset_info_with_status(candidate_path)
    assert status == "ok"
    assert data.get("vendor") == "摩众"
    assert data.get("import_state") == "incomplete"
    assert data.get("intended_firmware_type") == "handcontrol_ui"


def test_incomplete_candidate_double_blind_zone(tmp_path: Path) -> None:
    """候选目录不被普通 scanner 发现，但能被 scan_incomplete_imports 发现。"""
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    )
    assert result["ok"] is True

    assets, issues = scan_firmware_assets(str(tmp_path))
    candidate_path = result["payload"]["candidate_path"]
    assert not any(a["path"] == candidate_path for a in assets)

    candidates, scan_issues = scan_incomplete_imports(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].path == candidate_path
    assert candidates[0].ready_to_promote is False


def test_scan_incomplete_imports_ready_to_promote_after_manual_fix(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    )
    candidate_path = Path(result["payload"]["candidate_path"])
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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


def _make_incomplete_candidate(tmp_path: Path, model_root: Path) -> str:
    src_dir = tmp_path / "_src"
    src_dir.mkdir(exist_ok=True)
    rom_file = _write_rom_only(src_dir, f"fw-{len(list(src_dir.iterdir()))}.rom")
    result = create_asset(
        str(tmp_path),
        str(tmp_path),
        source=[str(rom_file)],
        source_kind="files",
        model_root=model_root,
        scope="通用",
        module_name="手控",
        asset_name="程序A",
    )
    assert result["ok"] is True
    return result["payload"]["candidate_id"]


def test_supplement_candidate_completes_and_clears_import_state(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)

    pkg_dir = tmp_path / "_pkg"
    pkg_dir.mkdir()
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

    dup_dir = tmp_path / "_dup"
    dup_dir.mkdir()
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

    extra_dir = tmp_path / "_extra"
    extra_dir.mkdir()
    extra_file = extra_dir / "readme.txt"
    extra_file.write_bytes(b"note")

    r1 = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[extra_file]
    )
    assert r1["ok"] is True
    assert r1["payload"]["complete"] is False

    pkg_dir = tmp_path / "_pkg"
    pkg_dir.mkdir()
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

    pkg_dir = tmp_path / "_pkg"
    pkg_dir.mkdir()
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
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    candidate_id = _make_incomplete_candidate(tmp_path, model_root)
    candidate_root = managed_root(tmp_path, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    before = sorted(p.name for p in candidate_path.iterdir())

    pkg_dir = tmp_path / "_pkg"
    pkg_dir.mkdir()
    pkg_file = pkg_dir / "fw.pkg"
    pkg_file.write_bytes(b"pkg")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("模拟合并失败")

    monkeypatch.setattr(svc, "_merge_staging_into", _boom)
    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[pkg_file]
    )

    assert result["ok"] is False
    after = sorted(p.name for p in candidate_path.iterdir())
    assert before == after


# ---------------------------------------------------------------------------
# delete_asset（D2.4a）
# ---------------------------------------------------------------------------


def test_delete_asset_success_no_external_reference(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is True
    assert not asset_path.exists()
    assert "quarantine_record_id" in result["payload"]


def test_delete_asset_zero_toml_rewrite(tmp_path: Path) -> None:
    """R8 不变量：删除前后所有 TOML 逐字节一致。"""
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
    r = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    asset_path = Path(r["payload"]["asset_path"])
    (model_root / "型号配置.toml").write_text("not valid toml =====", encoding="utf-8")

    result = delete_asset(str(tmp_path), str(tmp_path), asset_path, confirm_shared=False)

    assert result["ok"] is False
    assert result["code"] == "lookup_blocked"


def test_delete_asset_last_variant_rmdir_empty_module_container(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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


def test_undo_asset_delete_rebuilds_module_container(tmp_path: Path) -> None:
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
    r1 = _create_common_asset(
        tmp_path, model_root, module_name="蓝牙", files=[_write_bin(src_dir, "fw.bin")]
    )
    assert r1["ok"] is True

    src_dir2 = tmp_path / "_src2"
    src_dir2.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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

    dup_dir = tmp_path / "_dup"
    dup_dir.mkdir()
    dup_file = dup_dir / existing_name
    dup_file.write_bytes(b"dup")

    result = supplement_candidate(
        str(tmp_path), str(tmp_path), candidate_id, files=[dup_file]
    )

    assert result["ok"] is False
    assert result["code"] == "file_exists"
    assert load_workspace_status(tmp_path).state == "clean"


# ---------------------------------------------------------------------------
# 中断恢复（阶段边界）
# ---------------------------------------------------------------------------


def test_create_asset_interrupted_after_promote_before_index_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.core.services import asset_service as svc

    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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
    """候选区提升在 os.replace **之前**失败 → 真零产物，收敛为 clean 且可重试。

    判据与 MSC-014 一致：看内容是否已离开 staging 会话，不是「有没有抛异常」。
    这里 manifest 计算发生在移动之前，会话仍在原处，必须 commit() 为 clean；
    同时异常须收束为 ServiceResult，不得裸抛穿透 service 边界
    （AGENTS.md：不以裸异常代替服务错误码）。
    """
    model_root = _make_model(tmp_path)
    src_dir = tmp_path / "_src"
    src_dir.mkdir()
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

    assert result["ok"] is False
    assert result["code"] == "promote_failed"
    assert not result.get("payload", {}).get("recovery_required")
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
