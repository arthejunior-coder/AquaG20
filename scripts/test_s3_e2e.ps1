# Smoke test end-to-end do off-site backup S3.
#
# O que faz, em ordem:
#   1) Verifica que .env tem S3_BACKUP_BUCKET preenchido
#   2) Cria um arquivo fake em backups/ (não toca seu MySQL)
#   3) Roda dry-run — deve listar o arquivo fake como "would upload"
#   4) Roda upload real — deve enviar o arquivo
#   5) Roda de novo — deve fazer skip (idempotente)
#   6) Lista o bucket via boto3 pra confirmar que está lá
#   7) Apaga o arquivo fake local e remoto
#
# Uso:
#   .\scripts\test_s3_e2e.ps1
#
# Pré-requisitos: .env preenchido + credenciais AWS válidas.

$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "ERRO: venv não encontrada em .venv\Scripts\python.exe" -ForegroundColor Red
    exit 1
}

# ---- 1) Sanidade do .env ---------------------------------------------------
Write-Host "==> 1/7  Checando .env" -ForegroundColor Cyan
$envCheck = & $python -c @"
from dotenv import load_dotenv
import os, sys
load_dotenv()
bucket = os.environ.get('S3_BACKUP_BUCKET')
key = os.environ.get('AWS_ACCESS_KEY_ID')
if not bucket:
    print('FALTA: S3_BACKUP_BUCKET'); sys.exit(1)
if not key:
    print('FALTA: AWS_ACCESS_KEY_ID'); sys.exit(1)
print(f'bucket={bucket}')
print(f'access_key={key[:6]}...')
"@
if ($LASTEXITCODE -ne 0) {
    Write-Host "Edite .env e tente de novo." -ForegroundColor Red
    exit 1
}
Write-Host $envCheck -ForegroundColor Green

# ---- 2) Cria arquivo fake em backups/ --------------------------------------
Write-Host "`n==> 2/7  Criando arquivo fake em backups\" -ForegroundColor Cyan
$backupsDir = ".\backups"
if (-not (Test-Path $backupsDir)) { New-Item -ItemType Directory -Path $backupsDir | Out-Null }
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$fakeName = "aquag20-s3test-$ts.sql.gz"
$fakePath = Join-Path $backupsDir $fakeName

# Gera um .sql.gz pequeno (não é dump real — só pra exercitar o sync)
& $python -c @"
import gzip
with gzip.open(r'$fakePath', 'wb') as f:
    f.write(b'-- AquaG20 S3 sync smoke test\n-- Safe to delete\n')
"@
Write-Host "  $fakePath criado." -ForegroundColor Green

# ---- 3) Dry-run ------------------------------------------------------------
Write-Host "`n==> 3/7  Dry-run (não envia, só lista)" -ForegroundColor Cyan
& $python scripts\sync_backups_s3.py --dry-run
if ($LASTEXITCODE -ne 0) { Write-Host "Dry-run falhou." -ForegroundColor Red; exit 1 }

# ---- 4) Upload real --------------------------------------------------------
Write-Host "`n==> 4/7  Upload real" -ForegroundColor Cyan
& $python scripts\sync_backups_s3.py
if ($LASTEXITCODE -ne 0) { Write-Host "Upload falhou." -ForegroundColor Red; exit 1 }

# ---- 5) Segundo run — deve ser idempotente ---------------------------------
Write-Host "`n==> 5/7  Rodando de novo (deve fazer skip — idempotente)" -ForegroundColor Cyan
$out = & $python scripts\sync_backups_s3.py 2>&1 | Out-String
Write-Host $out
if ($out -notmatch "0 enviado") {
    Write-Host "AVISO: esperava 0 uploads no segundo run. Verifique o output." -ForegroundColor Yellow
}

# ---- 6) Confirma no bucket via boto3 ---------------------------------------
Write-Host "`n==> 6/7  Confirmando no bucket via boto3.list_objects_v2" -ForegroundColor Cyan
& $python -c @"
from dotenv import load_dotenv
load_dotenv()
import os, boto3
# Respeita S3_BACKUP_ENDPOINT_URL pra suportar B2/OCI/R2 etc.
kwargs = {}
endpoint = os.environ.get('S3_BACKUP_ENDPOINT_URL')
if endpoint:
    kwargs['endpoint_url'] = endpoint
client = boto3.client('s3', **kwargs)
bucket = os.environ['S3_BACKUP_BUCKET']
prefix = os.environ.get('S3_BACKUP_PREFIX', 'aquag20-backups/')
resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix + '$fakeName')
items = resp.get('Contents', [])
if not items:
    print('NAO ENCONTRADO no bucket — sync silenciosamente nao subiu.')
    raise SystemExit(1)
for obj in items:
    # StorageClass nem sempre vem (R2/B2 podem omitir) — usa .get com fallback
    sc = obj.get('StorageClass', '-')
    print(f\"  s3://{bucket}/{obj['Key']}  ({obj['Size']} bytes, {sc})\")
"@
if ($LASTEXITCODE -ne 0) { Write-Host "Verificacao falhou." -ForegroundColor Red; exit 1 }

# ---- 7) Cleanup ------------------------------------------------------------
Write-Host "`n==> 7/7  Cleanup (apaga local + remoto)" -ForegroundColor Cyan
& $python -c @"
from dotenv import load_dotenv
load_dotenv()
import os, boto3
kwargs = {}
endpoint = os.environ.get('S3_BACKUP_ENDPOINT_URL')
if endpoint:
    kwargs['endpoint_url'] = endpoint
client = boto3.client('s3', **kwargs)
bucket = os.environ['S3_BACKUP_BUCKET']
prefix = os.environ.get('S3_BACKUP_PREFIX', 'aquag20-backups/')
client.delete_object(Bucket=bucket, Key=prefix + '$fakeName')
print(f'  removido do bucket: {prefix}$fakeName')
"@
Remove-Item $fakePath
Write-Host "  removido local: $fakePath" -ForegroundColor Green

Write-Host "`n==> Tudo OK. Off-site backup S3 esta funcional." -ForegroundColor Green
Write-Host "Proximo passo: agendar no Task Scheduler (instrucoes no README, secao 'Off-site backup')." -ForegroundColor Cyan
