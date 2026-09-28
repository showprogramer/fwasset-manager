$ErrorActionPreference = "Stop"

uv run ruff check src scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
