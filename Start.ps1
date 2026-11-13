param([switch]$Worker, [switch]$NoDemo)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimeRoot = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível criar o ambiente Python.' }
    & $pythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível instalar as dependências.' }
}
$keyFile = Join-Path $runtimeRoot 'local-secret.txt'
if (-not (Test-Path -LiteralPath $keyFile)) {
    & $pythonExe -c 'import secrets; print(secrets.token_urlsafe(50))' | Set-Content -LiteralPath $keyFile -NoNewline
}
$env:SECRET_KEY = Get-Content -LiteralPath $keyFile -Raw
$env:DEBUG = '1'
$env:SQLITE_PATH = Join-Path $runtimeRoot 'local.sqlite3'
$env:ALLOWED_HOSTS = '127.0.0.1,localhost'
$env:DATABASE_URL = ''
$env:DATABASE_SCHEMA = ''
$localEnv = Join-Path $PSScriptRoot '.env.local'
if (Test-Path -LiteralPath $localEnv) {
    foreach ($line in (Get-Content -LiteralPath $localEnv)) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}
& $pythonExe manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Falha nas migrations.' }
if ($Worker) {
    & $pythonExe manage.py runworker
} else {
    if (-not $NoDemo) {
        & $pythonExe manage.py seed_demo
        if ($LASTEXITCODE -ne 0) { throw 'Falha na criação da demonstração.' }
    }
    Write-Host 'Abra http://127.0.0.1:8765. Para receber webhooks locais, execute o worker em outro terminal.'
    & $pythonExe manage.py runserver 127.0.0.1:8765 --noreload
}
