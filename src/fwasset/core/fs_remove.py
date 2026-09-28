"""删除文件和目录树，自动去掉 Windows 只读属性。

厂商固件常带只读属性，``copy2`` / ``copytree`` 会原样保留它；Windows 上
``unlink`` / ``rmtree`` 遇到只读文件直接报「拒绝访问」（WinError 5）。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fwasset.core.manifest import path_is_reparse_point


def _make_writable(path: str | Path) -> None:
    # reparse point 上 chmod 会跟随到链接目标，不能动。
    if not path_is_reparse_point(path):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def unlink_force(path: str | Path) -> None:
    """删除单个文件（或链接本身）；只读文件先去掉只读属性再删。"""
    target = Path(path)
    try:
        target.unlink()
    except PermissionError:
        if path_is_reparse_point(target):
            raise
        _make_writable(target)
        target.unlink()


def _retry_writable(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    if not isinstance(exc, PermissionError) or path_is_reparse_point(path):
        raise exc
    _make_writable(path)
    func(path)


def rmtree_force(path: str | Path) -> None:
    """``shutil.rmtree``，遇到只读文件先去掉只读属性再重试。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_writable)
    else:
        shutil.rmtree(
            path, onerror=lambda func, target, info: _retry_writable(func, target, info[1])
        )
