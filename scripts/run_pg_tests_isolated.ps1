# OpsPilot     PostgreSQL            worker    database
#    .\scripts\run_pg_tests_isolated.ps1
#           database opspilot_p4a / opspilot_p4b    PostgreSQL
# Alembic upgrade head          PG
# DROP/CREATE          worker
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$base = $env:PGP4_URL
if (-not $base) { $base = "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/" }

function Invoke-Py([string]$code, [string]$url) {
    $env:DATABASE_URL = $url
    & $py -c $code
    if ($LASTEXITCODE -ne 0) { throw "python    exit=$LASTEXITCODE" }
}

function New-IsolatedDb([string]$name) {
    $admin = $base + "postgres"
    $mk = "import os`nfrom sqlalchemy import create_engine,text`n" +
        "e=create_engine(os.environ['DATABASE_URL'],isolation_level='AUTOCOMMIT')`n" +
        "with e.connect() as c:`n" +
        "    try: c.execute(text('CREATE DATABASE $name'))`n" +
        "    except Exception: pass`n" +
        "e.dispose()"
    Invoke-Py $mk $admin
    $db = $base + $name
    Invoke-Py "pass" $db | Out-Null
    $env:DATABASE_URL = $db
    & $py -m alembic -c (Join-Path $root "alembic.ini") upgrade head | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed on $name" }
    Write-Output "ready: $name (schema at alembic head)"
}

$tests = @(
    "tests/integration/test_schema_0004_live.py",
    "tests/integration/test_postgres_repository_live.py",
    "tests/integration/test_pg_commands_live.py"
)
$code = 0
foreach ($name in @("opspilot_p4a", "opspilot_p4b")) {
    New-IsolatedDb $name
    $env:DATABASE_URL = $base + $name
    $env:PYTHONIOENCODING = "utf-8"
    Write-Output "=====         $name ====="
    & $py -m pytest $tests -q -p no:cacheprovider
    Write-Output "[$name] exit=$LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}
if ($code -eq 0) { Write-Output "ISOLATED DOUBLE-RUN PASS                       " }
exit $code
