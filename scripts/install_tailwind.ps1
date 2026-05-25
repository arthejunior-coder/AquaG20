# Baixa o Tailwind CSS standalone (sem precisar de Node.js).
# Rode uma vez por checkout:
#     .\scripts\install_tailwind.ps1
#
# Usa Tailwind v3.4.17 — o v4 standalone tem incompatibilidades com CPUs
# Intel anteriores ao Skylake (instruções AVX exigidas pelo Bun bundled).

param(
    [string]$Version = "v3.4.17"
)

$ErrorActionPreference = "Stop"
$dest = Join-Path $PSScriptRoot "tailwindcss.exe"

if (Test-Path $dest) {
    Write-Host "Ja existe: $dest"
    & $dest --help | Select-Object -First 1
    Write-Host "Para reinstalar, apague o arquivo antes."
    return
}

$url = "https://github.com/tailwindlabs/tailwindcss/releases/download/$Version/tailwindcss-windows-x64.exe"
Write-Host "Baixando $Version de $url ..."
Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
Unblock-File -Path $dest

$sizeMb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
Write-Host "OK ($sizeMb MB)"
& $dest --help | Select-Object -First 1
