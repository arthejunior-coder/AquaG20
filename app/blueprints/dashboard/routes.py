from flask import render_template
from flask_login import current_user, login_required

from app.blueprints.dashboard import bp
from app.extensions import db
from app.services.indicadores_service import IndicadoresService


@bp.route("/")
@login_required
def index():
    svc = IndicadoresService(db.session, current_user.tenant_id)
    snap = svc.snapshot()
    return render_template(
        "dashboard/index.html",
        user=current_user,
        envelhecimento=snap["envelhecimento"],
        casamento=snap["casamento"],
        custo=snap["custo_reposicao"],
    )
