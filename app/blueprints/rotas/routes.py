"""Blueprint /rotas — planejamento + execução de rotas de entrega.

Endpoints:
  GET  /rotas                         lista filtr por status / data
  GET  /rotas/nova                    form cabeçalho
  POST /rotas/nova                    cria rota
  GET  /rotas/<id>                    detalhe (cabeçalho + paradas)
  POST /rotas/<id>                    edita cabeçalho (só planejada)
  POST /rotas/<id>/paradas            adiciona parada (pedido_id no body)
  POST /rotas/<id>/paradas/<pid>/remover  remove parada
  POST /rotas/<id>/iniciar            planejada → em_andamento
  POST /rotas/<id>/concluir           em_andamento → concluida
  POST /rotas/<id>/cancelar           planejada/em_andamento → cancelada
  POST /rotas/<id>/paradas/<pid>/falhar  marca parada como falhou

Autorização: admin, gestor, atendimento (admin/gestor podem editar; atendimento
deveria ser leitura mas mantemos amplo p/ MVP — refinar se necessário).
"""

from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.auth.decorators import papel_requerido, repo
from app.blueprints.rotas import bp
from app.blueprints.rotas.forms import RotaForm
from app.extensions import db
from app.models.cadastros import Cliente
from app.models.frota import Entregador, Veiculo
from app.models.logistica import Rota, RotaParada, StatusRota
from app.models.pedidos import Pedido, StatusPedido
from app.models.pool import TipoGarrafao
from app.repositories.cadastros_repo import ClienteRepository
from app.repositories.pedido_repo import PedidoRepository
from app.repositories.pool_repo import TipoGarrafaoRepository
from app.repositories.rota_repo import RotaParadaRepository, RotaRepository
from app.services.rota_service import RotaInvalidaError, RotaService


def _hx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _svc() -> RotaService:
    from flask_login import current_user
    return RotaService(db.session, current_user.tenant_id)


def _veiculos_choices() -> list[tuple]:
    """Choices para SelectField — coerce=_none_or_int aceita "" → None."""
    # NO-TENANT-FILTER: repository já filtra
    from app.repositories.base import BaseRepository

    class _R(BaseRepository):
        model = Veiculo

    r = repo(_R)
    return [("", "—")] + [
        (v.id, f"{v.tipo.value} · {v.placa or v.descricao or f'#{v.id}'}")
        for v in r.all(r.select().where(Veiculo.ativo == True).order_by(Veiculo.placa))  # noqa: E712
    ]


def _entregadores_choices() -> list[tuple]:
    from app.repositories.base import BaseRepository

    class _R(BaseRepository):
        model = Entregador

    r = repo(_R)
    return [("", "—")] + [
        (e.id, e.nome)
        for e in r.all(r.select().where(Entregador.ativo == True)  # noqa: E712
                          .order_by(Entregador.nome))
    ]


# ---------------------------------------------------------------------------
# Lista
# ---------------------------------------------------------------------------


@bp.route("/")
@papel_requerido("admin", "gestor", "atendimento")
def lista():
    status_filter = (request.args.get("status") or "").strip()
    data_filter = (request.args.get("data") or "").strip()

    r = repo(RotaRepository)
    stmt = r.select().order_by(Rota.data_rota.desc(), Rota.id.desc())
    if status_filter:
        try:
            stmt = stmt.where(Rota.status == StatusRota(status_filter))
        except ValueError:
            status_filter = ""
    if data_filter:
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(data_filter, "%Y-%m-%d").date()
            stmt = stmt.where(Rota.data_rota == d)
        except ValueError:
            data_filter = ""

    rotas = r.all(stmt.limit(200))
    veiculos_by_id = {v.id: v for v in db.session.scalars(
        select(Veiculo).where(Veiculo.tenant_id == _tenant_id())
    )}
    entregadores_by_id = {e.id: e for e in db.session.scalars(
        select(Entregador).where(Entregador.tenant_id == _tenant_id())
    )}

    # Conta paradas por rota
    paradas_count: dict[int, int] = {}
    if rotas:
        from sqlalchemy import func
        rota_ids = [r.id for r in rotas]
        # NO-TENANT-FILTER: rota_ids já vieram do repo filtrado por tenant
        rows = db.session.execute(
            select(RotaParada.rota_id,
                   func.count(RotaParada.id).label("c"))  # NO-TENANT-FILTER
            .where(RotaParada.rota_id.in_(rota_ids))
            .group_by(RotaParada.rota_id)
        ).all()
        paradas_count = {row.rota_id: int(row.c) for row in rows}

    statuses = [(s.value, s.value.replace("_", " ").title()) for s in StatusRota]
    template = "rotas/_tabela.html" if _hx() else "rotas/lista.html"
    return render_template(
        template,
        rotas=rotas,
        veiculos_by_id=veiculos_by_id,
        entregadores_by_id=entregadores_by_id,
        paradas_count=paradas_count,
        statuses=statuses,
        status_filter=status_filter, data_filter=data_filter,
    )


def _tenant_id() -> int:
    from flask_login import current_user
    return current_user.tenant_id


# ---------------------------------------------------------------------------
# Cria / edita
# ---------------------------------------------------------------------------


@bp.route("/nova", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def nova():
    form = RotaForm()
    form.veiculo_id.choices = _veiculos_choices()
    form.entregador_id.choices = _entregadores_choices()
    if not form.data_rota.data:
        form.data_rota.data = date.today()

    if form.validate_on_submit():
        try:
            rota = _svc().criar_rota(
                data_rota=form.data_rota.data,
                veiculo_id=form.veiculo_id.data,
                entregador_id=form.entregador_id.data,
            )
            db.session.commit()
            flash(f"Rota #{rota.id} criada.", "success")
            return redirect(url_for("rotas.detalhe", id=rota.id))
        except RotaInvalidaError as e:
            db.session.rollback()
            flash(f"Erro: {e}", "danger")

    return render_template("rotas/form.html", form=form, rota=None)


@bp.route("/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor", "atendimento")
def detalhe(id):
    rota = repo(RotaRepository).get(id)
    if rota is None:
        abort(404)

    # Form de edição do cabeçalho (apenas se planejada)
    form = RotaForm()
    form.veiculo_id.choices = _veiculos_choices()
    form.entregador_id.choices = _entregadores_choices()

    if request.method == "POST":
        if rota.status != StatusRota.planejada:
            flash("Rota não editável fora de 'planejada'.", "warning")
            return redirect(url_for("rotas.detalhe", id=id))
        if form.validate_on_submit():
            try:
                _svc().editar_cabecalho(
                    rota,
                    data_rota=form.data_rota.data,
                    veiculo_id=form.veiculo_id.data,
                    entregador_id=form.entregador_id.data,
                )
                db.session.commit()
                flash("Rota atualizada.", "success")
                return redirect(url_for("rotas.detalhe", id=id))
            except RotaInvalidaError as e:
                db.session.rollback()
                flash(f"Erro: {e}", "danger")
    else:
        form.data_rota.data = rota.data_rota
        form.veiculo_id.data = rota.veiculo_id
        form.entregador_id.data = rota.entregador_id

    # NO-TENANT-FILTER: rota.id já é do tenant
    paradas = db.session.scalars(
        select(RotaParada)  # NO-TENANT-FILTER
        .where(RotaParada.rota_id == rota.id)
        .order_by(RotaParada.ordem, RotaParada.id)
    ).all()

    # Mapas auxiliares
    pedidos_by_id = {p.id: p for p in repo(PedidoRepository).all()}
    clientes_by_id = {c.id: c for c in repo(ClienteRepository).all()}
    veiculo = db.session.get(Veiculo, rota.veiculo_id) if rota.veiculo_id else None
    entregador = (db.session.get(Entregador, rota.entregador_id)
                  if rota.entregador_id else None)

    # Pedidos disponíveis pra adicionar (aberto ou roteirizado e do tenant)
    # — só interessa se rota está planejada
    pedidos_disponiveis = []
    if rota.status == StatusRota.planejada:
        # IDs já em paradas para não oferecer duplicado
        ja_paradas = {p.pedido_id for p in paradas}
        # Pedidos do tenant em aberto/roteirizado
        rped = repo(PedidoRepository)
        stmt = rped.select().where(
            Pedido.status.in_([StatusPedido.aberto, StatusPedido.roteirizado])
        ).order_by(Pedido.criado_em.desc()).limit(100)
        candidatos = rped.all(stmt)
        pedidos_disponiveis = [p for p in candidatos if p.id not in ja_paradas]

    return render_template(
        "rotas/detalhe.html",
        rota=rota, form=form,
        paradas=paradas,
        pedidos_by_id=pedidos_by_id,
        clientes_by_id=clientes_by_id,
        veiculo=veiculo, entregador=entregador,
        pedidos_disponiveis=pedidos_disponiveis,
    )


# ---------------------------------------------------------------------------
# Paradas
# ---------------------------------------------------------------------------


@bp.route("/<int:id>/paradas", methods=["POST"])
@papel_requerido("admin", "gestor")
def adicionar_parada(id):
    rota = repo(RotaRepository).get(id)
    if rota is None:
        abort(404)
    try:
        pedido_id = int(request.form.get("pedido_id") or 0)
        if pedido_id <= 0:
            raise RotaInvalidaError("pedido_id inválido")
        _svc().adicionar_parada(rota, pedido_id=pedido_id)
        db.session.commit()
        flash(f"Pedido #{pedido_id} adicionado à rota.", "success")
    except RotaInvalidaError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("rotas.detalhe", id=id))


@bp.route("/<int:id>/paradas/<int:pid>/remover", methods=["POST"])
@papel_requerido("admin", "gestor")
def remover_parada(id, pid):
    rota = repo(RotaRepository).get(id)
    parada = repo(RotaParadaRepository).get(pid)
    if rota is None or parada is None or parada.rota_id != id:
        abort(404)
    try:
        _svc().remover_parada(parada)
        db.session.commit()
        flash("Parada removida.", "info")
    except RotaInvalidaError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("rotas.detalhe", id=id))


@bp.route("/<int:id>/paradas/<int:pid>/falhar", methods=["POST"])
@papel_requerido("admin", "gestor", "atendimento")
def falhar_parada(id, pid):
    parada = repo(RotaParadaRepository).get(pid)
    if parada is None or parada.rota_id != id:
        abort(404)
    _svc().marcar_parada_falhou(parada)
    db.session.commit()
    flash("Parada marcada como falhou.", "warning")
    return redirect(url_for("rotas.detalhe", id=id))


# ---------------------------------------------------------------------------
# Transições
# ---------------------------------------------------------------------------


@bp.route("/<int:id>/iniciar", methods=["POST"])
@papel_requerido("admin", "gestor")
def iniciar(id):
    rota = repo(RotaRepository).get(id)
    if rota is None:
        abort(404)
    try:
        _svc().iniciar(rota)
        db.session.commit()
        flash(f"Rota #{id} iniciada.", "success")
    except RotaInvalidaError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("rotas.detalhe", id=id))


@bp.route("/<int:id>/concluir", methods=["POST"])
@papel_requerido("admin", "gestor")
def concluir(id):
    rota = repo(RotaRepository).get(id)
    if rota is None:
        abort(404)
    try:
        _svc().concluir(rota)
        db.session.commit()
        flash(f"Rota #{id} concluída.", "success")
    except RotaInvalidaError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("rotas.detalhe", id=id))


@bp.route("/<int:id>/cancelar", methods=["POST"])
@papel_requerido("admin", "gestor")
def cancelar(id):
    rota = repo(RotaRepository).get(id)
    if rota is None:
        abort(404)
    try:
        _svc().cancelar(rota)
        db.session.commit()
        flash(f"Rota #{id} cancelada.", "info")
    except RotaInvalidaError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("rotas.detalhe", id=id))
