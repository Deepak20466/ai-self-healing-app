# Phase 1 bootstrap for Windows: create a venv, install dependencies, and
# create the PostgreSQL role/database used by every pod.
#
# Usage:
#   .\scripts\bootstrap.ps1
#   .\scripts\bootstrap.ps1 -PgSuperuser postgres -PgSuperPassword "..." -AppDbPassword "..."
#
# If -PgSuperPassword is omitted, database creation is skipped and this
# script only sets up the venv + .env; create the DB yourself and re-run
# with -SkipVenv to just run migrations.

param(
    [string]$PgHost = "localhost",
    [int]$PgPort = 5432,
    [string]$PgSuperuser = "postgres",
    [string]$PgSuperPassword = $env:PGSUPERPASSWORD,
    [string]$AppDbName = "selfheal",
    [string]$AppDbUser = "selfheal",
    [string]$AppDbPassword = $env:SELFHEAL_DB_PASSWORD,
    [switch]$SkipVenv,
    [switch]$SkipMigrate
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Find-Python311 {
    $candidates = @("python", "python3", "py -3.11")
    foreach ($cmd in $candidates) {
        try {
            $parts = $cmd.Split(" ")
            $version = & $parts[0] $parts[1..($parts.Length - 1)] --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $version -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return $cmd
            }
        } catch {
            continue
        }
    }
    throw "Python 3.11+ was not found on PATH. Install it from python.org and retry."
}

function Find-PsqlExe {
    $onPath = Get-Command psql -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $found = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }

    return $null
}

Write-Host "== Step 1/4: Python virtual environment ==" -ForegroundColor Cyan
if (-not $SkipVenv) {
    if (-not (Test-Path ".venv")) {
        $pythonCmd = Find-Python311
        Write-Host "Creating venv with '$pythonCmd' at .venv"
        $parts = $pythonCmd.Split(" ")
        & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
    } else {
        Write-Host "venv already exists at .venv, reusing it"
    }

    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip | Out-Host
    & $venvPython -m pip install -e ".[dev]" | Out-Host
} else {
    Write-Host "Skipped (-SkipVenv)"
}

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Write-Host "`n== Step 2/4: .env file ==" -ForegroundColor Cyan
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - fill in secrets before running the app."
} else {
    Write-Host ".env already exists, leaving it untouched"
}

Write-Host "`n== Step 3/4: PostgreSQL role and database ==" -ForegroundColor Cyan
if (-not $PgSuperPassword) {
    Write-Host "No -PgSuperPassword / `$env:PGSUPERPASSWORD given - skipping DB creation." -ForegroundColor Yellow
    Write-Host "Create it manually, e.g.:"
    Write-Host "  createuser -h $PgHost -p $PgPort -U $PgSuperuser -P $AppDbUser"
    Write-Host "  createdb   -h $PgHost -p $PgPort -U $PgSuperuser -O $AppDbUser $AppDbName"
    Write-Host "Then set DATABASE_URL in .env and re-run with -SkipVenv -SkipMigrate:`$false"
} else {
    $psql = Find-PsqlExe
    if (-not $psql) {
        throw "psql.exe not found. Install PostgreSQL client tools or add them to PATH."
    }

    if (-not $AppDbPassword) {
        Add-Type -AssemblyName System.Web
        $AppDbPassword = [System.Web.Security.Membership]::GeneratePassword(24, 4) -replace "[';\`"\\]", "x"
    }

    $env:PGPASSWORD = $PgSuperPassword
    $TestDbName = "${AppDbName}_test"
    try {
        $sql = @"
DO `$`$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$AppDbUser') THEN
      CREATE ROLE $AppDbUser LOGIN PASSWORD '$AppDbPassword';
   ELSE
      ALTER ROLE $AppDbUser WITH PASSWORD '$AppDbPassword';
   END IF;
END
`$`$;
-- Grants CREATEDB (not SUPERUSER) so the pytest suite can create and drop its
-- own throwaway "$TestDbName" database on every run without ever touching
-- Postgres superuser credentials again. See CLAUDE.md "Test database".
ALTER ROLE $AppDbUser CREATEDB;
"@
        $sql | & $psql -h $PgHost -p $PgPort -U $PgSuperuser -d postgres -v ON_ERROR_STOP=1 -q

        $dbExists = & $psql -h $PgHost -p $PgPort -U $PgSuperuser -d postgres -tAc `
            "SELECT 1 FROM pg_database WHERE datname = '$AppDbName'"
        if ($dbExists.Trim() -ne "1") {
            & $psql -h $PgHost -p $PgPort -U $PgSuperuser -d postgres -v ON_ERROR_STOP=1 -q -c `
                "CREATE DATABASE $AppDbName OWNER $AppDbUser"
        }

        $testDbExists = & $psql -h $PgHost -p $PgPort -U $PgSuperuser -d postgres -tAc `
            "SELECT 1 FROM pg_database WHERE datname = '$TestDbName'"
        if ($testDbExists.Trim() -ne "1") {
            & $psql -h $PgHost -p $PgPort -U $PgSuperuser -d postgres -v ON_ERROR_STOP=1 -q -c `
                "CREATE DATABASE $TestDbName OWNER $AppDbUser"
        }
        Write-Host "Role '$AppDbUser' and databases '$AppDbName' / '$TestDbName' are ready."
    } finally {
        Remove-Item Env:\PGPASSWORD
    }

    $databaseUrl = "postgresql+asyncpg://${AppDbUser}:${AppDbPassword}@${PgHost}:${PgPort}/${AppDbName}"
    $testDatabaseUrl = "postgresql+asyncpg://${AppDbUser}:${AppDbPassword}@${PgHost}:${PgPort}/${TestDbName}"
    $envContent = Get-Content ".env"
    if ($envContent -match "^DATABASE_URL=") {
        $envContent = $envContent -replace "^DATABASE_URL=.*", "DATABASE_URL=$databaseUrl"
    } else {
        $envContent += "DATABASE_URL=$databaseUrl"
    }
    if ($envContent -match "^TEST_DATABASE_URL=") {
        $envContent = $envContent -replace "^TEST_DATABASE_URL=.*", "TEST_DATABASE_URL=$testDatabaseUrl"
    } else {
        $envContent += "TEST_DATABASE_URL=$testDatabaseUrl"
    }
    Set-Content ".env" $envContent
    Write-Host "Wrote DATABASE_URL and TEST_DATABASE_URL into .env"
}

Write-Host "`n== Step 4/4: Alembic migrations ==" -ForegroundColor Cyan
if (-not $SkipMigrate) {
    & $venvPython -m alembic upgrade head
    Write-Host "Migrations applied." -ForegroundColor Green
} else {
    Write-Host "Skipped (-SkipMigrate)"
}

Write-Host "`nBootstrap complete." -ForegroundColor Green
