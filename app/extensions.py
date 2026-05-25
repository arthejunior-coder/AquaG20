from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Faça login para continuar."
login_manager.login_message_category = "warning"

csrf = CSRFProtect()

# Rate limiter:
#   - Por IP por padrão (get_remote_address).
#   - Limites GLOBAIS pra defender contra burst genérico.
#   - Rotas sensíveis (login, esqueci-senha) recebem decorator próprio
#     com limite mais apertado — ver app/blueprints/auth/routes.py.
#
# Storage:
#   - Default "memory://" — OK pra dev/teste, NÃO pra prod com multi-worker
#     (cada worker teria contador próprio → limite efetivo = N×limite).
#   - Pra prod multi-worker: setar env RATELIMIT_STORAGE_URI="redis://host:6379/0".
#     Flask-Limiter respeita esse env via app.config — sobrescreve o default
#     do construtor em init_app.
#
# Em DEV/TEST o limiter pode ser desabilitado via config; ver app/__init__.py.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000 per hour", "60 per minute"],
    storage_uri="memory://",
    strategy="fixed-window",
    headers_enabled=True,  # X-RateLimit-* nas respostas
)
