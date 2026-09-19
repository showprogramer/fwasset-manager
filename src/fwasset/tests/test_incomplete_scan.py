"""``scan_incomplete_imports`` 单元测试（TASK-20260918-asset-crud-incomplete，A4）。

直接操作受管候选区磁盘内容（不经过 ``asset_service``），覆盖诊断分级、
``ready_to_promote`` 判定与空工作区场景。与 ``test_asset_service.py`` 的
端到端场景互补——那边验证 create_asset → 候选区 → scan 全链路，这里验证
scan 本身对各种磁盘现场的独立判定。
"""

from __future__ import annotations

from pathlib import Path

from fwasset.core.asset_info import save_candidate_metadata
from fwasset.core.incomplete_scan import scan_incomplete_imports
from fwasset.core.managed_paths import managed_root
from fwasset.core.workspace_transaction import WorkspaceTransaction


def _init_workspace(ws: Path) -> None:
    with WorkspaceTransaction(ws, operation="noop") as tx:
        tx.begin_product_write()
        tx.commit()


def _candidate_dir(ws: Path, candidate_id: str) -> Path:
    root = managed_root(ws, "incomplete_candidate")
    path = root / candidate_id
    path.mkdir(parents=True)
    return path


def test_scan_empty_workspace_no_candidates(tmp_path: Path) -> None:
    candidates, issues = scan_incomplete_imports(tmp_path)
    assert candidates == []
    assert issues == []


def test_scan_finds_valid_incomplete_candidate(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "abc12345-fw")
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    save_candidate_metadata(
        candidate_dir, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert issues == []
    assert len(candidates) == 1
    item = candidates[0]
    assert item.candidate_id == "abc12345-fw"
    assert item.vendor == "摩众"
    assert item.intended_firmware_type == "handcontrol_ui"
    assert item.ready_to_promote is False


def test_scan_ready_to_promote_when_complete(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "abc12345-fw")
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    (candidate_dir / "fw.pkg").write_bytes(b"pkg")
    save_candidate_metadata(
        candidate_dir, vendor="", intended_firmware_type="handcontrol_ui"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert issues == []
    assert len(candidates) == 1
    assert candidates[0].ready_to_promote is True
    # 不自动提升：候选目录原样保留
    assert candidate_dir.is_dir()


def test_scan_metadata_only_directory_diagnosed_as_empty(tmp_path: Path) -> None:
    """ACI-007 回归：候选目录只有 ``程序信息.toml`` 时按「目录空」诊断。

    受管元数据不是固件内容——把元数据算作有效内容会让空候选逃过
    「目录空」诊断，形成普通 scanner 不认、候选 scanner 也不报的双盲死角。
    """
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "meta-only")
    save_candidate_metadata(
        candidate_dir, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "空" in issues[0]["message"]


def test_scan_nested_rom_and_pkg_ready_to_promote(tmp_path: Path) -> None:
    """ACI-007 回归：完整性判定使用递归相对路径清单。

    rom 与 pkg 位于子目录时，顶层文件名清单看不到它们；create 分流用
    ``rglob`` 递归清单判定，候选扫描若只看顶层文件名，同一内容会得出
    相反结论（口径漂移）。
    """
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "nested-content")
    subdir = candidate_dir / "V1.0"
    subdir.mkdir()
    (subdir / "fw.rom").write_bytes(b"rom")
    (subdir / "fw.pkg").write_bytes(b"pkg")
    save_candidate_metadata(
        candidate_dir, vendor="", intended_firmware_type="handcontrol_ui"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert issues == []
    assert len(candidates) == 1
    assert candidates[0].ready_to_promote is True


def test_scan_diagnoses_missing_metadata_file(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "no-meta")
    (candidate_dir / "fw.rom").write_bytes(b"rom")

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert issues[0]["path"].endswith("程序信息.toml")


def test_scan_diagnoses_parse_error(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "bad-toml")
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    (candidate_dir / "程序信息.toml").write_bytes(b"not = [valid toml")

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "解析" in issues[0]["message"] or "parse" in issues[0]["message"].lower()


def test_scan_diagnoses_empty_directory(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    _candidate_dir(tmp_path, "empty-one")

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "空" in issues[0]["message"]


def test_scan_diagnoses_illegal_import_state_value(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "bad-state")
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    (candidate_dir / "程序信息.toml").write_text(
        'import_state = "complete"\n', encoding="utf-8"
    )

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert "非法" in issues[0]["message"]


def test_scan_ignores_metadata_missing_import_state_key(tmp_path: Path) -> None:
    """没有 import_state 键 = 普通程序信息，不是待补齐候选，但会给出诊断提示。"""
    _init_workspace(tmp_path)
    candidate_dir = _candidate_dir(tmp_path, "no-import-state")
    (candidate_dir / "fw.rom").write_bytes(b"rom")
    (candidate_dir / "程序信息.toml").write_text('vendor = "摩众"\n', encoding="utf-8")

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert candidates == []
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"


def test_scan_multiple_candidates_mixed_results(tmp_path: Path) -> None:
    _init_workspace(tmp_path)

    good = _candidate_dir(tmp_path, "good-1")
    (good / "fw.rom").write_bytes(b"rom")
    save_candidate_metadata(good, vendor="摩众", intended_firmware_type="handcontrol_ui")

    _candidate_dir(tmp_path, "empty-1")

    bad_state = _candidate_dir(tmp_path, "bad-state-1")
    (bad_state / "fw.rom").write_bytes(b"rom")
    (bad_state / "程序信息.toml").write_text('import_state = "x"\n', encoding="utf-8")

    candidates, issues = scan_incomplete_imports(tmp_path)

    assert len(candidates) == 1
    assert candidates[0].candidate_id == "good-1"
    assert len(issues) == 2


def test_scan_missing_candidate_root_returns_empty(tmp_path: Path) -> None:
    # 候选区根尚未初始化（从未发生过任何写操作）：视为空，不报错。
    candidates, issues = scan_incomplete_imports(tmp_path)
    assert candidates == []
    assert issues == []
