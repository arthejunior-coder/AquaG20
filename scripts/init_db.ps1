# Inicializa os databases do AquaG20 a partir do schema oficial.
# Uso:
#   .\scripts\init_db.ps1
#   .\scripts\init_db.ps1 -DbUser aquag20 -DbName aquag20
#
# Pressupõe que o usuário MySQL já existe (ver README, seção "Inicialização do banco").

param(
    [string]$DbName     = "aquag20",
    [string]$TestDbName = "aquag20_test",
    [string]$DbUser     = "aquag20",
    [string]$RootUser   = "root"
)

$ErrorActionPreference = "Stop"

$schemaPath = Join-Path $PSScriptRoot "..\files\schema_aguaG20.sql"
if (-not (Test-Path $schemaPath)) {
    Write-Error "Schema nao encontrado em $schemaPath"
    exit 1
}

Write-Host "==> Criando databases $DbName e $TestDbName (precisa senha do $RootUser)..."
$createSql = @"
CREATE DATABASE IF NOT EXISTS $DbName     CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS $TestDbName CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"@
$createSql | mysql -u $RootUser -p

Write-Host "==> Importando schema em $DbName (precisa senha do $DbUser)..."
Get-Content $schemaPath -Raw | mysql -u $DbUser -p $DbName

Write-Host "==> Importando schema em $TestDbName (precisa senha do $DbUser)..."
Get-Content $schemaPath -Raw | mysql -u $DbUser -p $TestDbName

Write-Host ""
Write-Host "OK. Proximo passo:"
Write-Host "    flask db init"
Write-Host "    flask db stamp head"
