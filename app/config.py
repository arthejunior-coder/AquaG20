import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-change-me")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "mysql+pymysql://aquag20:senha@localhost:3306/aquag20?charset=utf8mb4",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    _minutes = int(os.environ.get("PERMANENT_SESSION_LIFETIME_MINUTES", "480"))
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=_minutes)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    WTF_CSRF_TIME_LIMIT = None

    ARGON2_TIME_COST = int(os.environ.get("ARGON2_TIME_COST", "2"))
    ARGON2_MEMORY_COST = int(os.environ.get("ARGON2_MEMORY_COST", "65536"))
    ARGON2_PARALLELISM = int(os.environ.get("ARGON2_PARALLELISM", "2"))

    # Rate limiting (ver app/extensions.py)
    RATELIMIT_ENABLED = True
    # Storage do limiter:
    #   memory://         — single-process (dev/test)
    #   redis://host:6379/0 — multi-worker em prod
    #   memcached://...   — outra opção
    # Flask-Limiter usa este config key automaticamente em init_app.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    # Headers de segurança — HSTS só faz sentido com HTTPS, ligado em prod
    SECURITY_HSTS_ENABLED = False
    SECURITY_HSTS_MAX_AGE = 60 * 60 * 24 * 180  # 180 dias
    # Logs em arquivo (prod). Path relativo a CWD ou absoluto.
    LOG_FILE_PATH = os.environ.get("LOG_FILE_PATH")  # None → não usa arquivo
    LOG_FILE_MAX_BYTES = int(os.environ.get("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
    LOG_FILE_BACKUP_COUNT = int(os.environ.get("LOG_FILE_BACKUP_COUNT", "5"))

    # Observabilidade — request-id sempre ligado; logs JSON e /metrics
    # opt-in via env (default falso pra não atrapalhar dev humano).
    OBSERVABILITY_JSON_LOGS = (
        os.environ.get("OBSERVABILITY_JSON_LOGS", "").lower() in ("1", "true", "yes")
    )
    OBSERVABILITY_METRICS_ENABLED = (
        os.environ.get("OBSERVABILITY_METRICS_ENABLED", "").lower() in ("1", "true", "yes")
    )

    # Email — backend 'log' loga no app.logger; 'smtp' envia de verdade.
    # Pra prod: setar MAIL_BACKEND=smtp + SMTP_HOST + SMTP_FROM_ADDR.
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "log")
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")
    SMTP_FROM_ADDR = os.environ.get("SMTP_FROM_ADDR")
    SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "AquaG20")
    SMTP_TIMEOUT = int(os.environ.get("SMTP_TIMEOUT", "30"))


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SECURITY_HSTS_ENABLED = True
    # Defaults sãos pra prod — força JSON logs + métricas (pode ser
    # sobrescrito por env se o operador quiser dev-like).
    OBSERVABILITY_JSON_LOGS = True
    OBSERVABILITY_METRICS_ENABLED = True


class TestConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "mysql+pymysql://aquag20:senha@localhost:3306/aquag20_test?charset=utf8mb4",
    )
    WTF_CSRF_ENABLED = False
    # Limiter ENABLED em test — autouse fixture em conftest.py reseta o
    # storage antes de cada test pra evitar interferência entre cenários.
    # Storage permanece em memória; default limits do BaseConfig são
    # suficientemente altos pra não estourar com a suíte normal.
    RATELIMIT_ENABLED = True


CONFIG_MAP = {
    "dev": DevConfig,
    "prod": ProdConfig,
    "test": TestConfig,
}


def get_config(name: str = "dev"):
    return CONFIG_MAP[name]
