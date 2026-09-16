from __future__ import annotations

import threading
from collections.abc import Callable

from fwasset.core.asset_index import count_assets, load_scan_meta, save_assets
from fwasset.core.file_scan import handcontrol_folders_from_assets, scan_firmware_assets
from fwasset.core.types import ServiceResult


def build_scan_result(
    root: str,
    log_fn: Callable[..., None] = print,
    cancel_event: threading.Event | None = None,
) -> ServiceResult:
    try:
        log_fn(f"扫描中: {root}")
        assets, issues = scan_firmware_assets(
            root,
            cancel_event=cancel_event,
        )
        # 诊断分级（D0.1a）：errors 只含 error 级（目录读取失败 / 取消），
        # warnings 只含 warning 级（chassis 配置损坏），message 文案不变。
        errors = [
            issue["message"] for issue in issues if issue.get("severity") == "error"
        ]
        warnings = [
            issue["message"] for issue in issues if issue.get("severity") == "warning"
        ]

        if cancel_event is not None and cancel_event.is_set():
            return {
                "ok": True,
                "code": "cancelled",
                "message": "扫描已被用户取消",
                "payload": {
                    "assets": [],
                    "folders": [],
                    "errors": errors,
                    "warnings": warnings,
                },
            }

        # 从本次 assets 派生，避免 find_handcontrol_folders 再扫一整遍树
        folders = handcontrol_folders_from_assets(assets)
        save_assets(assets, root)
        message = "扫描完成"
        if errors:
            message = f"扫描完成（{len(errors)} 个目录读取失败）"
        return {
            "ok": True,
            "code": "ok",
            "message": message,
            "payload": {
                "assets": assets,
                "folders": folders,
                "errors": errors,
                "warnings": warnings,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": "scan_failed",
            "message": str(exc),
            "payload": {
                "assets": [],
                "folders": [],
                "errors": [],
                "warnings": [],
            },
        }


def build_cached_scan_result(log_fn: Callable[..., None] = print) -> ServiceResult:
    """读取本地索引状态；任何索引/DB 异常都映射为 ServiceResult，禁止裸抛。"""
    try:
        asset_count = count_assets()
        meta = load_scan_meta()
        if not asset_count:
            return {
                "ok": True,
                "code": "index_empty",
                "message": "本地资产索引为空，请首次扫描根目录",
                "payload": {
                "assets": [],
                "folders": [],
                "errors": [],
                "warnings": [],  # 缓存读取无新扫描 warning（与 build_scan_result 对齐）
                "scan_meta": meta,
                "asset_count": 0,
                },
            }

        log_fn(f"本地资产索引已有 {asset_count} 个程序条目")
        return {
            "ok": True,
            "code": "ok",
            "message": "已读取本地资产索引状态",
            "payload": {
                "assets": [],
                "folders": [],
                "errors": [],
                "warnings": [],  # 缓存读取无新扫描 warning（与 build_scan_result 对齐）
                "scan_meta": meta,
                "asset_count": asset_count,
            },
        }
    except Exception as exc:
        # 含 AssetIndexError、sqlite3.OperationalError（锁库）等；与 build_scan_result 一致不裸抛
        return {
            "ok": False,
            "code": "index_unavailable",
            "message": str(exc),
            "payload": {
                "assets": [],
                "folders": [],
                "errors": [str(exc)],
                "warnings": [],  # 与 build_scan_result 对齐；缓存读取无新扫描 warning
                "scan_meta": [],
            },
        }
