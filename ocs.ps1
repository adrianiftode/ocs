#!/usr/bin/env pwsh
# ocs launcher for Windows (PowerShell)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Get-Command python -ErrorAction SilentlyContinue) {
  $py = "python"
} else {
  $py = "py"
}
& $py (Join-Path $dir "ocs.py") @args
exit $LASTEXITCODE
