"""Sync de backups locais (.sql.gz) pra um bucket S3-compatível — off-site backup.

Compatível com qualquer provider S3-API: AWS S3, Backblaze B2, Oracle OCI,
Cloudflare R2, Wasabi, MinIO. Basta setar S3_BACKUP_ENDPOINT_URL apontando
pro endpoint do provider (vazio = AWS S3 nativo).

Uso:
    python scripts\\sync_backups_s3.py                                # default ./backups → S3
    python scripts\\sync_backups_s3.py --source D:\\backups
    python scripts\\sync_backups_s3.py --dry-run                      # mostra sem enviar
    python scripts\\sync_backups_s3.py --s3-retention-days 365        # rotaciona no bucket

Config via env (ou .env):
    S3_BACKUP_BUCKET            obrigatório — nome do bucket
    S3_BACKUP_PREFIX            opcional, default "aquag20-backups/"
    S3_BACKUP_STORAGE_CLASS     opcional, default "STANDARD"
                                (AWS: STANDARD_IA/GLACIER_IR pra economizar.
                                Vazio "" pula o param — útil pra B2/R2 que
                                não suportam o conceito ou erram com nomes AWS)
    S3_BACKUP_ENDPOINT_URL      opcional — URL do provider S3-compat.
                                Vazio = AWS. Exemplos:
                                  B2:  https://s3.us-west-002.backblazeb2.com
                                  OCI: https://<ns>.compat.objectstorage.<region>.oraclecloud.com
                                  R2:  https://<acct>.r2.cloudflarestorage.com
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
                                credenciais boto3 padrão. Pra B2/OCI/R2,
                                use as keys equivalentes do provider
                                (mesmas envs, mesma API).

Idempotente: pula arquivos cujo nome já existe no bucket (não sobrescreve).
Estratégia segura — backups locais são imutáveis (timestamp no nome).
Server-side encryption AES256 ativada por padrão (AWS/B2; R2 ignora silenciosamente).

Agendamento sugerido (logo após backup_db.py, 2h05):
    Linux/cron:  5 2 * * * /path/python /path/scripts/sync_backups_s3.py

NO-TENANT-FILTER: opera em arquivos de backup, fora do escopo de tenant.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# Windows console default é CP1252/CP850, que quebra em chars como →, —, etc.
# Força UTF-8 no stdout/stderr pra evitar UnicodeEncodeError em prints.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_DEFAULT_PREFIX = "aquag20-backups/"
_DEFAULT_STORAGE_CLASS = "STANDARD"


def _normalize_prefix(prefix: str) -> str:
    """Garante que o prefix termina com / (S3 usa / como separador convencional)."""
    if not prefix:
        return ""
    return prefix if prefix.endswith("/") else prefix + "/"


def list_local_backups(source_dir: Path) -> list[Path]:
    """Lista todos os .sql.gz no diretório local, ordenados por nome."""
    if not source_dir.is_dir():
        return []
    return sorted(source_dir.glob("*.sql.gz"))


def list_remote_keys(client, bucket: str, prefix: str) -> set[str]:
    """Retorna set de keys (filename only) presentes no bucket sob o prefix."""
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            full_key = obj["Key"]
            # Reduz pra basename — comparamos contra os arquivos locais
            filename = full_key[len(prefix):] if full_key.startswith(prefix) else full_key
            if filename:  # ignora "diretório" vazio
                keys.add(filename)
    return keys


def upload_file(
    client, local_path: Path, bucket: str, key: str, storage_class: str,
) -> None:
    """Faz upload com SSE AES256 + storage class configurada.

    Se `storage_class` for vazio (""), pula o param — compatibilidade com
    providers que não suportam StorageClass ou usam nomes diferentes (B2, R2).
    """
    extra_args = {
        "ServerSideEncryption": "AES256",
        "ContentType": "application/gzip",
    }
    if storage_class:
        extra_args["StorageClass"] = storage_class
    client.upload_file(str(local_path), bucket, key, ExtraArgs=extra_args)


def rotate_remote(
    client, bucket: str, prefix: str, retention_days: int,
    now: datetime.datetime | None = None,
) -> int:
    """Apaga objetos do bucket mais antigos que `retention_days` (LastModified
    do S3, não confia no nome). Retorna número de objetos removidos.

    Roda só se o usuário passar --s3-retention-days explicitamente — sem flag,
    backups acumulam off-site indefinidamente (estratégia segura por default).
    """
    cutoff = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=retention_days)
    to_delete: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            last_modified = obj["LastModified"]
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=datetime.timezone.utc)
            if last_modified < cutoff:
                to_delete.append({"Key": obj["Key"]})

    if not to_delete:
        return 0

    # delete_objects aceita no máximo 1000 keys por chamada
    removed = 0
    for i in range(0, len(to_delete), 1000):
        batch = to_delete[i:i + 1000]
        client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        removed += len(batch)
    return removed


def sync(
    *,
    source_dir: str | Path,
    bucket: str | None = None,
    prefix: str | None = None,
    storage_class: str | None = None,
    endpoint_url: str | None = None,
    s3_retention_days: int | None = None,
    dry_run: bool = False,
    client=None,
) -> int:
    """Sync principal. Retorna exit code (0 = sucesso)."""
    bucket = bucket or os.environ.get("S3_BACKUP_BUCKET")
    if not bucket:
        print("ERRO: S3_BACKUP_BUCKET não definida (.env ou ambiente).", file=sys.stderr)
        return 2

    prefix = _normalize_prefix(
        prefix if prefix is not None else os.environ.get("S3_BACKUP_PREFIX", _DEFAULT_PREFIX)
    )
    # storage_class pode ser "" (string vazia) pra desabilitar — usar getenv
    # direto preserva "" diferente de None.
    if storage_class is None:
        storage_class = os.environ.get("S3_BACKUP_STORAGE_CLASS", _DEFAULT_STORAGE_CLASS)

    endpoint_url = endpoint_url or os.environ.get("S3_BACKUP_ENDPOINT_URL") or None

    source_dir = Path(source_dir)
    locals_ = list_local_backups(source_dir)
    if not locals_:
        print(f"Nenhum .sql.gz em {source_dir} — nada a fazer.")
        return 0

    if client is None:
        try:
            import boto3
        except ImportError:
            print("ERRO: boto3 não instalado. Rode: pip install boto3", file=sys.stderr)
            return 3
        client_kwargs = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        client = boto3.client("s3", **client_kwargs)

    sc_label = storage_class or "(omitido)"
    provider = endpoint_url or "AWS S3"
    print(f"Provider: {provider}")
    print(f"Bucket:   s3://{bucket}/{prefix}  (storage class: {sc_label})")
    print(f"Source:   {source_dir}  ({len(locals_)} arquivo(s) local)")

    remote_keys = list_remote_keys(client, bucket, prefix)
    print(f"Remote: {len(remote_keys)} arquivo(s) já no bucket.")

    uploaded = 0
    skipped = 0
    for local in locals_:
        if local.name in remote_keys:
            skipped += 1
            continue
        key = prefix + local.name
        size_mb = local.stat().st_size / (1024 * 1024)
        if dry_run:
            print(f"  [DRY-RUN] would upload {local.name} ({size_mb:.2f} MB)")
        else:
            print(f"  Upload {local.name} ({size_mb:.2f} MB) → {key}")
            upload_file(client, local, bucket, key, storage_class)
        uploaded += 1

    print(f"OK — {uploaded} enviado(s), {skipped} já presente(s).")

    if s3_retention_days is not None and not dry_run:
        removed = rotate_remote(client, bucket, prefix, s3_retention_days)
        if removed:
            print(f"Rotação S3: {removed} objeto(s) removido(s) (>{s3_retention_days}d).")
        else:
            print(f"Rotação S3: nenhum objeto >{s3_retention_days}d.")

    return 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default="backups",
                   help="Diretório local com .sql.gz (default: ./backups).")
    p.add_argument("--bucket", help="Override S3_BACKUP_BUCKET.")
    p.add_argument("--prefix", help="Override S3_BACKUP_PREFIX (default 'aquag20-backups/').")
    p.add_argument("--storage-class",
                   help="Override S3_BACKUP_STORAGE_CLASS (STANDARD/STANDARD_IA/GLACIER_IR/etc). "
                        "Passe string vazia pra desabilitar em providers que não suportam.")
    p.add_argument("--endpoint-url",
                   help="Override S3_BACKUP_ENDPOINT_URL — URL do provider S3-compat "
                        "(B2/OCI/R2/Wasabi/MinIO). Sem flag = AWS S3.")
    p.add_argument("--s3-retention-days", type=int,
                   help="Se passado, apaga objetos no bucket mais antigos que N dias. "
                        "Sem flag, backups acumulam indefinidamente (default seguro).")
    p.add_argument("--dry-run", action="store_true",
                   help="Lista o que seria enviado/rotacionado, sem alterar nada.")
    args = p.parse_args()

    return sync(
        source_dir=args.source,
        bucket=args.bucket,
        prefix=args.prefix,
        storage_class=args.storage_class,
        endpoint_url=args.endpoint_url,
        s3_retention_days=args.s3_retention_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
