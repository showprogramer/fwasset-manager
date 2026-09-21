"""D4.3③：legacy 泛化 ``"旧"`` 关键词退役配套的只读报告入口。

``SCAN_EXCLUDE_DIR_KEYWORDS`` 里曾有泛化的 ``"旧"``，它按**子串**匹配目录名，
会连带整枝排除 ``旧款L36`` 这类真实型号目录。退役前必须让用户能看见这批被
静默吞掉的目录，因此这里提供一个纯只读的扫描入口供「软件修复」展示。

本模块不写盘、不持锁、不读配置文件；``keyword`` 可覆盖，便于在退役之后仍能
对历史口径做回归。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict

from fwasset.core.managed_paths import MANAGED_ROOT_DIRNAME, managed_path_reason

__all__ = [
    "LEGACY_GENERIC_KEYWORD",
    "LegacyExcludedDir",
    "scan_legacy_excluded_dirs",
]

LEGACY_GENERIC_KEYWORD = "旧"


class LegacyExcludedDir(TypedDict):
    """一个因泛化关键词命中而被整枝排除的目录。"""

    path: str
    matched_name: str
    is_retired_versions: bool


def scan_legacy_excluded_dirs(
    workspace_root: str | Path,
    *,
    keyword: str = LEGACY_GENERIC_KEYWORD,
) -> list[LegacyExcludedDir]:
    """列出工作区内因泛化 ``keyword`` 子串命中而被整枝排除的目录。

    - 命中即记录并**不再下钻**（与 scanner 的整枝语义一致）；
    - ``旧版本/`` 受管副本目录标记 ``is_retired_versions=True`` 一并列出，
      让用户知道它是**有意**排除的，不是误伤；
    - 其余受管类别（``.fwasset``、staging、隔离区等）与本关键词无关，不列入；
    - ``OSError`` 跳过该目录继续，不抛。
    """
    needle = str(keyword or "")
    root = Path(str(workspace_root or "").strip())
    if not needle or not root.is_dir():
        return []

    found: list[LegacyExcludedDir] = []
    resolved = root.resolve()
    _walk(resolved, needle, resolved, found)
    found.sort(key=lambda item: item["path"])
    return found


def _walk(
    current: Path, needle: str, workspace_root: Path, found: list[LegacyExcludedDir]
) -> None:
    try:
        children = sorted(current.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        # 受管容器与受管根先于关键词判定：``.fwasset/`` 下的 staging、隔离区等
        # 是应用自己的内部区域，其中名含「旧」的目录不是被泛化关键词吞掉的
        # 业务目录，列出来只会误导用户（6.2「其余受管类别不列入」）。
        if os.path.normcase(child.name) == os.path.normcase(MANAGED_ROOT_DIRNAME):
            continue
        reason = managed_path_reason(child, is_dir=True, workspace_root=workspace_root)
        if reason is not None and reason != "retired_versions":
            continue
        if needle in child.name:
            found.append(
                {
                    "path": str(child),
                    "matched_name": child.name,
                    "is_retired_versions": reason == "retired_versions",
                }
            )
            continue  # 整枝：命中后不再下钻
        if reason is not None:
            continue
        _walk(child, needle, workspace_root, found)
