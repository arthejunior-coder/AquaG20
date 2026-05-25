"""Rotas do pool de garrafões: saldos, histórico de movimentos e 5 forms
de movimento manual (compra, descarte, transferência, avaria, ajuste).

Autorização: admin e gestor podem tudo; atendimento tem leitura.
Toda escrita passa pelo PoolService — não toca em saldos/movimentos direto.
"""

from collections import defaultdict

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.decorators import papel_requerido, repo
from app.blueprints.pool import bp
from app.blueprints.pool.forms import (
    AjusteForm,
    AvariaForm,
    CompraForm,
    DescarteForm,
    EnvaseForm,
    TransferenciaForm,
)
from app.extensions import db
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    GarrafaoSaldo,
    LocalEstoque,
    TipoGarrafao,
    TipoLocal,
    TipoMovimento,
)
from app.repositories.pool_repo import (
    GarrafaoMovimentoRepository,
    GarrafaoSaldoRepository,
    LocalEstoqueRepository,
    TipoGarrafaoRepository,
)
from app.services.envase_service import EnvaseService
from app.services.fefo_service import FEFOService
from app.services.pool_service import (
    EstoqueInsuficienteError,
    InvariantePoolViolada,
    PoolService,
)


# --- helpers ---------------------------------------------------------------


def _hx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _tipos_choices(somente_ativos: bool = True):
    r = repo(TipoGarrafaoRepository)
    stmt = r.select().order_by(TipoGarrafao.nome)
    if somente_ativos:
        stmt = stmt.where(TipoGarrafao.ativo == True)  # noqa: E712
    return [(t.id, f"{t.nome}") for t in r.all(stmt)]


def _locais_choices(somente_tipo: TipoLocal | None = None):
    r = repo(LocalEstoqueRepository)
    stmt = r.select().order_by("tipo", "nome")
    if somente_tipo:
        stmt = stmt.where(LocalEstoque.tipo == somente_tipo)
    locais = r.all(stmt)
    return [(l.id, f"[{l.tipo.value}] {l.nome}") for l in locais]


def _pool_service() -> PoolService:
    return PoolService(db.session, current_user.tenant_id, current_user.id)


def _envase_service() -> EnvaseService:
    return EnvaseService(db.session, current_user.tenant_id, current_user.id)


def _fefo_service() -> FEFOService:
    return FEFOService(db.session, current_user.tenant_id)


# --- FEFO endpoint JSON (consultado pelo pedido no passo 14) ---------------


@bp.route("/fefo")
@papel_requerido("admin", "gestor", "atendimento")
def fefo_sugestao():
    """Sugere lotes a despachar via FEFO. Resposta JSON.

    Query params:
        tipo_garrafao_id (int) — required
        local_id (int)         — required (geralmente um veículo ou CD)
        quantidade (int)       — required, >0
        estado (str)           — opcional, default 'cheio'
    """
    try:
        tipo_id = int(request.args["tipo_garrafao_id"])
        local_id = int(request.args["local_id"])
        quantidade = int(request.args["quantidade"])
        estado_str = request.args.get("estado", "cheio")
        estado = EstadoGarrafao(estado_str)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": f"parâmetros inválidos: {e}"}), 400

    sugestao = _fefo_service().recomendar_lotes(
        tipo_garrafao_id=tipo_id,
        local_id=local_id,
        quantidade=quantidade,
        estado=estado,
    )
    return jsonify({
        "lotes": [
            {"validade": l.validade.isoformat(), "quantidade": l.quantidade}
            for l in sugestao.lotes
        ],
        "quantidade_solicitada": sugestao.quantidade_solicitada,
        "total_atendido": sugestao.total_atendido,
        "quantidade_faltando": sugestao.quantidade_faltando,
        "atende_totalmente": sugestao.atende_totalmente,
    })


# --- saldos ----------------------------------------------------------------


@bp.route("/")
@papel_requerido("admin", "gestor", "atendimento")
def index():
    return redirect(url_for("pool.saldos"))


@bp.route("/saldos")
@papel_requerido("admin", "gestor", "atendimento")
def saldos():
    r_saldo = repo(GarrafaoSaldoRepository)
    r_tipo = repo(TipoGarrafaoRepository)
    r_local = repo(LocalEstoqueRepository)

    stmt = (
        r_saldo.select()
        .where(GarrafaoSaldo.quantidade > 0)
        .order_by(GarrafaoSaldo.tipo_garrafao_id, GarrafaoSaldo.local_id,
                  GarrafaoSaldo.estado, GarrafaoSaldo.validade)
    )
    saldos_rows = r_saldo.all(stmt)

    tipos_by_id = {t.id: t for t in r_tipo.all()}
    locais_by_id = {l.id: l for l in r_local.all()}

    # Agrupa por tipo_garrafao_id; dentro, por (local_id, estado)
    grupos = defaultdict(lambda: {"tipo": None, "items": [], "total": 0})
    for s in saldos_rows:
        g = grupos[s.tipo_garrafao_id]
        g["tipo"] = tipos_by_id.get(s.tipo_garrafao_id)
        g["items"].append(s)
        g["total"] += s.quantidade

    # Lista ordenada de grupos
    grupos_list = [grupos[tid] for tid in sorted(grupos.keys())]
    total_pool = sum(g["total"] for g in grupos_list)

    return render_template(
        "pool/saldos.html",
        grupos=grupos_list,
        locais_by_id=locais_by_id,
        total_pool=total_pool,
    )


# --- histórico de movimentos -----------------------------------------------


@bp.route("/movimentos")
@papel_requerido("admin", "gestor", "atendimento")
def movimentos():
    tipo_filter = request.args.get("tipo", "").strip()

    r_mov = repo(GarrafaoMovimentoRepository)
    r_tipo = repo(TipoGarrafaoRepository)
    r_local = repo(LocalEstoqueRepository)

    stmt = r_mov.select().order_by(GarrafaoMovimento.criado_em.desc(), GarrafaoMovimento.id.desc())
    if tipo_filter:
        try:
            stmt = stmt.where(GarrafaoMovimento.tipo == TipoMovimento(tipo_filter))
        except ValueError:
            pass  # filtro inválido — ignora
    movs = r_mov.all(stmt.limit(200))

    tipos_by_id = {t.id: t for t in r_tipo.all()}
    locais_by_id = {l.id: l for l in r_local.all()}

    tipos_movimento = [(t.value, t.value.title()) for t in TipoMovimento]

    template = "pool/_tabela_movimentos.html" if _hx() else "pool/movimentos.html"
    return render_template(
        template,
        movs=movs,
        tipos_by_id=tipos_by_id,
        locais_by_id=locais_by_id,
        tipos_movimento=tipos_movimento,
        tipo_filter=tipo_filter,
    )


# --- 5 formulários de movimento --------------------------------------------


def _handle_pool_op(svc_call, success_msg: str, redirect_endpoint="pool.movimentos"):
    """Wrap chamada do PoolService com try/except para mostrar erros como flash."""
    try:
        svc_call()
        db.session.commit()
        flash(success_msg, "success")
        return redirect(url_for(redirect_endpoint))
    except (EstoqueInsuficienteError, InvariantePoolViolada, ValueError) as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
        return None


@bp.route("/movimentos/novo/compra", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_compra():
    form = CompraForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    form.local_destino_id.choices = _locais_choices()
    if form.validate_on_submit():
        svc = _pool_service()
        result = _handle_pool_op(
            lambda: svc.registrar_compra(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_destino_id=form.local_destino_id.data,
                validade=form.validade.data,
                estado=EstadoGarrafao(form.estado.data),
                observacao=form.observacao.data or None,
            ),
            success_msg=f"Compra registrada: {form.quantidade.data} garrafões.",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_compra.html", form=form)


@bp.route("/movimentos/novo/descarte", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_descarte():
    form = DescarteForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    form.local_origem_id.choices = _locais_choices()
    if form.validate_on_submit():
        svc = _pool_service()
        result = _handle_pool_op(
            lambda: svc.registrar_descarte(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_origem_id=form.local_origem_id.data,
                estado=EstadoGarrafao(form.estado.data),
                validade=form.validade.data,
                observacao=form.observacao.data or None,
            ),
            success_msg=f"Descarte registrado: {form.quantidade.data} garrafões.",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_descarte.html", form=form)


@bp.route("/movimentos/novo/transferencia", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_transferencia():
    form = TransferenciaForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    locais = _locais_choices()
    form.local_origem_id.choices = locais
    form.local_destino_id.choices = locais
    if form.validate_on_submit():
        svc = _pool_service()
        result = _handle_pool_op(
            lambda: svc.registrar_transferencia(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_origem_id=form.local_origem_id.data,
                local_destino_id=form.local_destino_id.data,
                estado=EstadoGarrafao(form.estado.data),
                validade=form.validade.data,
                observacao=form.observacao.data or None,
            ),
            success_msg=f"Transferência registrada: {form.quantidade.data} garrafões.",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_transferencia.html", form=form)


@bp.route("/movimentos/novo/avaria", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_avaria():
    form = AvariaForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    form.local_id.choices = _locais_choices()
    if form.validate_on_submit():
        svc = _pool_service()
        result = _handle_pool_op(
            lambda: svc.registrar_avaria(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_id=form.local_id.data,
                estado_origem=EstadoGarrafao(form.estado_origem.data),
                validade=form.validade.data,
                observacao=form.observacao.data or None,
            ),
            success_msg=f"Avaria registrada: {form.quantidade.data} garrafões.",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_avaria.html", form=form)


@bp.route("/movimentos/novo/envase", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_envase():
    form = EnvaseForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    form.local_industria_id.choices = _locais_choices(somente_tipo=TipoLocal.industria)
    if not form.local_industria_id.choices:
        flash(
            "Cadastre primeiro pelo menos um Local de Estoque do tipo 'industria'.",
            "warning",
        )
    if form.validate_on_submit():
        svc = _envase_service()
        result = _handle_pool_op(
            lambda: svc.registrar_envase(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_industria_id=form.local_industria_id.data,
                validade=form.validade.data,
                observacao=form.observacao.data or None,
            ),
            success_msg=f"Envase registrado: {form.quantidade.data} garrafões (vazio→cheio).",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_envase.html", form=form)


@bp.route("/movimentos/novo/ajuste", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_movimento_ajuste():
    form = AjusteForm()
    form.tipo_garrafao_id.choices = _tipos_choices()
    form.local_id.choices = _locais_choices()
    if form.validate_on_submit():
        svc = _pool_service()
        result = _handle_pool_op(
            lambda: svc.registrar_ajuste(
                tipo_garrafao_id=form.tipo_garrafao_id.data,
                quantidade=form.quantidade.data,
                local_id=form.local_id.data,
                estado=EstadoGarrafao(form.estado.data),
                validade=form.validade.data,
                sinal=form.sinal.data,
                observacao=form.observacao.data,
            ),
            success_msg=f"Ajuste registrado: {form.sinal.data:+d} × {form.quantidade.data}.",
        )
        if result is not None:
            return result
    return render_template("pool/movimento_ajuste.html", form=form)
