"""PostToolUse hook：编辑 .py 后跑 ruff check，失败时把结果回传给 Claude。"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    path = str(payload.get("tool_input", {}).get("file_path", ""))
    if not path.endswith(".py"):
        return 0
    result = subprocess.run(
        ["ruff", "check", "--output-format", "concise", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        return 0
    sys.stderr.write(result.stdout + result.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
