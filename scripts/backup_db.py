"""Backup do MySQL via mysqldump + gzip + rotação.

Uso:
    python scripts\\backup_db.py                                # default ./backups, retenção 30d
    python scripts\\backup_db.py --output D:\\backups
    python scripts\\backup_db.py --retention-days 14
    python scripts\\backup_db.py --keep-all                     # sem rotação

Lê `DATABASE_URL` do `.env` ou do ambiente. Precisa de `mysqldump`
disponível no PATH.

Agendamento sugerido (todo dia 2h da manhã):
    Linux/cron:       0 2 * * * /path/python /path/scripts/backup_db.py
    Windows/Task Sch: agendar python.exe com argumentos.

Estratégia de dump:
    --single-transaction (consistência em InnoDB sem lock de tabela)
    --routines + --triggers (objetos completos)
    --default-character-set=utf8mb4
    Senha vai via env MYSQL_PWD pra não vazar via lista de processos.

NO-TENANT-FILTER: backup do banco inteiro, fora do isolamento por tenant.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


def parse_database_url(url: str) -> dict:
    """Extrai host/porta/user/senha/db de DATABASE_URL.

    Aceita prefixos `mysql://` ou `mysql+pymysql://` (SQLAlchemy).
    Decodifica URL-encoded na senha (caso tenha caracteres especiais).
    """
    normalized = url.replace("mysql+pymysql://", "mysql://", 1)
    parsed = urlparse(normalized)
    db = parsed.path.lstrip("/") if parsed.path else ""
    return {
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "database": db,
    }


def rotate(output_dir: Path, db_name: str, retention_days: int) -> int:
    """Remove backups {db_name}-*.sql.gz mais antigos que `retention_days`
    dias. Retorna número de arquivos removidos.

    Usa mtime do arquivo — não confia no nome (timezone, drift de relógio).
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    pattern = f"{db_name}-*.sql.gz"
    removed = 0
    for f in output_dir.glob(pattern):
        if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
    return removed


def _build_mysqldump_cmd(cfg: dict) -> list[str]:
    """Constrói a lista de args do mysqldump (sem senha — vai por env)."""
    return [
        "mysqldump",
        f"--host={cfg['host']}",
        f"--port={cfg['port']}",
        f"--user={cfg['user']}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--default-character-set=utf8mb4",
        cfg["database"],
    ]


def backup(
    *,
    output_dir: str | Path,
    retention_days: int,
    keep_all: bool,
    database_url: str | None = None,
    now: datetime.datetime | None = None,
) -> int:
    """Roda o dump. Retorna exit code (0 = sucesso)."""
    if database_url is None:
        database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERRO: DATABASE_URL não definida (.env ou ambiente).", file=sys.stderr)
        return 2

    cfg = parse_database_url(database_url)
    if not cfg["database"]:
        print("ERRO: DATABASE_URL sem nome de database no path.", file=sys.stderr)
        return 2
    if not cfg["user"]:
        print("ERRO: DATABASE_URL sem usuário.", file=sys.stderr)
        return 2

    if not shutil.which("mysqldump"):
        print("ERRO: mysqldump não encontrado no PATH. Instale mysql-client.",
              file=sys.stderr)
        return 3

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    out_file = output_dir / f"{cfg['database']}-{ts}.sql.gz"

    env = os.environ.copy()
    if cfg["password"]:
        # MYSQL_PWD evita expor senha em `ps aux` / Task Manager.
        env["MYSQL_PWD"] = cfg["password"]

    cmd = _build_mysqldump_cmd(cfg)
    print(f"Dumping {cfg['database']}@{cfg['host']} → {out_file}")

    # Roda mysqldump e pipe stdout direto pro gzip (sem buffer cheio em memória).
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "(sem stderr)"
        print(f"ERRO mysqldump (exit {proc.returncode}):\n{stderr}", file=sys.stderr)
        return proc.returncode

    with gzip.open(out_file, "wb") as fout:
        fout.write(proc.stdout)

    size_mb = out_file.stat().st_size / (1024 * 1024)
    print(f"OK — {out_file.name} ({size_mb:.2f} MB)")

    if not keep_all:
        removed = rotate(output_dir, cfg["database"], retention_days)
        if removed:
            print(f"Rotacionados: {removed} arquivo(s) antigos (>{retention_days}d).")
        else:
            print(f"Sem rotação: nenhum arquivo >{retention_days}d.")

    return 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output", default="backups",
                   help="Diretório de saída (default: ./backups).")
    p.add_argument("--retention-days", type=int, default=30,
                   help="Dias de retenção (default: 30).")
    p.add_argument("--keep-all", action="store_true",
                   help="Não rotacionar — útil pra arquivamento manual.")
    p.add_argument("--database-url",
                   help="Override DATABASE_URL (default: env).")
    args = p.parse_args()

    return backup(
        output_dir=args.output,
        retention_days=args.retention_days,
        keep_all=args.keep_all,
        database_url=args.database_url,
    )


if __name__ == "__main__":
    sys.exit(main())
