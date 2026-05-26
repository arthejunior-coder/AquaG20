"""Testes do scripts/sync_backups_gdrive.py — sem chamar Google Drive real.

Cobre:
  - Helpers: list_local_backups, list_remote_files (single-page)
  - sync(): config faltando, dry-run, idempotência, upload
  - rotate_remote: cutoff por createdTime, formato ISO 8601 com Z

Usa um FakeDriveService que mimica a chain `service.files().X(...).execute()`.
Sem google-api-python-client real, sem rede.
"""

from __future__ import annotations

import datetime
import gzip
import re
from pathlib import Path

import pytest

from scripts.sync_backups_gdrive import (
    list_local_backups,
    list_remote_files,
    rotate_remote,
    sync,
)


# ---------------------------------------------------------------------------
# Fake Drive client — mimica a chain `service.files().list(...).execute()`
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime.datetime) -> str:
    """Formata como o Drive devolve: '2026-05-26T14:30:00.000Z'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class _FakeRequest:
    """Suporta a chamada `.execute()` no fim da chain."""
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFiles:
    """Implementa list/create/delete com a mesma assinatura do googleapiclient."""

    def __init__(self, parent: "FakeDriveService"):
        self.parent = parent

    def list(self, q=None, fields=None, pageSize=None, pageToken=None):
        # q vem como "'<folder_id>' in parents and trashed=false"
        m = re.match(r"'([^']+)' in parents", q or "")
        target_folder = m.group(1) if m else None
        matching = [
            f for f in self.parent._data
            if f.get("_parent") == target_folder
        ]
        # Devolve sem o campo interno `_parent`, sem nextPageToken (uma página só)
        out_files = [{k: v for k, v in f.items() if not k.startswith("_")} for f in matching]
        return _FakeRequest({"files": out_files})

    def create(self, body=None, media_body=None, fields=None):
        new_id = f"id-{len(self.parent._data) + len(self.parent.uploads) + 1}"
        new_file = {
            "id": new_id,
            "name": body["name"],
            "_parent": body["parents"][0],
            "createdTime": _utc_iso(datetime.datetime.now(datetime.timezone.utc)),
            "size": "100",
        }
        self.parent._data.append(new_file)
        self.parent.uploads.append({
            "name": body["name"],
            "parent": body["parents"][0],
            "media": media_body,
        })
        return _FakeRequest({"id": new_id, "name": body["name"]})

    def delete(self, fileId=None):
        self.parent.deletes.append(fileId)
        self.parent._data = [f for f in self.parent._data if f.get("id") != fileId]
        return _FakeRequest(None)


class FakeDriveService:
    """Mínimo necessário pra exercitar sync_backups_gdrive."""

    def __init__(self, initial_files: list[dict] | None = None):
        # Cada arquivo: {id, name, _parent, createdTime, size}
        self._data: list[dict] = list(initial_files or [])
        self.uploads: list[dict] = []
        self.deletes: list[str] = []

    def files(self):
        return _FakeFiles(self)


# ---------------------------------------------------------------------------


class TestListLocalBackups:
    def test_lista_so_sql_gz(self, tmp_path):
        (tmp_path / "aquag20-20260101-000000.sql.gz").touch()
        (tmp_path / "aquag20-20260102-000000.sql.gz").touch()
        (tmp_path / "outro.txt").touch()
        result = list_local_backups(tmp_path)
        assert len(result) == 2

    def test_diretorio_inexistente_devolve_lista_vazia(self, tmp_path):
        assert list_local_backups(tmp_path / "nao-existe") == []


# ---------------------------------------------------------------------------


class TestListRemoteFiles:
    def test_filtra_por_parent_folder(self):
        service = FakeDriveService([
            {"id": "1", "name": "a.sql.gz", "_parent": "folder-A",
             "createdTime": _utc_iso(datetime.datetime.now(datetime.timezone.utc))},
            {"id": "2", "name": "b.sql.gz", "_parent": "folder-B",
             "createdTime": _utc_iso(datetime.datetime.now(datetime.timezone.utc))},
        ])
        files = list_remote_files(service, "folder-A")
        assert len(files) == 1
        assert files[0]["name"] == "a.sql.gz"

    def test_pasta_vazia_devolve_lista_vazia(self):
        service = FakeDriveService([])
        assert list_remote_files(service, "folder-X") == []


# ---------------------------------------------------------------------------


class TestSync:
    def _make_local_backup(self, dir_: Path, name: str) -> Path:
        p = dir_ / name
        with gzip.open(p, "wb") as f:
            f.write(b"-- dump fake")
        return p

    def test_sem_folder_id_retorna_2(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GDRIVE_FOLDER_ID", raising=False)
        service = FakeDriveService()
        rc = sync(source_dir=tmp_path, folder_id=None, service=service)
        assert rc == 2

    def test_sem_arquivos_locais_e_noop(self, tmp_path):
        service = FakeDriveService()
        rc = sync(source_dir=tmp_path, folder_id="folder-X", service=service)
        assert rc == 0
        assert service.uploads == []

    def test_upload_arquivos_novos(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        self._make_local_backup(tmp_path, "aquag20-20260526-000000.sql.gz")
        service = FakeDriveService()

        rc = sync(source_dir=tmp_path, folder_id="folder-X", service=service)
        assert rc == 0
        assert len(service.uploads) == 2
        names = {u["name"] for u in service.uploads}
        assert names == {
            "aquag20-20260525-000000.sql.gz",
            "aquag20-20260526-000000.sql.gz",
        }
        # Cada upload foi pro folder certo
        assert all(u["parent"] == "folder-X" for u in service.uploads)

    def test_skip_se_ja_existe_na_pasta(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        self._make_local_backup(tmp_path, "aquag20-20260526-000000.sql.gz")
        # 20260525 já está na pasta
        service = FakeDriveService([
            {"id": "existing", "name": "aquag20-20260525-000000.sql.gz",
             "_parent": "folder-X",
             "createdTime": _utc_iso(datetime.datetime.now(datetime.timezone.utc))},
        ])

        rc = sync(source_dir=tmp_path, folder_id="folder-X", service=service)
        assert rc == 0
        # Só 1 upload (o novo)
        assert len(service.uploads) == 1
        assert service.uploads[0]["name"] == "aquag20-20260526-000000.sql.gz"

    def test_dry_run_nao_envia(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        service = FakeDriveService()
        rc = sync(
            source_dir=tmp_path, folder_id="folder-X",
            service=service, dry_run=True,
        )
        assert rc == 0
        assert service.uploads == []

    def test_le_folder_id_do_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDRIVE_FOLDER_ID", "env-folder-id")
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        service = FakeDriveService()
        sync(source_dir=tmp_path, service=service)
        assert service.uploads[0]["parent"] == "env-folder-id"


# ---------------------------------------------------------------------------


class TestRotateRemote:
    def test_remove_arquivos_mais_antigos_que_cutoff(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        recent = now - datetime.timedelta(days=10)
        service = FakeDriveService([
            {"id": "old1", "name": "old1.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(old)},
            {"id": "old2", "name": "old2.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(old)},
            {"id": "recent", "name": "recent.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(recent)},
        ])
        removed = rotate_remote(service, "f", retention_days=365)
        assert removed == 2
        assert service.deletes == ["old1", "old2"]
        remaining = {f["id"] for f in service._data}
        assert remaining == {"recent"}

    def test_zero_se_nada_a_remover(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        service = FakeDriveService([
            {"id": "recent", "name": "recent.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(now - datetime.timedelta(days=5))},
        ])
        removed = rotate_remote(service, "f", retention_days=30)
        assert removed == 0
        assert service.deletes == []

    def test_sync_chama_rotate_quando_flag_setada(self, tmp_path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        service = FakeDriveService([
            {"id": "old", "name": "old.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(old)},
        ])
        # Cria 1 arquivo local pra ter algo a fazer
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        rc = sync(
            source_dir=tmp_path, folder_id="f",
            service=service, retention_days=365,
        )
        assert rc == 0
        assert "old" in service.deletes

    def test_sync_nao_rotaciona_em_dry_run(self, tmp_path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        service = FakeDriveService([
            {"id": "old", "name": "old.sql.gz", "_parent": "f",
             "createdTime": _utc_iso(old)},
        ])
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        sync(
            source_dir=tmp_path, folder_id="f",
            service=service, retention_days=365, dry_run=True,
        )
        assert service.deletes == []
        assert service.uploads == []
