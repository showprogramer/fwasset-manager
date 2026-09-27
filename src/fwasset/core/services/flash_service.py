from collections.abc import Callable
from pathlib import Path

from fwasset.core.asset_helpers import handcontrol_copy_filenames
from fwasset.core.types import ServiceResult
from fwasset.core.usb_ops import (
    copy_named_files_to_usb,
    copy_to_usb,
    eject_usb,
    format_usb,
)


def _usb_result(
    ok: bool,
    code: str,
    message: str,
    *,
    format_ok: bool,
    copy_ok: bool,
    ejected: bool,
) -> ServiceResult:
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "payload": {
            "format_ok": format_ok,
            "copy_ok": copy_ok,
            "ejected": ejected,
        },
    }


def run_handcontrol_copy(
    drive: str,
    source_dir: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """格式化 U 盘，把手控固件文件复制到根目录，然后弹出。"""
    try:
        folder = Path(source_dir)
        if not folder.is_dir():
            log_fn("没有可复制的固件文件，流程中止")
            return _usb_result(
                False,
                "files_missing",
                "没有可复制的固件文件",
                format_ok=False,
                copy_ok=False,
                ejected=False,
            )
        selected = handcontrol_copy_filenames(
            [path.name for path in folder.iterdir() if path.is_file()]
        )
        if not selected:
            log_fn("没有可复制的固件文件，流程中止")
            return _usb_result(
                False,
                "files_missing",
                "没有可复制的固件文件",
                format_ok=False,
                copy_ok=False,
                ejected=False,
            )

        log_fn("复制到 U 盘")
        formatted = format_usb(drive, log_fn)
        if not formatted:
            log_fn("格式化失败，流程中止")
            return _usb_result(
                False,
                "format_failed",
                "格式化失败",
                format_ok=False,
                copy_ok=False,
                ejected=False,
            )

        copied = copy_named_files_to_usb(source_dir, selected, drive, log_fn)
        if not copied:
            log_fn("复制失败，流程中止")
            return _usb_result(
                False,
                "copy_failed",
                "复制失败",
                format_ok=True,
                copy_ok=False,
                ejected=False,
            )

        ejected = bool(eject_usb(drive, log_fn))
        message = "已复制到 U 盘" if ejected else "文件已复制，请手动弹出 U 盘"
        return _usb_result(
            True,
            "ok",
            message,
            format_ok=True,
            copy_ok=True,
            ejected=ejected,
        )
    except Exception as exc:
        return _usb_result(
            False,
            "service_exception",
            str(exc),
            format_ok=False,
            copy_ok=False,
            ejected=False,
        )


def run_one_click(
    drive: str,
    model: str,
    version: str,
    rom_path: str,
    pkg_path: str,
    log_fn: Callable[..., None] = print,
) -> dict:
    try:
        log_fn("=" * 50)
        log_fn(f"一键执行: {model} {version}")

        formatted = format_usb(drive, log_fn)
        if not formatted:
            log_fn("格式化失败，流程中止")
            return {
                "ok": False,
                "code": "format_failed",
                "message": "格式化失败",
                "payload": {
                    "format_ok": False,
                    "copy_ok": False,
                    "ejected": False,
                },
            }

        copied = copy_to_usb(rom_path, pkg_path, drive, log_fn)
        if not copied:
            log_fn("复制失败，流程中止")
            return {
                "ok": False,
                "code": "copy_failed",
                "message": "复制失败",
                "payload": {
                    "format_ok": True,
                    "copy_ok": False,
                    "ejected": False,
                },
            }

        ejected = bool(eject_usb(drive, log_fn))
        return {
            "ok": True,
            "code": "ok",
            "message": "一键执行完成",
            "payload": {
                "format_ok": True,
                "copy_ok": True,
                "ejected": ejected,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": "service_exception",
            "message": str(exc),
            "payload": {
                "format_ok": False,
                "copy_ok": False,
                "ejected": False,
            },
        }
