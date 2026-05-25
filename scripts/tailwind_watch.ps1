# Compila o Tailwind em modo watch.
# Rode num terminal separado enquanto desenvolve:
#     .\scripts\tailwind_watch.ps1
#
# Para build de produção:
#     .\scripts\tailwindcss.exe -i .\app\static\css\input.css -o .\app\static\css\tailwind.css --minify

$ErrorActionPreference = "Stop"
$tw = Join-Path $PSScriptRoot "tailwindcss.exe"
if (-not (Test-Path $tw)) {
    Write-Error "tailwindcss.exe nao encontrado. Rode primeiro: .\scripts\install_tailwind.ps1"
    exit 1
}
& $tw -i .\app\static\css\input.css -o .\app\static\css\tailwind.css --watch
