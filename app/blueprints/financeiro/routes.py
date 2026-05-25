"""Rotas do blueprint /financeiro.

Endpoints:
  GET  /financeiro                  — fluxo de caixa (totais + tabela mensal)
  GET  /financeiro/lancamentos      — lista filtrável (status, natureza)
  GET  /financeiro/lancamentos/novo — form
  POST /financeiro/lancamentos/novo — cria
  GET  /financeiro/lancamentos/<id> — form de edição
  POST /financeiro/lancamentos/<id> — atualiza
  POST /financeiro/lancamentos/<id>/pagar    — abre modal/form e marca pago
  POST /financeiro/lancamentos/<id>/cancelar — soft cancel
  POST /financeiro/lancamentos/<id>/reabrir  — reseta para pendente

Autorização: admin + financeiro.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for

from app.auth.decorators import papel_requerido, repo
from app.blueprints.financeiro import bp
from app.blueprints.financeiro.forms import LancamentoForm, PagarForm
from app.extensions import db
from app.models.cadastros import CentroCusto, Cliente, Fornecedor
from app.models.financeiro import (
    FormaLancamento,
    Lancamento,
    NaturezaLancamento,
    StatusLancamento,
)
from app.repositories.cadastros_repo import (
    CentroCustoRepository,
    ClienteRepository,
    FornecedorRepository,
)
from app.repositories.financeiro_repo import LancamentoRepository
from app.services.financeiro_service import (
    FinanceiroService,
    LancamentoInvalidoError,
)


def _hx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _svc() -> FinanceiroService:
    from flask_login import current_user
    return FinanceiroService(db.session, current_user.tenant_id)


def _populate_form_choices(form: LancamentoForm) -> None:
    r_cc = repo(CentroCustoRepository)
    r_cli = repo(ClienteRepository)
    r_for = repo(FornecedorRepository)
    form.centro_custo_id.choices = [("", "—")] + [
        (str(cc.id), cc.nome)
        for cc in r_cc.all(r_cc.select().where(CentroCusto.ativo == True)  # noqa: E712
                              .order_by(CentroCusto.nome))
    ]
    form.cliente_id.choices = [("", "—")] + [
        (str(c.id), c.nome)
        for c in r_cli.all(r_cli.select().where(Cliente.ativo == True)  # noqa: E712
                              .order_by(Cliente.nome))
    ]
    form.fornecedor_id.choices = [("", "—")] + [
        (str(f.id), f.nome)
        for f in r_for.all(r_for.select().order_by(Fornecedor.nome))
    ]


# ===========================================================================
# Dashboard de fluxo
# ===========================================================================


@bp.route("/")
@papel_requerido("admin", "financeiro")
def index():
    from datetime import timedelta
    hoje = date.today()
    # Janela default: 6 meses pra trás + 6 pra frente
    inicio = (hoje.replace(day=1) - timedelta(days=180)).replace(day=1)
    fim = (hoje.replace(day=1) + timedelta(days=210))

    svc = _svc()
    fluxos = svc.fluxo_mensal(inicio=inicio, fim=fim)
    totais_30 = svc.totais_pendentes_proximos_dias(dias=30)

    # Agrupa fluxos por (ano, mes) para facilitar a tabela
    meses_dict = {}
    for f in fluxos:
        key = (f.ano, f.mes)
        if key not in meses_dict:
            meses_dict[key] = {
                "ano": f.ano, "mes": f.mes,
                "receber_previsto": Decimal("0"), "receber_realizado": Decimal("0"),
                "pagar_previsto": Decimal("0"), "pagar_realizado": Decimal("0"),
            }
        if f.natureza == NaturezaLancamento.receber:
            meses_dict[key]["receber_previsto"] = f.previsto
            meses_dict[key]["receber_realizado"] = f.realizado
        else:
            meses_dict[key]["pagar_previsto"] = f.previsto
            meses_dict[key]["pagar_realizado"] = f.realizado

    meses = [meses_dict[k] for k in sorted(meses_dict.keys())]

    return render_template(
        "financeiro/index.html",
        meses=meses,
        totais_30=totais_30,
        receber_30=totais_30.get(NaturezaLancamento.receber, Decimal("0")),
        pagar_30=totais_30.get(NaturezaLancamento.pagar, Decimal("0")),
    )


# ===========================================================================
# Lista
# ===========================================================================


@bp.route("/lancamentos")
@papel_requerido("admin", "financeiro")
def lista_lancamentos():
    natureza = (request.args.get("natureza") or "").strip()
    status = (request.args.get("status") or "").strip()

    r = repo(LancamentoRepository)
    stmt = r.select().order_by(Lancamento.vencimento.desc(), Lancamento.id.desc())
    if natureza:
        try:
            stmt = stmt.where(Lancamento.natureza == NaturezaLancamento(natureza))
        except ValueError:
            natureza = ""
    if status:
        try:
            stmt = stmt.where(Lancamento.status == StatusLancamento(status))
        except ValueError:
            status = ""

    lancamentos = r.all(stmt.limit(200))
    clientes_by_id = {c.id: c for c in repo(ClienteRepository).all()}
    fornecedores_by_id = {f.id: f for f in repo(FornecedorRepository).all()}
    centros_by_id = {cc.id: cc for cc in repo(CentroCustoRepository).all()}

    naturezas = [(n.value, n.value.title()) for n in NaturezaLancamento]
    statuses = [(s.value, s.value.title()) for s in StatusLancamento]

    template = "financeiro/_tabela.html" if _hx() else "financeiro/lista.html"
    return render_template(
        template,
        lancamentos=lancamentos,
        clientes_by_id=clientes_by_id,
        fornecedores_by_id=fornecedores_by_id,
        centros_by_id=centros_by_id,
        naturezas=naturezas, statuses=statuses,
        natureza_filter=natureza, status_filter=status,
    )


# ===========================================================================
# Criar / editar
# ===========================================================================


def _form_kwargs(form: LancamentoForm) -> dict:
    return {
        "natureza": NaturezaLancamento(form.natureza.data),
        "descricao": form.descricao.data,
        "valor": form.valor.data,
        "vencimento": form.vencimento.data,
        "centro_custo_id": form.centro_custo_id.data,
        "cliente_id": form.cliente_id.data,
        "fornecedor_id": form.fornecedor_id.data,
        "pedido_id": form.parsed_pedido_id(),
        "forma": FormaLancamento(form.forma.data) if form.forma.data else None,
    }


@bp.route("/lancamentos/novo", methods=["GET", "POST"])
@papel_requerido("admin", "financeiro")
def novo_lancamento():
    form = LancamentoForm()
    _populate_form_choices(form)
    if form.validate_on_submit():
        try:
            _svc().criar(**_form_kwargs(form))
            db.session.commit()
            flash("Lançamento criado.", "success")
            return redirect(url_for("financeiro.lista_lancamentos"))
        except LancamentoInvalidoError as e:
            db.session.rollback()
            flash(f"Erro: {e}", "danger")
    return render_template(
        "financeiro/form.html", form=form, lancamento=None,
    )


@bp.route("/lancamentos/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "financeiro")
def editar_lancamento(id):
    lanc = repo(LancamentoRepository).get(id)
    if lanc is None:
        abort(404)
    form = LancamentoForm()
    _populate_form_choices(form)

    if request.method == "POST":
        if form.validate_on_submit():
            try:
                # Editar = update direto (não cria novo). Mas re-valida pela
                # mesma lógica do criar criando e descartando — ou aplica
                # validações inline. Para MVP, set direto + valida valor.
                if form.valor.data is None or form.valor.data <= 0:
                    raise LancamentoInvalidoError("valor deve ser > 0")
                lanc.natureza = NaturezaLancamento(form.natureza.data)
                lanc.descricao = form.descricao.data.strip()
                lanc.valor = form.valor.data
                lanc.vencimento = form.vencimento.data
                lanc.centro_custo_id = form.centro_custo_id.data
                lanc.cliente_id = form.cliente_id.data
                lanc.fornecedor_id = form.fornecedor_id.data
                lanc.pedido_id = form.parsed_pedido_id()
                lanc.forma = FormaLancamento(form.forma.data) if form.forma.data else None
                # Coerência natureza ↔ contraparte
                if lanc.natureza == NaturezaLancamento.receber and lanc.fornecedor_id:
                    raise LancamentoInvalidoError("receber não combina com fornecedor")
                if lanc.natureza == NaturezaLancamento.pagar and lanc.cliente_id:
                    raise LancamentoInvalidoError("pagar não combina com cliente")
                db.session.commit()
                flash("Lançamento atualizado.", "success")
                return redirect(url_for("financeiro.lista_lancamentos"))
            except LancamentoInvalidoError as e:
                db.session.rollback()
                flash(f"Erro: {e}", "danger")
    else:
        # Pré-preenche o form com valores atuais
        form.natureza.data = lanc.natureza.value
        form.descricao.data = lanc.descricao
        form.valor.data = lanc.valor
        form.vencimento.data = lanc.vencimento
        form.centro_custo_id.data = str(lanc.centro_custo_id) if lanc.centro_custo_id else ""
        form.cliente_id.data = str(lanc.cliente_id) if lanc.cliente_id else ""
        form.fornecedor_id.data = str(lanc.fornecedor_id) if lanc.fornecedor_id else ""
        form.pedido_id.data = str(lanc.pedido_id) if lanc.pedido_id else ""
        form.forma.data = lanc.forma.value if lanc.forma else ""

    return render_template("financeiro/form.html", form=form, lancamento=lanc)


# ===========================================================================
# Ações de pagamento
# ===========================================================================


@bp.route("/lancamentos/<int:id>/pagar", methods=["GET", "POST"])
@papel_requerido("admin", "financeiro")
def pagar_lancamento(id):
    lanc = repo(LancamentoRepository).get(id)
    if lanc is None:
        abort(404)

    form = PagarForm()
    if request.method == "POST":
        if form.validate_on_submit():
            try:
                _svc().marcar_pago(
                    lanc,
                    pago_em=form.pago_em.data,
                    valor_pago=form.valor_pago.data,
                    forma=FormaLancamento(form.forma.data) if form.forma.data else None,
                )
                db.session.commit()
                flash(f"Lançamento #{id} → {lanc.status.value}.", "success")
                return redirect(url_for("financeiro.lista_lancamentos"))
            except LancamentoInvalidoError as e:
                db.session.rollback()
                flash(f"Erro: {e}", "danger")
    else:
        form.pago_em.data = date.today()
        form.valor_pago.data = lanc.valor

    return render_template("financeiro/pagar.html", form=form, lancamento=lanc)


@bp.route("/lancamentos/<int:id>/cancelar", methods=["POST"])
@papel_requerido("admin", "financeiro")
def cancelar_lancamento(id):
    lanc = repo(LancamentoRepository).get(id)
    if lanc is None:
        abort(404)
    try:
        _svc().cancelar(lanc)
        db.session.commit()
        flash(f"Lançamento #{id} cancelado.", "info")
    except LancamentoInvalidoError as e:
        db.session.rollback()
        flash(f"Erro: {e}", "danger")
    return redirect(url_for("financeiro.lista_lancamentos"))


@bp.route("/lancamentos/<int:id>/reabrir", methods=["POST"])
@papel_requerido("admin", "financeiro")
def reabrir_lancamento(id):
    lanc = repo(LancamentoRepository).get(id)
    if lanc is None:
        abort(404)
    _svc().reabrir(lanc)
    db.session.commit()
    flash(f"Lançamento #{id} reaberto (pendente).", "info")
    return redirect(url_for("financeiro.lista_lancamentos"))
