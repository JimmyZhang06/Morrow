[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$releaseRoot = Split-Path -Parent $PSScriptRoot
$releasePython = Join-Path $releaseRoot '.venv/Scripts/python.exe'
Push-Location $releaseRoot
try {
    & $releasePython scripts/prepare_postgres_runtime.py
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL runtime verification failed.' }
    $migrationData = (Resolve-Path alembic).Path + ':alembic'
    $seedData = (Resolve-Path scripts/seed_local.py).Path + ':scripts'
    & $releasePython -m PyInstaller --noconfirm --onedir --name vistora-backend `
        --distpath .data/release-runtime --workpath .data/pyinstaller-work --specpath .data `
        --paths src --collect-submodules life_coach --collect-submodules asyncpg `
        --collect-submodules uvicorn --add-data $migrationData --add-data $seedData scripts/desktop_backend.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend runtime build failed.' }
    & $releasePython scripts/collect_runtime_notices.py
    if ($LASTEXITCODE -ne 0) { throw 'Runtime notices collection failed.' }
} finally { Pop-Location }
