"""目录 manifest 与稳定哈希（TASK-20260915-atomic-dir-primitives，父规格 D4.2）。

manifest 只描述**普通文件树**：相对路径（normcase + 正斜杠）、字节数与内容
SHA-256，不含 mtime；空目录不产生条目。树内出现任何 reparse point
（junction / 符号链接）都会被拒绝——被重定向的目录无法给出可信的内容
清单，静默跳过会让「删除本次产物」的校验形同虚设。
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fwasset.core.workspace_transaction import WorkspaceTransactionError

_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


@dataclass(frozen=True)
class FileEntry:
    """manifest 条目：相对路径（normcase + 正斜杠）、字节数、内容 SHA-256。"""

    relpath: str
    size: int
    sha256: str


class ManifestError(WorkspaceTransactionError):
    """manifest 无法安全计算（含 reparse point）。"""


def path_is_reparse_point(path: str | Path) -> bool:
    """路径本身是否为 reparse point（junction / 符号链接，不跟随目标）。"""
    candidate = Path(path)
    if os.path.islink(candidate):
        return True
    try:
        attributes = os.lstat(candidate).st_file_attributes
    except AttributeError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _normalized_relpath(root: Path, path: Path) -> str:
    relative = os.path.relpath(path, root)
    return os.path.normcase(relative).replace("\\", "/")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_manifest(
    root: str | Path, *, exclude_names: frozenset[str] = frozenset()
) -> list[FileEntry]:
    """收集 ``root`` 下全部文件的 manifest 条目（按归一化相对路径排序）。

    ``exclude_names`` 按文件名精确（normcase）排除，预留给子任务 6 的副本
    元数据文件；``程序信息.toml`` 是资产内容，默认不排除。根或树内任何条目
    是 reparse point 时抛 :class:`ManifestError`。
    """
    base = Path(root)
    if not base.is_dir():
        raise ManifestError(f"manifest 根不是目录：{base}")
    if path_is_reparse_point(base):
        raise ManifestError(f"manifest 根是链接或重定向路径，已拒绝：{base}")

    excluded = {os.path.normcase(name) for name in exclude_names}
    entries: list[FileEntry] = []
    stack = [base]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if path_is_reparse_point(entry):
                raise ManifestError(
                    f"manifest 遇到链接或重定向路径，已拒绝：{entry}"
                )
            if entry.is_dir():
                stack.append(entry)
                continue
            if os.path.normcase(entry.name) in excluded:
                continue
            entries.append(
                FileEntry(
                    relpath=_normalized_relpath(base, entry),
                    size=entry.stat().st_size,
                    sha256=_hash_file(entry),
                )
            )
    entries.sort(key=lambda item: item.relpath)
    return entries


def manifest_hash(entries: Sequence[FileEntry]) -> str:
    """对条目集合生成稳定哈希；空集合等于空输入的 SHA-256。"""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            f"{entry.relpath}\0{entry.size}\0{entry.sha256}\n".encode()
        )
    return digest.hexdigest()


def directory_manifest_hash(
    root: str | Path, *, exclude_names: frozenset[str] = frozenset()
) -> str:
    """:func:`directory_manifest` + :func:`manifest_hash` 的组合入口。"""
    return manifest_hash(directory_manifest(root, exclude_names=exclude_names))
