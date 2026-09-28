import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from fwasset.core.fs_remove import rmtree_force, unlink_force
from fwasset.core.managed_paths import should_exclude_managed_path

_PERMISSION_HINTS = [
    "access is denied",
    "requires elevation",
    "administrator",
    "拒绝访问",
    "需要提升",
]


def _extract_drive_letter(drive: str) -> str:
    text = str(drive or "").strip()
    if not text:
        return ""
    if len(text) >= 2 and text[1] == ":":
        return text[0].upper()
    p = Path(text)
    anchor = str(p.anchor)
    if len(anchor) >= 2 and anchor[1] == ":":
        return anchor[0].upper()
    return text[0].upper()


def _looks_like_permission_denied(text: str) -> bool:
    lower_text = (text or "").lower()
    return any(hint in lower_text for hint in _PERMISSION_HINTS)


def get_usb_drives() -> list[str]:
    """Return removable USB drive mountpoints, e.g. ['E:\\', 'F:\\']."""
    drives = []
    for part in psutil.disk_partitions(all=False):
        if "removable" in part.opts.lower() or part.fstype.upper() in (
            "FAT32",
            "FAT",
            "EXFAT",
        ):
            drives.append(part.mountpoint)
    return drives


def copy_to_usb(
    rom_path: str, pkg_path: str, drive: str, log_fn: Callable[..., None] = print
) -> bool:
    """Copy ROM and PKG to USB root, replacing existing ROM/PKG files first."""
    drive_root = Path(drive)
    try:
        for old in drive_root.iterdir():
            if old.suffix.lower() in (".rom", ".pkg"):
                unlink_force(old)
                log_fn(f"  移除旧文件: {old.name}")

        shutil.copy2(rom_path, drive_root / Path(rom_path).name)
        log_fn(f"  已复制: {Path(rom_path).name}")
        shutil.copy2(pkg_path, drive_root / Path(pkg_path).name)
        log_fn(f"  已复制: {Path(pkg_path).name}")
        return True
    except Exception as e:
        log_fn(f"  复制失败: {e}")
        return False


def _ignore_managed_entries(directory: str, names: list[str]) -> set[str]:
    """copytree 回调：过滤受管路径（``旧版本/``、``程序信息.toml``）。

    父规格 D4.5：这些是应用内部内容，不应随程序目录进入 U 盘。判定统一走
    :func:`fwasset.core.managed_paths.should_exclude_managed_path`，不在此处
    复制字符串规则。这里不传 ``workspace_root``——来源可能已脱离工作区，只需
    按目录段与文件名判定的两类规则生效。
    """
    base = Path(directory)
    ignored: set[str] = set()
    for name in names:
        entry = base / name
        if should_exclude_managed_path(entry, is_dir=entry.is_dir()):
            ignored.add(name)
    return ignored


def copy_named_files_to_usb(
    source_dir: str,
    filenames: list[str],
    drive: str,
    log_fn: Callable[..., None] = print,
) -> bool:
    """把指定文件复制到 U 盘根目录，不创建程序文件夹。"""
    src = Path(source_dir)
    root = Path(drive)
    if not src.is_dir():
        log_fn(f"  复制失败: 源目录不存在 {source_dir}")
        return False
    if not root.is_dir():
        log_fn(f"  复制失败: U盘路径无效 {drive}")
        return False
    if not filenames:
        log_fn("  复制失败: 没有可复制的固件文件")
        return False
    try:
        for name in filenames:
            if not name or name != Path(name).name:
                log_fn(f"  复制失败: 非法文件名 {name}")
                return False
            if should_exclude_managed_path(name, is_dir=False):
                log_fn(f"  复制失败: 拒绝复制内部文件 {name}")
                return False
            file_path = src / name
            if not file_path.is_file():
                log_fn(f"  复制失败: 找不到 {name}")
                return False
            shutil.copy2(file_path, root / name)
            log_fn(f"  已复制: {name}")
        return True
    except Exception as e:
        log_fn(f"  复制失败: {e}")
        return False


def copy_directory_to_usb(
    source_dir: str, drive: str, log_fn: Callable[..., None] = print
) -> bool:
    """Copy one directory to USB root, excluding managed paths (D4.5).

    If target exists, replace it.
    """
    src = Path(source_dir)
    root = Path(drive)
    if not src.exists() or not src.is_dir():
        log_fn(f"  复制失败: 源目录不存在 {source_dir}")
        return False
    if not root.exists() or not root.is_dir():
        log_fn(f"  复制失败: U盘路径无效 {drive}")
        return False

    target = root / src.name
    try:
        if target.exists():
            rmtree_force(target)
            log_fn(f"  已移除旧目录: {target.name}")
        shutil.copytree(src, target, ignore=_ignore_managed_entries)
        log_fn(f"  已复制目录: {src.name}")
        return True
    except Exception as e:
        log_fn(f"  复制失败: {e}")
        return False


def eject_usb(drive: str, log_fn: Callable[..., None] = print) -> bool:
    """Safely eject USB using PowerShell."""
    letter = drive.rstrip("\\").rstrip("/")
    script = f"""
$vol = Get-WmiObject -Class Win32_Volume -Filter "DriveLetter='{letter}'"
$vol.DriveLetter = $null
$vol.Put()
(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{letter}').InvokeVerb('Eject')
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log_fn(f"  U盘已安全弹出: {drive}")
            return True
        log_fn(f"  弹出失败（可忽略，手动拔出即可）: {result.stderr.strip()}")
        return False
    except Exception as e:
        log_fn(f"  弹出异常: {e}")
        return False


def format_usb(drive: str, log_fn: Callable[..., None] = print) -> bool:
    """Format USB to FAT32 using PowerShell Format-Volume.

    Requires explicit user confirmation before calling.
    """
    letter = _extract_drive_letter(drive)
    # Issue 7: 盘符必须是单个英文字母，防止异常输入进入破坏性操作
    if not (len(letter) == 1 and letter.isascii() and letter.isalpha()):
        log_fn("  格式化异常: 无效盘符")
        return False

    # PowerShell Format-Volume 不需要交互输入，适合脚本化调用
    script = (
        f"Format-Volume -DriveLetter {letter} "
        f"-FileSystem FAT32 -NewFileSystemLabel '' "
        f"-Confirm:$false -Force"
    )
    try:
        log_fn(f"  正在格式化 {drive} (FAT32) ...")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "Format-Volume 失败").strip()
            log_fn(f"  格式化失败: {msg}")
            return False

        # Issue 6: 轮询驱动器就绪（最长 10 秒，每 0.5 秒检查一次）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(drive).is_dir():
                log_fn("  格式化完成")
                return True
            time.sleep(0.5)
        log_fn("  格式化完成，但驱动器未重新就绪（超时）")
        return False
    except (subprocess.TimeoutExpired, TimeoutError):
        log_fn("  格式化超时")
        return False
    except Exception as e:
        log_fn(f"  格式化异常: {e}")
        return False
