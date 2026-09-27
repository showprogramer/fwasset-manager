"""新建程序对话框的名称预填与来源推断。服务层不推断名称。"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

_METADATA_NAME = "程序信息.toml"


class PickedCreateSource(NamedTuple):
    kind: str
    source: str | None
    files: tuple[str, ...]


def classify_create_source(
    *,
    directory: str | None = None,
    files: list[str] | None = None,
) -> PickedCreateSource:
    """目录为 directory；单个 .zip 为 archive；其余已选文件为 files。取消则空。"""
    folder = (directory or "").strip()
    if folder:
        return PickedCreateSource("directory", folder, ())
    picked = [item for item in (files or []) if item.strip()]
    if len(picked) == 1 and Path(picked[0]).suffix.casefold() == ".zip":
        return PickedCreateSource("archive", picked[0], ())
    if picked:
        return PickedCreateSource("files", picked[0], tuple(picked))
    return PickedCreateSource("", None, ())


def describe_create_source(picked: PickedCreateSource) -> str:
    """来源只读文案。未选择时与对话框初值一致。"""
    if picked.kind == "directory" and picked.source:
        return f"已选择：文件夹 {picked.source}"
    if picked.kind == "archive" and picked.source:
        return f"已选择：{Path(picked.source).name}"
    if picked.kind == "files" and picked.files:
        return f"已选择：{len(picked.files)} 个文件"
    return "尚未选择来源"


def prefill_asset_name(
    *,
    source_kind: str,
    source: str | Path | None = None,
    files: list[str | Path] | None = None,
) -> str:
    """按来源预填程序名。zip 只去掉末尾一次 ``.zip``。"""
    if source_kind == "directory":
        return Path(source or "").name
    if source_kind == "archive":
        name = Path(source or "").name
        if name.casefold().endswith(".zip"):
            return name[:-4]
        return name
    paths = [Path(item) for item in (files or [])]
    roms = [item for item in paths if item.suffix.casefold() == ".rom"]
    if roms:
        return roms[0].stem
    for item in paths:
        if item.name != _METADATA_NAME:
            return item.stem
    return ""


def handcontrol_gap_hint(files: list[str | Path], asset_name: str) -> str:
    """手控散选缺 .rom 或 .pkg 时的不阻断提示。配对齐全则返回空串。"""
    label = asset_name.casefold()
    if "手控" not in asset_name and "handcontrol" not in label:
        return ""
    suffixes = {Path(item).suffix.casefold() for item in files}
    if ".rom" in suffixes and ".pkg" in suffixes:
        return ""
    return "手控程序通常需要同时有 .rom 和 .pkg。仍可保存。"
