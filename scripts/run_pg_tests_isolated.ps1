# OpsPilot     PostgreSQL            worker    database
#    .\scripts\run_pg_tests_isolated.ps1
#           database opspilot_test_a_<run> / opspilot_test_b_<run>    PostgreSQL
# Alembic upgrade head          PG
# DROP/CREATE          worker
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root "scripts\init_d_env.ps1")
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
        "    c.execute(text('CREATE DATABASE $name'))`n" +
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
$runToken = [guid]::NewGuid().ToString("N").Substring(0, 8)
foreach ($name in @("opspilot_test_a_$runToken", "opspilot_test_b_$runToken")) {
    New-IsolatedDb $name
    $db = $base + $name
    $env:DATABASE_URL = $db                    # alembic / 连接工具
    $env:OPSPILOT_TEST_DATABASE_URL = $db      # 破坏性 live 测试的唯一隔离目标（guard）
    $env:PYTHONIOENCODING = "utf-8"
    Write-Output "=====         $name ====="
    & $py -m pytest $tests -q -p no:cacheprovider
    Write-Output "[$name] exit=$LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}
if ($code -eq 0) { Write-Output "ISOLATED DOUBLE-RUN PASS                       " }
exit $code
