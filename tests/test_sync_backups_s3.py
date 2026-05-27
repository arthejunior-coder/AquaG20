"""Testes do scripts/sync_backups_s3.py — sem chamar S3 real.

Cobre:
  - Helpers: _normalize_prefix, list_local_backups, list_remote_keys
  - sync(): config faltando, dry-run, idempotência (skip se já existe),
            upload com SSE+storage class, rotação opcional
  - rotate_remote: cutoff por LastModified do S3 (não pelo nome)

Usa um FakeS3Client que registra chamadas — sem boto3 real, sem rede.
"""

from __future__ import annotations

import datetime
import gzip
from pathlib import Path

import pytest

from scripts.sync_backups_s3 import (
    _normalize_prefix,
    list_local_backups,
    list_remote_keys,
    rotate_remote,
    sync,
    upload_file,
)


# ---------------------------------------------------------------------------
# Fake S3 client — mimica boto3.client("s3") só nos métodos que usamos
# ---------------------------------------------------------------------------


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        # Filtra por Bucket+Prefix se aplicável (suficiente pros testes)
        for page in self._pages:
            yield page


class FakeS3Client:
    """Mínimo necessário pra exercitar o sync."""

    def __init__(self, initial_objects: list[dict] | None = None):
        # objects = lista de dicts {Key, LastModified, Size?}
        self.objects: list[dict] = list(initial_objects or [])
        self.uploads: list[dict] = []  # registro de upload_file
        self.deletes: list[list[dict]] = []  # registro de delete_objects

    def get_paginator(self, op_name):
        assert op_name == "list_objects_v2"
        # Devolve uma única página com tudo (suficiente pros tamanhos de teste)
        return _FakePaginator([{"Contents": self.objects}])

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append({
            "filename": filename, "bucket": bucket, "key": key,
            "extra_args": ExtraArgs or {},
        })
        # Atualiza estado interno como se o objeto estivesse no bucket agora
        self.objects.append({
            "Key": key,
            "LastModified": datetime.datetime.now(datetime.timezone.utc),
        })

    def delete_objects(self, Bucket, Delete):
        keys_to_remove = {o["Key"] for o in Delete["Objects"]}
        self.deletes.append(Delete["Objects"])
        self.objects = [o for o in self.objects if o["Key"] not in keys_to_remove]


# ---------------------------------------------------------------------------


class TestNormalizePrefix:
    def test_adiciona_barra_no_fim(self):
        assert _normalize_prefix("foo") == "foo/"
        assert _normalize_prefix("foo/bar") == "foo/bar/"

    def test_preserva_barra_existente(self):
        assert _normalize_prefix("foo/") == "foo/"

    def test_vazio_devolve_vazio(self):
        assert _normalize_prefix("") == ""


# ---------------------------------------------------------------------------


class TestListLocalBackups:
    def test_lista_so_sql_gz(self, tmp_path):
        (tmp_path / "aquag20-20260101-000000.sql.gz").touch()
        (tmp_path / "aquag20-20260102-000000.sql.gz").touch()
        (tmp_path / "outro.txt").touch()
        (tmp_path / "aquag20.sql").touch()  # sem .gz
        result = list_local_backups(tmp_path)
        assert len(result) == 2
        assert all(f.suffix == ".gz" for f in result)

    def test_ordenado_por_nome(self, tmp_path):
        # Nomes com timestamp ordenam cronologicamente
        (tmp_path / "aquag20-20260102-000000.sql.gz").touch()
        (tmp_path / "aquag20-20260101-000000.sql.gz").touch()
        result = list_local_backups(tmp_path)
        assert result[0].name == "aquag20-20260101-000000.sql.gz"
        assert result[1].name == "aquag20-20260102-000000.sql.gz"

    def test_diretorio_inexistente_devolve_lista_vazia(self, tmp_path):
        assert list_local_backups(tmp_path / "nao-existe") == []


# ---------------------------------------------------------------------------


class TestListRemoteKeys:
    def test_extrai_filename_relativo_ao_prefix(self):
        client = FakeS3Client([
            {"Key": "aquag20-backups/file-a.sql.gz", "LastModified": datetime.datetime.now(datetime.timezone.utc)},
            {"Key": "aquag20-backups/file-b.sql.gz", "LastModified": datetime.datetime.now(datetime.timezone.utc)},
        ])
        keys = list_remote_keys(client, "my-bucket", "aquag20-backups/")
        assert keys == {"file-a.sql.gz", "file-b.sql.gz"}

    def test_bucket_vazio_devolve_set_vazio(self):
        client = FakeS3Client([])
        assert list_remote_keys(client, "my-bucket", "aquag20-backups/") == set()


# ---------------------------------------------------------------------------


class TestSync:
    def _make_local_backup(self, dir_: Path, name: str) -> Path:
        p = dir_ / name
        with gzip.open(p, "wb") as f:
            f.write(b"-- dump fake")
        return p

    def test_sem_bucket_retorna_2(self, tmp_path, monkeypatch):
        monkeypatch.delenv("S3_BACKUP_BUCKET", raising=False)
        client = FakeS3Client()
        rc = sync(source_dir=tmp_path, bucket=None, client=client)
        assert rc == 2

    def test_sem_arquivos_locais_e_noop(self, tmp_path):
        client = FakeS3Client()
        rc = sync(source_dir=tmp_path, bucket="my-bucket", client=client)
        assert rc == 0
        assert client.uploads == []

    def test_upload_arquivos_novos(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        self._make_local_backup(tmp_path, "aquag20-20260526-000000.sql.gz")
        client = FakeS3Client()

        rc = sync(
            source_dir=tmp_path, bucket="my-bucket",
            prefix="aquag20-backups/", client=client,
        )
        assert rc == 0
        assert len(client.uploads) == 2
        # Key inclui prefix
        keys = {u["key"] for u in client.uploads}
        assert keys == {
            "aquag20-backups/aquag20-20260525-000000.sql.gz",
            "aquag20-backups/aquag20-20260526-000000.sql.gz",
        }

    def test_skip_se_ja_existe_no_bucket(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        self._make_local_backup(tmp_path, "aquag20-20260526-000000.sql.gz")
        # 20260525 já está no bucket
        client = FakeS3Client([
            {"Key": "aquag20-backups/aquag20-20260525-000000.sql.gz",
             "LastModified": datetime.datetime.now(datetime.timezone.utc)},
        ])

        rc = sync(
            source_dir=tmp_path, bucket="my-bucket",
            prefix="aquag20-backups/", client=client,
        )
        assert rc == 0
        # Só 1 upload (o novo)
        assert len(client.uploads) == 1
        assert client.uploads[0]["key"].endswith("20260526-000000.sql.gz")

    def test_dry_run_nao_envia(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket",
            client=client, dry_run=True,
        )
        assert rc == 0
        assert client.uploads == []  # nenhum upload de verdade

    def test_upload_usa_sse_aes256_e_storage_class(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket",
            storage_class="STANDARD_IA", client=client,
        )
        assert rc == 0
        assert len(client.uploads) == 1
        extra = client.uploads[0]["extra_args"]
        assert extra["ServerSideEncryption"] == "AES256"
        assert extra["StorageClass"] == "STANDARD_IA"
        assert extra["ContentType"] == "application/gzip"

    def test_storage_class_default_standard(self, tmp_path, monkeypatch):
        monkeypatch.delenv("S3_BACKUP_STORAGE_CLASS", raising=False)
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(source_dir=tmp_path, bucket="my-bucket", client=client)
        assert client.uploads[0]["extra_args"]["StorageClass"] == "STANDARD"

    def test_prefix_normalizado_se_sem_barra(self, tmp_path):
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(
            source_dir=tmp_path, bucket="my-bucket",
            prefix="custom-prefix",  # sem / no fim
            client=client,
        )
        assert client.uploads[0]["key"] == "custom-prefix/aquag20-20260525-000000.sql.gz"

    def test_le_bucket_e_prefix_do_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S3_BACKUP_BUCKET", "env-bucket")
        monkeypatch.setenv("S3_BACKUP_PREFIX", "env-prefix/")
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(source_dir=tmp_path, client=client)
        assert client.uploads[0]["bucket"] == "env-bucket"
        assert client.uploads[0]["key"].startswith("env-prefix/")

    def test_storage_class_vazio_omite_param(self, tmp_path):
        """Compatibilidade B2/R2 — passar "" desabilita StorageClass no ExtraArgs."""
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(
            source_dir=tmp_path, bucket="my-bucket",
            storage_class="",  # explicitamente vazio
            client=client,
        )
        extra = client.uploads[0]["extra_args"]
        assert "StorageClass" not in extra
        # SSE e ContentType continuam:
        assert extra["ServerSideEncryption"] == "AES256"
        assert extra["ContentType"] == "application/gzip"

    def test_storage_class_env_vazio_omite_param(self, tmp_path, monkeypatch):
        """S3_BACKUP_STORAGE_CLASS=  no .env também desabilita."""
        monkeypatch.setenv("S3_BACKUP_STORAGE_CLASS", "")
        self._make_local_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(source_dir=tmp_path, bucket="my-bucket", client=client)
        assert "StorageClass" not in client.uploads[0]["extra_args"]


# ---------------------------------------------------------------------------


class TestEndpointURL:
    """Plumbing do endpoint_url — pra suporte a B2/OCI/R2/MinIO."""

    def test_endpoint_url_passado_pra_boto3_client(self, tmp_path, monkeypatch):
        """Quando endpoint_url é setado, boto3.client('s3') recebe o param."""
        # Cria 1 arquivo local pra forçar instanciação do client
        from scripts.sync_backups_s3 import sync
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        captured_kwargs = {}

        class _FakeBoto3:
            @staticmethod
            def client(service_name, **kwargs):
                captured_kwargs["service"] = service_name
                captured_kwargs.update(kwargs)
                return FakeS3Client()

        monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)

        sync(
            source_dir=tmp_path, bucket="my-bucket",
            endpoint_url="https://s3.us-west-002.backblazeb2.com",
            # client=None — força criar via boto3
        )
        assert captured_kwargs["service"] == "s3"
        assert captured_kwargs["endpoint_url"] == "https://s3.us-west-002.backblazeb2.com"

    def test_sem_endpoint_url_nao_passa_kwarg(self, tmp_path, monkeypatch):
        """Sem endpoint_url, boto3.client é chamado sem kwarg endpoint_url (AWS default)."""
        from scripts.sync_backups_s3 import sync
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        captured_kwargs = {}

        class _FakeBoto3:
            @staticmethod
            def client(service_name, **kwargs):
                captured_kwargs["service"] = service_name
                captured_kwargs.update(kwargs)
                return FakeS3Client()

        monkeypatch.delenv("S3_BACKUP_ENDPOINT_URL", raising=False)
        monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)

        sync(source_dir=tmp_path, bucket="my-bucket")
        # endpoint_url NÃO deve aparecer nos kwargs (deixa boto3 usar AWS default)
        assert "endpoint_url" not in captured_kwargs

    def test_endpoint_url_do_env(self, tmp_path, monkeypatch):
        """Lê S3_BACKUP_ENDPOINT_URL do .env quando não passado por arg."""
        from scripts.sync_backups_s3 import sync
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        captured_kwargs = {}

        class _FakeBoto3:
            @staticmethod
            def client(service_name, **kwargs):
                captured_kwargs.update(kwargs)
                return FakeS3Client()

        monkeypatch.setenv("S3_BACKUP_ENDPOINT_URL", "https://oci.example.com")
        monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)

        sync(source_dir=tmp_path, bucket="my-bucket")
        assert captured_kwargs["endpoint_url"] == "https://oci.example.com"


# ---------------------------------------------------------------------------


class TestRotateRemote:
    def test_remove_objetos_mais_antigos_que_cutoff(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        recent = now - datetime.timedelta(days=10)
        client = FakeS3Client([
            {"Key": "aquag20-backups/old1.sql.gz", "LastModified": old},
            {"Key": "aquag20-backups/old2.sql.gz", "LastModified": old},
            {"Key": "aquag20-backups/recent.sql.gz", "LastModified": recent},
        ])
        removed = rotate_remote(client, "my-bucket", "aquag20-backups/", retention_days=365)
        assert removed == 2
        remaining_keys = {o["Key"] for o in client.objects}
        assert remaining_keys == {"aquag20-backups/recent.sql.gz"}

    def test_zero_se_nada_a_remover(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        client = FakeS3Client([
            {"Key": "aquag20-backups/recent.sql.gz",
             "LastModified": now - datetime.timedelta(days=5)},
        ])
        removed = rotate_remote(client, "my-bucket", "aquag20-backups/", retention_days=30)
        assert removed == 0
        assert client.deletes == []  # nenhum delete chamado

    def test_sync_chama_rotate_quando_flag_setada(self, tmp_path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        client = FakeS3Client([
            {"Key": "aquag20-backups/old.sql.gz", "LastModified": old},
        ])
        # Cria 1 arquivo local pra ter algo a fazer
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        rc = sync(
            source_dir=tmp_path, bucket="my-bucket",
            prefix="aquag20-backups/", client=client,
            s3_retention_days=365,
        )
        assert rc == 0
        assert len(client.deletes) == 1  # rotação aconteceu

    def test_sync_nao_rotaciona_em_dry_run(self, tmp_path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=400)
        client = FakeS3Client([
            {"Key": "aquag20-backups/old.sql.gz", "LastModified": old},
        ])
        p = tmp_path / "aquag20-20260525-000000.sql.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"dump")

        sync(
            source_dir=tmp_path, bucket="my-bucket",
            prefix="aquag20-backups/", client=client,
            s3_retention_days=365, dry_run=True,
        )
        assert client.deletes == []
        assert client.uploads == []


# ---------------------------------------------------------------------------


class TestObjectLock:
    """Object Lock COMPLIANCE per-upload — proteção anti-ransomware.

    Guardrails são crítos: Compliance é IRREVERSÍVEL. Bug que envia retention
    enorme cria custo permanente. Esses testes blindam o cap.
    """

    def _make_backup(self, dir_, name):
        p = dir_ / name
        with gzip.open(p, "wb") as f:
            f.write(b"dump")
        return p

    def test_sem_lock_config_nao_envia_lock_headers(self, tmp_path, monkeypatch):
        """Sem env vars + sem args, upload sai sem ObjectLockMode."""
        monkeypatch.delenv("S3_BACKUP_LOCK_MODE", raising=False)
        monkeypatch.delenv("S3_BACKUP_LOCK_RETENTION_DAYS", raising=False)
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(source_dir=tmp_path, bucket="my-bucket", client=client)
        assert rc == 0
        extra = client.uploads[0]["extra_args"]
        assert "ObjectLockMode" not in extra
        assert "ObjectLockRetainUntilDate" not in extra

    def test_lock_compliance_aplica_headers_no_upload(self, tmp_path):
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE", lock_retention_days=7,
        )
        assert rc == 0
        extra = client.uploads[0]["extra_args"]
        assert extra["ObjectLockMode"] == "COMPLIANCE"
        # RetainUntilDate é datetime ~7 dias no futuro
        retain_until = extra["ObjectLockRetainUntilDate"]
        assert isinstance(retain_until, datetime.datetime)
        delta = retain_until - datetime.datetime.now(datetime.timezone.utc)
        assert 6.9 < delta.total_seconds() / 86400 < 7.1  # ~7 dias

    def test_lock_governance_aceito(self, tmp_path):
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="GOVERNANCE", lock_retention_days=30,
        )
        assert rc == 0
        assert client.uploads[0]["extra_args"]["ObjectLockMode"] == "GOVERNANCE"

    def test_lock_le_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S3_BACKUP_LOCK_MODE", "COMPLIANCE")
        monkeypatch.setenv("S3_BACKUP_LOCK_RETENTION_DAYS", "10")
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        sync(source_dir=tmp_path, bucket="my-bucket", client=client)
        extra = client.uploads[0]["extra_args"]
        assert extra["ObjectLockMode"] == "COMPLIANCE"
        delta = extra["ObjectLockRetainUntilDate"] - datetime.datetime.now(datetime.timezone.utc)
        assert 9.9 < delta.total_seconds() / 86400 < 10.1

    def test_lock_mode_invalido_retorna_2(self, tmp_path):
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="INVALID", lock_retention_days=7,
        )
        assert rc == 2
        assert client.uploads == []  # nada subiu

    def test_lock_retention_zero_retorna_2(self, tmp_path):
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE", lock_retention_days=0,
        )
        assert rc == 2
        assert client.uploads == []

    def test_lock_retention_negativo_retorna_2(self, tmp_path):
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE", lock_retention_days=-5,
        )
        assert rc == 2

    def test_lock_retention_excede_cap_retorna_2(self, tmp_path):
        """CRÍTICO: cap MAX_LOCK_RETENTION_DAYS protege contra typo (30 vs 30000)."""
        from scripts.sync_backups_s3 import _MAX_LOCK_RETENTION_DAYS
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE",
            lock_retention_days=_MAX_LOCK_RETENTION_DAYS + 1,
        )
        assert rc == 2
        assert client.uploads == []  # rejeitado ANTES de qualquer upload

    def test_lock_retention_no_cap_aceito(self, tmp_path):
        from scripts.sync_backups_s3 import _MAX_LOCK_RETENTION_DAYS
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE",
            lock_retention_days=_MAX_LOCK_RETENTION_DAYS,  # exatamente no limite
        )
        assert rc == 0
        assert len(client.uploads) == 1

    def test_lock_mode_sem_retention_retorna_2(self, tmp_path, monkeypatch):
        """Setar só um lado é ambíguo — exigir ambos juntos."""
        # Isola env vars do .env real do dev pra não contaminar o teste
        monkeypatch.delenv("S3_BACKUP_LOCK_MODE", raising=False)
        monkeypatch.delenv("S3_BACKUP_LOCK_RETENTION_DAYS", raising=False)
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE", lock_retention_days=None,
        )
        assert rc == 2

    def test_lock_retention_sem_mode_retorna_2(self, tmp_path, monkeypatch):
        monkeypatch.delenv("S3_BACKUP_LOCK_MODE", raising=False)
        monkeypatch.delenv("S3_BACKUP_LOCK_RETENTION_DAYS", raising=False)
        self._make_backup(tmp_path, "aquag20-20260525-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode=None, lock_retention_days=7,
        )
        assert rc == 2

    def test_lock_validacao_acontece_antes_de_qualquer_upload(self, tmp_path):
        """Guardrail roda CEDO — múltiplos arquivos locais, nenhum sobe se inválido."""
        for n in range(5):
            self._make_backup(tmp_path, f"aquag20-2026052{n}-000000.sql.gz")
        client = FakeS3Client()
        rc = sync(
            source_dir=tmp_path, bucket="my-bucket", client=client,
            lock_mode="COMPLIANCE", lock_retention_days=99999,
        )
        assert rc == 2
        assert client.uploads == []  # zero uploads — não vazou nenhum
