from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fwasset.core.manifest import (
    ManifestError,
    directory_manifest,
    directory_manifest_hash,
    manifest_hash,
)


def _make_tree(root: Path) -> None:
    (root / "子目录").mkdir()
    (root / "b.txt").write_text("内容B", encoding="utf-8")
    (root / "a.txt").write_text("内容A", encoding="utf-8")
    (root / "子目录" / "nested.bin").write_bytes(b"\x00\x01\x02")


def test_manifest_is_stable_across_path_and_creation_order(tmp_path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "另一个目录"
    left.mkdir()
    right.mkdir()

    _make_tree(left)
    (right / "子目录").mkdir()
    (right / "子目录" / "nested.bin").write_bytes(b"\x00\x01\x02")
    (right / "a.txt").write_text("内容A", encoding="utf-8")
    (right / "b.txt").write_text("内容B", encoding="utf-8")

    assert directory_manifest_hash(left) == directory_manifest_hash(right)


def test_manifest_ignores_mtime_and_empty_directories(tmp_path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("内容", encoding="utf-8")
    (root / "空目录").mkdir()

    before = directory_manifest_hash(root)
    time.sleep(0.01)
    os.utime(root / "a.txt", (1.0, 1.0))

    assert directory_manifest_hash(root) == before
    (root / "另一个空目录").mkdir()
    assert directory_manifest_hash(root) == before


def test_manifest_empty_tree_has_deterministic_hash(tmp_path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    assert directory_manifest(root) == []
    assert manifest_hash([]) == directory_manifest_hash(root)


def test_manifest_exclude_names_are_skipped(tmp_path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "keep.txt").write_text("保留", encoding="utf-8")
    (root / "skip.bin").write_text("排除", encoding="utf-8")
    (root / "子").mkdir()
    (root / "子" / "skip.bin").write_text("排除", encoding="utf-8")

    entries = directory_manifest(root, exclude_names=frozenset({"skip.bin"}))

    assert [entry.relpath for entry in entries] == ["keep.txt"]


def test_manifest_entries_are_sorted_with_normalized_relpaths(tmp_path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "B.txt").write_text("1", encoding="utf-8")
    (root / "a.txt").write_text("22", encoding="utf-8")

    entries = directory_manifest(root)

    assert [entry.relpath for entry in entries] == ["a.txt", "b.txt"]
    assert entries[0].size == 2
    assert entries[1].size == 1
    assert len(entries[1].sha256) == 64


def _try_make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_manifest_rejects_reparse_points(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "文件.txt").write_text("内容", encoding="utf-8")

    root = tmp_path / "tree"
    root.mkdir()
    (root / "normal.txt").write_text("普通", encoding="utf-8")
    if not _try_make_junction(root / "链接", real):
        pytest.skip("当前环境无法创建 junction")

    with pytest.raises(ManifestError):
        directory_manifest(root)
