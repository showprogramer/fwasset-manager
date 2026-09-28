from collections.abc import Callable

from fwasset.core.usb_ops import copy_directory_to_usb, eject_usb, format_usb


def run_music_flash(
    source_dir: str,
    drive: str,
    *,
    format_first: bool = True,
    eject_after: bool = True,
    log_fn: Callable[..., None] = print,
) -> dict:
    try:
        formatted = False
        if format_first:
            log_fn(f"格式化 U 盘: {drive}")
            formatted = bool(format_usb(drive, log_fn=log_fn))
            if not formatted:
                return {
                    "ok": False,
                    "code": "format_failed",
                    "message": "U 盘格式化失败",
                    "payload": {"formatted": False, "copied": False, "ejected": False},
                }

        log_fn(f"复制音乐目录: {source_dir} -> {drive}")
        copied = bool(copy_directory_to_usb(source_dir, drive, log_fn=log_fn))
        if not copied:
            return {
                "ok": False,
                "code": "copy_failed",
                "message": "复制音乐目录失败",
                "payload": {"formatted": formatted, "copied": False, "ejected": False},
            }

        ejected = False
        if eject_after:
            ejected = bool(eject_usb(drive, log_fn=log_fn))

        return {
            "ok": True,
            "code": "ok",
            "message": "音乐版 U 盘准备完成",
            "payload": {
                "formatted": formatted,
                "copied": True,
                "ejected": ejected,
                "eject_requested": eject_after,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": "service_exception",
            "message": str(exc),
            "payload": {"formatted": False, "copied": False, "ejected": False},
        }
