# OpsPilot D-drive environment init (V1 storage constraint)
#
# Usage (dot-source in project root PowerShell):
#     . .\scripts\init_d_env.ps1
#
# Sets process-scope env vars so temp/cache/pycache land under the D-drive
# project dir (.runtime / .cache). Does NOT modify system env. Child
# processes inherit. No C-drive writes.
$ErrorActionPreference = "Stop"
$script:OpsRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $OpsRoot ".runtime"
$CacheRoot   = Join-Path $OpsRoot ".cache"
$TmpDir      = Join-Path $RuntimeRoot "tmp"
$CkptDir     = Join-Path $RuntimeRoot "checkpoints"
foreach ($d in @($RuntimeRoot, $TmpDir, $CkptDir, $CacheRoot, (Join-Path $CacheRoot "pip"))) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
$env:OPSPILOT_RUNTIME_ROOT = $RuntimeRoot
$env:TEMP = $TmpDir
$env:TMP  = $TmpDir
$env:PYTEST_DEBUG_TEMPROOT = $TmpDir
$env:PIP_CACHE_DIR = Join-Path $CacheRoot "pip"
$env:PYTHONPYCACHEPREFIX = Join-Path $CacheRoot "pycache"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "[OpsPilot] D-drive env initialized (current process only):"
Write-Host ("  OPSPILOT_RUNTIME_ROOT = {0}" -f $RuntimeRoot)
Write-Host ("  TEMP/TMP              = {0}" -f $TmpDir)
Write-Host ("  PYTEST_DEBUG_TEMPROOT = {0}" -f $env:PYTEST_DEBUG_TEMPROOT)
Write-Host ("  PIP_CACHE_DIR         = {0}" -f $env:PIP_CACHE_DIR)
Write-Host ("  PYTHONPYCACHEPREFIX   = {0}" -f $env:PYTHONPYCACHEPREFIX)
Write-Host ("  PYTHONIOENCODING       = {0}" -f $env:PYTHONIOENCODING)
