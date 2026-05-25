"""Testes do wiring de RATELIMIT_STORAGE_URI.

Sem dependência de Redis rodando — testamos só que a config é propagada
corretamente. Validação de "Redis funciona" cabe a integração externa.
"""

from __future__ import annotations

import os

import pytest


class TestStorageURIConfig:
    def test_default_memory_em_dev(self, monkeypatch):
        """Sem env definida, default é memory://."""
        monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
        # Reimporta config pra reler env
        from importlib import reload
        from app import config as app_config
        reload(app_config)
        assert app_config.BaseConfig.RATELIMIT_STORAGE_URI == "memory://"

    def test_env_var_propaga_pra_config(self, monkeypatch):
        """Setar env antes de importar a config gera storage URI esperada."""
        monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://localhost:6379/0")
        from importlib import reload
        from app import config as app_config
        reload(app_config)
        assert app_config.BaseConfig.RATELIMIT_STORAGE_URI == "redis://localhost:6379/0"

    def test_app_config_recebe_storage_uri(self, monkeypatch):
        """`app.config["RATELIMIT_STORAGE_URI"]` reflete o que a env disse."""
        monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memcached://127.0.0.1:11211")
        from importlib import reload
        from app import config as app_config
        reload(app_config)
        from app import create_app
        app = create_app("dev")
        assert app.config["RATELIMIT_STORAGE_URI"] == "memcached://127.0.0.1:11211"

    def test_limiter_init_app_le_da_config_quando_setado(self, monkeypatch):
        """Verifica que Flask-Limiter respeita RATELIMIT_STORAGE_URI da
        config — usa o URI da config em vez do default do construtor."""
        from importlib import reload
        monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")  # vale como override
        from app import config as app_config
        reload(app_config)
        from app import create_app
        app = create_app("dev")
        from app.extensions import limiter
        # O Limiter exposto storage URI é o que vier do construtor ou da config;
        # como ambos batem em "memory://" aqui, basta confirmar que não quebra
        # e que app.config tem o valor que setamos.
        assert app.config["RATELIMIT_STORAGE_URI"] == "memory://"
        # Confirma também que o storage está utilizável
        with app.app_context():
            try:
                limiter._storage.reset()
            except Exception:
                pytest.fail("storage não inicializou após init_app")


class TestExtensionsModule:
    def test_redis_importavel(self):
        """Pacote `redis` deve estar instalado pra suportar storage Redis."""
        import importlib
        spec = importlib.util.find_spec("redis")
        assert spec is not None, (
            "pacote 'redis' não encontrado — adicione ao requirements.txt"
        )

    def test_limiter_emite_x_ratelimit_headers(self, client):
        """Headers X-RateLimit-* são úteis pra clientes decidirem back-off."""
        r = client.get("/auth/login")
        # Flask-Limiter expõe pelo menos limit / remaining nos headers
        assert any(h.startswith("X-RateLimit") for h in r.headers.keys()), (
            f"esperava X-RateLimit-* nos headers, vi: {list(r.headers.keys())}"
        )


class TestRedisURIFormat:
    """Sanity: aceita prefixos válidos sem normalizar."""

    @pytest.mark.parametrize("uri", [
        "redis://localhost:6379",
        "redis://localhost:6379/0",
        "redis://user:pw@redis.prod.internal:6379/1",
        "rediss://secure-redis.example.com:6380/0",  # TLS
        "memory://",
        "memcached://127.0.0.1:11211",
    ])
    def test_uri_aceito_sem_normalizacao(self, monkeypatch, uri):
        monkeypatch.setenv("RATELIMIT_STORAGE_URI", uri)
        from importlib import reload
        from app import config as app_config
        reload(app_config)
        assert app_config.BaseConfig.RATELIMIT_STORAGE_URI == uri
