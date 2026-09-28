"""请 Codex 只读审查当前任务差异。

用法：
    uv run python scripts/cross_review.py [--base HEAD] [--spec PATH] [--focus 文本] [--model 模型]

审查指令见 docs/review-prompt.md；结果写入系统临时目录并打印。
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "docs" / "review-prompt.md"
TIMEOUT_S = 30 * 60


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def build_prompt(base: str, spec: str, focus: str) -> str:
    stat = _git("diff", base, "--stat") or "（无已跟踪文件改动）"
    untracked = _git("ls-files", "--others", "--exclude-standard") or "（无）"
    parts = [
        PROMPT_FILE.read_text(encoding="utf-8"),
        "## 本次任务",
        f"查看差异：`git diff {base}`（已含暂存与未暂存改动）。",
        f"差异概览：\n```text\n{stat}\n```",
        f"未跟踪文件：\n```text\n{untracked}\n```",
    ]
    if spec:
        parts.append(f"规格：`{spec}`")
    if focus:
        parts.append(f"重点关注：{focus}")
    return "\n\n".join(parts) + "\n"


def codex_command(model: str, output: Path) -> list[str]:
    exe = shutil.which("codex")
    if exe is None:
        raise SystemExit("找不到 codex 命令，请先安装并登录。")
    cmd = [exe, "exec", "-s", "read-only", "--ephemeral", "--color", "never", "-C", str(ROOT)]
    cmd += ["-o", str(output)]
    if model:
        cmd += ["-m", model]
    return [*cmd, "-"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    parser.add_argument("--spec", default="", help="规格文件路径")
    parser.add_argument("--focus", default="", help="需要重点审查的点")
    parser.add_argument("--model", default="", help="Codex 模型，默认用 Codex 自己的设置")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK

    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = Path(tempfile.gettempdir()) / f"fwasset-review-{stamp}-codex.md"
    prompt = build_prompt(args.base, args.spec, args.focus)
    cmd = codex_command(args.model, output)
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )
    if result.returncode != 0 or not output.exists():
        sys.stderr.write(f"Codex 审查失败（exit {result.returncode}）：\n{result.stderr[-2000:]}\n")
        return 1
    print(f"审查结果：{output}\n")
    print(output.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
