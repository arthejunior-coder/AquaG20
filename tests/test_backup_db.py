"""Testes do scripts/backup_db.py — sem chamar mysqldump real.

Cobre:
  - parse_database_url: aceita variações da URL (mysql:// e mysql+pymysql://)
  - rotate: remove arquivos antigos por mtime, preserva os novos
  - backup: monkeypatch subprocess + shutil.which → valida fluxo completo
    (cria .sql.gz com conteúdo do "dump"; passa MYSQL_PWD via env;
    rotaciona ao final)
"""

from __future__ import annotations

import datetime
import gzip
import os
import subprocess
from pathlib import Path

import pytest

from scripts.backup_db import (
    _build_mysqldump_cmd,
    backup,
    parse_database_url,
    rotate,
)


# ---------------------------------------------------------------------------


class TestParseDatabaseURL:
    def test_mysql_pymysql_url_completa(self):
        cfg = parse_database_url(
            "mysql+pymysql://aquag20:senha123@db.prod:3306/aquag20?charset=utf8mb4"
        )
        assert cfg["user"] == "aquag20"
        assert cfg["password"] == "senha123"
        assert cfg["host"] == "db.prod"
        assert cfg["port"] == 3306
        assert cfg["database"] == "aquag20"

    def test_mysql_url_simples(self):
        cfg = parse_database_url("mysql://root:r00t@localhost/foo")
        assert cfg["user"] == "root"
        assert cfg["password"] == "r00t"
        assert cfg["host"] == "localhost"
        assert cfg["port"] == 3306  # default
        assert cfg["database"] == "foo"

    def test_senha_url_encoded_e_decodificada(self):
        # Senha "p@ss/w0rd" encoded:
        cfg = parse_database_url("mysql://u:p%40ss%2Fw0rd@localhost/db")
        assert cfg["password"] == "p@ss/w0rd"

    def test_sem_database_no_path(self):
        cfg = parse_database_url("mysql://u:p@host:3306/")
        assert cfg["database"] == ""


# ---------------------------------------------------------------------------


class TestRotate:
    def test_remove_arquivos_antigos_preserva_novos(self, tmp_path):
        # Cria 3 arquivos com mtimes diferentes
        old1 = tmp_path / "aquag20-20230101-000000.sql.gz"
        old2 = tmp_path / "aquag20-20230102-000000.sql.gz"
        novo = tmp_path / "aquag20-20260525-000000.sql.gz"
        for f in (old1, old2, novo):
            f.touch()
        # Setar mtimes: olds 60 dias atrás, novo agora
        now = datetime.datetime.now().timestamp()
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=60)).timestamp()
        os.utime(old1, (old_ts, old_ts))
        os.utime(old2, (old_ts, old_ts))
        os.utime(novo, (now, now))

        removed = rotate(tmp_path, "aquag20", retention_days=30)
        assert removed == 2
        assert not old1.exists()
        assert not old2.exists()
        assert novo.exists()

    def test_so_remove_arquivos_do_db_alvo(self, tmp_path):
        """Não remove arquivos de outros DBs no mesmo diretório."""
        alvo = tmp_path / "aquag20-20230101-000000.sql.gz"
        outro = tmp_path / "outro_db-20230101-000000.sql.gz"
        for f in (alvo, outro):
            f.touch()
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=99)).timestamp()
        os.utime(alvo, (old_ts, old_ts))
        os.utime(outro, (old_ts, old_ts))

        removed = rotate(tmp_path, "aquag20", retention_days=30)
        assert removed == 1
        assert outro.exists()

    def test_zero_quando_nada_a_rotacionar(self, tmp_path):
        # Dir vazio
        assert rotate(tmp_path, "aquag20", retention_days=30) == 0


# ---------------------------------------------------------------------------


class TestBuildCmd:
    def test_cmd_tem_flags_de_seguranca(self):
        cfg = {"user": "u", "host": "h", "port": 3306, "database": "d"}
        cmd = _build_mysqldump_cmd(cfg)
        assert cmd[0] == "mysqldump"
        assert "--single-transaction" in cmd
        assert "--routines" in cmd
        assert "--triggers" in cmd
        assert "--default-character-set=utf8mb4" in cmd
        # NÃO inclui --password no comando (deve vir via env MYSQL_PWD)
        assert not any("--password" in c for c in cmd)
        # Database NO FINAL (mysqldump exige nesse formato)
        assert cmd[-1] == "d"


# ---------------------------------------------------------------------------


class TestBackupFluxoCompleto:
    """Substitui subprocess.run + shutil.which pra testar o fluxo end-to-end."""

    def test_fluxo_feliz_gera_arquivo_gzipped(self, tmp_path, monkeypatch):
        # mysqldump "disponível"
        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: "/usr/bin/mysqldump")

        # Mock do subprocess.run: dump retorna stdout fake
        captured = {}
        fake_dump = b"-- MySQL dump\nINSERT INTO t VALUES (1);\n"

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, stdout=fake_dump, stderr=b"")

        monkeypatch.setattr("scripts.backup_db.subprocess.run", _fake_run)

        # Roda
        fixed_now = datetime.datetime(2026, 5, 25, 14, 30, 0)
        rc = backup(
            output_dir=tmp_path,
            retention_days=30, keep_all=False,
            database_url="mysql+pymysql://u:secret@h:3306/aquag20_test",
            now=fixed_now,
        )

        assert rc == 0
        # Arquivo criado com nome esperado
        expected = tmp_path / "aquag20_test-20260525-143000.sql.gz"
        assert expected.exists()
        # Conteúdo: gzip do dump
        with gzip.open(expected, "rb") as f:
            assert f.read() == fake_dump
        # MYSQL_PWD foi enviado via env (não via --password no cmd)
        assert captured["env"]["MYSQL_PWD"] == "secret"
        # User aparece no cmd
        assert "--user=u" in captured["cmd"]

    def test_mysqldump_ausente_retorna_3(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: None)
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=True,
            database_url="mysql://u:p@h/d",
        )
        assert rc == 3
        # Nenhum arquivo gerado
        assert list(tmp_path.iterdir()) == []

    def test_sem_database_url_retorna_2(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=True,
            database_url=None,
        )
        assert rc == 2

    def test_url_sem_db_no_path_retorna_2(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: "/usr/bin/mysqldump")
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=True,
            database_url="mysql://u:p@h/",
        )
        assert rc == 2

    def test_mysqldump_exit_nonzero_propaga(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: "/usr/bin/mysqldump")
        monkeypatch.setattr(
            "scripts.backup_db.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 1, stdout=b"", stderr=b"access denied for user",
            ),
        )
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=True,
            database_url="mysql://u:p@h/aquag20",
        )
        assert rc == 1
        # NÃO criou arquivo (não escreveu nada com gzip)
        assert list(tmp_path.iterdir()) == []

    def test_rotaciona_apos_dump_sucesso(self, tmp_path, monkeypatch):
        # Arquivo antigo no destino antes de rodar
        antigo = tmp_path / "aquag20_test-20230101-000000.sql.gz"
        antigo.touch()
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=60)).timestamp()
        os.utime(antigo, (old_ts, old_ts))

        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: "/usr/bin/mysqldump")
        monkeypatch.setattr(
            "scripts.backup_db.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 0, stdout=b"dump", stderr=b"",
            ),
        )
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=False,
            database_url="mysql://u:p@h/aquag20_test",
        )
        assert rc == 0
        assert not antigo.exists()  # rotacionado
        # Novo arquivo existe
        files = list(tmp_path.glob("aquag20_test-*.sql.gz"))
        assert len(files) == 1

    def test_keep_all_nao_rotaciona(self, tmp_path, monkeypatch):
        antigo = tmp_path / "aquag20_test-20230101-000000.sql.gz"
        antigo.touch()
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=99)).timestamp()
        os.utime(antigo, (old_ts, old_ts))

        monkeypatch.setattr("scripts.backup_db.shutil.which", lambda x: "/usr/bin/mysqldump")
        monkeypatch.setattr(
            "scripts.backup_db.subprocess.run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 0, stdout=b"dump", stderr=b"",
            ),
        )
        rc = backup(
            output_dir=tmp_path, retention_days=30, keep_all=True,
            database_url="mysql://u:p@h/aquag20_test",
        )
        assert rc == 0
        assert antigo.exists()  # NÃO removido com --keep-all
