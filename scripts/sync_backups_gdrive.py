"""Sync de backups locais (.sql.gz) pra uma pasta do Google Drive — off-site backup.

Alternativa gratuita ao S3 (15 GB no plano free). Mesma interface CLI do
sync_backups_s3.py: idempotente por filename, dry-run, retention opcional.

Uso:
    python scripts\\sync_backups_gdrive.py                                # default ./backups
    python scripts\\sync_backups_gdrive.py --source D:\\backups
    python scripts\\sync_backups_gdrive.py --dry-run                      # mostra sem enviar
    python scripts\\sync_backups_gdrive.py --gdrive-retention-days 365    # rotaciona

Setup (uma vez):
    1) Google Cloud Console → New Project → Enable "Google Drive API"
    2) APIs & Services → Credentials → Create OAuth client ID → Desktop app
    3) Download JSON → salve como gdrive_credentials.json na raiz do projeto
    4) No Google Drive (web), crie uma pasta "AquaG20 Backups" e copie
       o FOLDER_ID da URL (drive.google.com/drive/folders/<ESTE_ID>)
    5) Preencha no .env: GDRIVE_FOLDER_ID=<id_da_pasta>
    6) Rode `python scripts\\sync_backups_gdrive.py` 1x INTERATIVO:
       abre o browser, você autoriza, gera gdrive_token.json.
       Depois disso roda headless no Task Scheduler.

Config via env (ou .env):
    GDRIVE_FOLDER_ID            obrigatório — ID da pasta no Drive
    GDRIVE_CLIENT_SECRETS       opcional, default "gdrive_credentials.json"
    GDRIVE_TOKEN_FILE           opcional, default "gdrive_token.json"

Scope `drive.file` — esta app só vê arquivos que ELA criou. Não enxerga
seus outros arquivos. Princípio do menor privilégio.

NO-TENANT-FILTER: opera em arquivos de backup, fora do escopo de tenant.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path


_DEFAULT_CLIENT_SECRETS = "gdrive_credentials.json"
_DEFAULT_TOKEN_FILE = "gdrive_token.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_service(client_secrets_path: str, token_path: str):
    """Carrega credenciais; abre browser na 1ª vez, refresh transparente depois.

    Imports localizados pra não exigir google-* em dev quem não usa GDrive.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, _SCOPES)
            # port=0 escolhe porta livre; abre browser local
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    # cache_discovery=False: evita warning chato em Python 3.12+
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_local_backups(source_dir: Path) -> list[Path]:
    """Lista todos os .sql.gz no diretório local, ordenados por nome."""
    if not source_dir.is_dir():
        return []
    return sorted(source_dir.glob("*.sql.gz"))


def list_remote_files(service, folder_id: str) -> list[dict]:
    """Lista files (não-trashed) na pasta. Pagina automaticamente.

    Retorna lista de dicts com id, name, createdTime, size.
    """
    files: list[dict] = []
    page_token: str | None = None
    q = f"'{folder_id}' in parents and trashed=false"
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, createdTime, size)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def upload_file(service, local_path: Path, folder_id: str) -> dict:
    """Sobe um arquivo pra pasta. Resumable upload (sobrevive a glitches de rede)."""
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(local_path), mimetype="application/gzip", resumable=True)
    body = {"name": local_path.name, "parents": [folder_id]}
    return service.files().create(body=body, media_body=media, fields="id, name").execute()


def rotate_remote(
    service, folder_id: str, retention_days: int,
    now: datetime.datetime | None = None,
) -> int:
    """Apaga arquivos da pasta mais antigos que `retention_days` (pelo
    createdTime do Drive, não pelo nome). Retorna número removidos.
    """
    cutoff = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=retention_days)
    files = list_remote_files(service, folder_id)
    removed = 0
    for f in files:
        # createdTime vem como ISO 8601 com 'Z' (UTC)
        created_str = f["createdTime"].replace("Z", "+00:00")
        created = datetime.datetime.fromisoformat(created_str)
        if created < cutoff:
            service.files().delete(fileId=f["id"]).execute()
            removed += 1
    return removed


def sync(
    *,
    source_dir: str | Path,
    folder_id: str | None = None,
    client_secrets_path: str | None = None,
    token_path: str | None = None,
    retention_days: int | None = None,
    dry_run: bool = False,
    service=None,
) -> int:
    """Sync principal. Retorna exit code (0 = sucesso)."""
    folder_id = folder_id or os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        print("ERRO: GDRIVE_FOLDER_ID não definido (.env ou ambiente).", file=sys.stderr)
        return 2

    source_dir = Path(source_dir)
    locals_ = list_local_backups(source_dir)
    if not locals_:
        print(f"Nenhum .sql.gz em {source_dir} — nada a fazer.")
        return 0

    if service is None:
        client_secrets_path = client_secrets_path or os.environ.get(
            "GDRIVE_CLIENT_SECRETS", _DEFAULT_CLIENT_SECRETS
        )
        token_path = token_path or os.environ.get(
            "GDRIVE_TOKEN_FILE", _DEFAULT_TOKEN_FILE
        )
        if not os.path.exists(client_secrets_path):
            print(
                f"ERRO: {client_secrets_path} não encontrado. "
                f"Baixe do Google Cloud Console (Credentials → Desktop client → JSON).",
                file=sys.stderr,
            )
            return 3
        try:
            service = _get_service(client_secrets_path, token_path)
        except ImportError:
            print(
                "ERRO: google-api-python-client / google-auth-oauthlib não instalados. "
                "Rode: pip install google-api-python-client google-auth-oauthlib",
                file=sys.stderr,
            )
            return 3

    print(f"Folder ID: {folder_id}")
    print(f"Source:    {source_dir}  ({len(locals_)} arquivo(s) local)")

    remote = list_remote_files(service, folder_id)
    remote_names = {f["name"] for f in remote}
    print(f"Remote:    {len(remote)} arquivo(s) já na pasta.")

    uploaded = 0
    skipped = 0
    for local in locals_:
        if local.name in remote_names:
            skipped += 1
            continue
        size_mb = local.stat().st_size / (1024 * 1024)
        if dry_run:
            print(f"  [DRY-RUN] would upload {local.name} ({size_mb:.2f} MB)")
        else:
            print(f"  Upload {local.name} ({size_mb:.2f} MB)")
            upload_file(service, local, folder_id)
        uploaded += 1

    print(f"OK — {uploaded} enviado(s), {skipped} já presente(s).")

    if retention_days is not None and not dry_run:
        removed = rotate_remote(service, folder_id, retention_days)
        if removed:
            print(f"Rotação Drive: {removed} arquivo(s) removido(s) (>{retention_days}d).")
        else:
            print(f"Rotação Drive: nenhum arquivo >{retention_days}d.")

    return 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", default="backups",
                   help="Diretório local com .sql.gz (default: ./backups).")
    p.add_argument("--folder-id", help="Override GDRIVE_FOLDER_ID.")
    p.add_argument("--client-secrets",
                   help="Override caminho do gdrive_credentials.json.")
    p.add_argument("--token-file",
                   help="Override caminho do gdrive_token.json.")
    p.add_argument("--gdrive-retention-days", type=int,
                   help="Se passado, apaga arquivos na pasta mais antigos que N dias. "
                        "Sem flag, backups acumulam indefinidamente (default seguro).")
    p.add_argument("--dry-run", action="store_true",
                   help="Lista o que seria enviado/rotacionado, sem alterar nada.")
    args = p.parse_args()

    return sync(
        source_dir=args.source,
        folder_id=args.folder_id,
        client_secrets_path=args.client_secrets,
        token_path=args.token_file,
        retention_days=args.gdrive_retention_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
