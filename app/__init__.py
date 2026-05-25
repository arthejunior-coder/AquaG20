import logging
import logging.handlers
import os
import sys

from flask import Flask

from app.config import get_config
from app.errors import register_error_handlers
from app.extensions import csrf, db, limiter, login_manager, migrate
from app.observability import JsonFormatter, configure_observability
from app.security import register_healthcheck, register_security


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)

    config_name = config_name or os.environ.get("FLASK_CONFIG", "dev")
    app.config.from_object(get_config(config_name))

    _configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    register_error_handlers(app)
    register_security(app)
    register_healthcheck(app)
    configure_observability(app)

    from app import models  # noqa: F401  — registra mappers no SQLAlchemy
    from app.models.tenant import Usuario

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(Usuario, int(user_id))

    # Blueprints
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.cadastros import bp as cadastros_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.financeiro import bp as financeiro_bp
    from app.blueprints.pedidos import bp as pedidos_bp
    from app.blueprints.pool import bp as pool_bp
    from app.blueprints.rotas import bp as rotas_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(cadastros_bp)
    app.register_blueprint(pool_bp)
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(rotas_bp)
    app.register_blueprint(financeiro_bp)
    app.register_blueprint(admin_bp)

    # CLI
    from app.cli import register_cli

    register_cli(app)

    return app


def _configure_logging(app: Flask) -> None:
    """Logging mínimo + rotacionamento opcional em arquivo.

    Em test: WARNING (silencia ruído de cada request).
    Em dev/prod: INFO no stderr.
    Se LOG_FILE_PATH definido em config: também grava em arquivo
    rotacionado (max bytes + N backups via RotatingFileHandler).
    """
    if app.config.get("TESTING"):
        level = logging.WARNING
    else:
        level = logging.INFO

    # Lê config OU env direto (configs Flask são class-attrs em import-time).
    json_logs = (
        app.config.get("OBSERVABILITY_JSON_LOGS")
        or os.environ.get("OBSERVABILITY_JSON_LOGS", "").lower() in ("1", "true", "yes")
    )
    if json_logs:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    app.logger.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    app.logger.addHandler(stream)

    # Lê config OU env direto — facilita configuração runtime sem reload da app.
    log_file = app.config.get("LOG_FILE_PATH") or os.environ.get("LOG_FILE_PATH")
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=app.config["LOG_FILE_MAX_BYTES"],
            backupCount=app.config["LOG_FILE_BACKUP_COUNT"],
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        app.logger.addHandler(file_handler)

    app.logger.setLevel(level)
