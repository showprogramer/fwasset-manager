"""子树对账冷接口（REVIEW-20260728 R3 / gate #3）。

「文件先行、库可重建」的恢复策略：行级写失败或文件操作后索引与磁盘不一致时，
以磁盘扫描结果为准重建该子树的索引行。对账是冷接口——由上层 CRUD 动作在
失败恢复时显式调用，不自动重试。

防误删纪律（规格 TASK-20260901-r3-index-write-api；severity 分级见
TASK-20260916 父规格 D0.1a）：
- 扫描存在 ``severity == "error"`` 的 issue 或被取消 → 禁止写库
  （不完整扫描不得当作权威快照）；warning 级放行；
- 子树已不存在 → 视为空快照，允许清空其旧索引行（支撑删除恢复）；
- 工作区根不存在、子树存在但不是目录 → 报错不改库。
"""

from __future__ import annotations

import threading
from pathlib import Path

from fwasset.core.asset_index import (
    AssetIndexError,
    bulk_reindex_subtree,
)
from fwasset.core.file_scan import scan_firmware_subtree
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    normalize_workspace_path,
)
from fwasset.core.types import FirmwareAsset


def reconcile_subtree(
    workspace_root: str,
    subtree_root: str,
    *,
    catalog_path: str | Path | None = None,
    path: str | Path | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """以磁盘为准重建 ``subtree_root`` 子树的索引行（含隐藏项边界清理）。

    实现为「先扫描、后写库」：扫描交给 :func:`scan_firmware_subtree`（只遍历
    子树、归属按工作区根推导），写库交给 :func:`bulk_reindex_subtree`（单事务
    整批替换 + 单工作区一致性校验）。本函数只编排与分支策略，不手写字段映射。
    """
    if not normalize_workspace_path(workspace_root):
        raise AssetIndexError("工作区根目录未配置，拒绝对账")
    # 预置取消事件：任何写库（含已删子树清空）之前直接拒绝
    if cancel_event is not None and cancel_event.is_set():
        raise AssetIndexError("扫描已被取消，本次对账未写库")
    try:
        assert_within_workspace(subtree_root, workspace_root)
    except PathGuardError as exc:
        raise AssetIndexError(f"子树不在工作区内，拒绝对账：{exc}") from exc
    ws_path = Path(workspace_root)
    # 工作区根被卸载/重命名/不可访问时禁止清空索引（区分「子树已删除」
    # 与「整个工作区消失」：后者必须报错不改库）。
    if not ws_path.exists() or not ws_path.is_dir():
        raise AssetIndexError(
            f"工作区根目录不存在或不可访问，拒绝对账：{workspace_root}"
        )

    sub_path = Path(subtree_root)
    if not sub_path.exists():
        # 已删除子树 = 空快照，允许清空其旧索引行（支撑删除恢复）。
        assets: list[FirmwareAsset] = []
    else:
        if not sub_path.is_dir():
            raise AssetIndexError(f"子树不是目录，拒绝对账：{subtree_root}")
        assets, issues = scan_firmware_subtree(
            workspace_root,
            subtree_root,
            catalog_path=catalog_path,
            cancel_event=cancel_event,
        )
        # 不完整扫描（目录读错误 / 用户取消）不得当作权威快照，禁止写库；
        # warning 级（如 chassis 配置损坏）放行（TASK-20260916，父规格 D0.1a）。
        if cancel_event is not None and cancel_event.is_set():
            raise AssetIndexError("扫描已被取消，本次对账未写库")
        blocking = [
            issue for issue in issues if str(issue.get("severity")) == "error"
        ]
        if blocking:
            raise AssetIndexError(
                "子树扫描存在错误，禁止对账写库：\n"
                + "\n".join(str(issue.get("message")) for issue in blocking)
            )

    bulk_reindex_subtree(workspace_root, subtree_root, assets, path=path)
