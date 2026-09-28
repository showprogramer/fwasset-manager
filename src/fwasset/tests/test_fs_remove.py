from __future__ import annotations

import os
import stat

from fwasset.core.fs_remove import rmtree_force, unlink_force


def test_unlink_force_removes_read_only_file(tmp_path) -> None:
    target = tmp_path / "fw.bin"
    target.write_bytes(b"x")
    os.chmod(target, stat.S_IREAD)

    unlink_force(target)

    assert not target.exists()


def test_rmtree_force_removes_nested_read_only_files(tmp_path) -> None:
    root = tmp_path / "asset"
    nested = root / "旧版本" / "v1"
    nested.mkdir(parents=True)
    for path in (root / "a.bin", nested / "b.bin"):
        path.write_bytes(b"x")
        os.chmod(path, stat.S_IREAD)

    rmtree_force(root)

    assert not root.exists()
