"""Rotas do blueprint pedidos.

Endpoints:
    GET  /pedidos                  lista (filtrável por status, full / partial HTMX)
    GET  /pedidos/novo             form criar (com 1 linha vazia)
    POST /pedidos/novo             cria + redirect detalhe
    GET  /pedidos/<id>             detalhe (linhas, totais, ações de status)
    POST /pedidos/<id>/status      transição (status novo no body)
    POST /pedidos/<id>/cancelar    atalho cancelar

HTMX:
    GET  /pedidos/itens/nova-linha?idx=N
          → retorna um <tr> vazio com inputs `itens-N-*` para crescer
            dinamicamente a tabela no form de criação. Adicionar/remover
            linhas via HTMX sem reload.

Autorização: admin, gestor, atendimento (atendimento cria/lê; só admin/gestor
deve cancelar — mantido aqui no MVP como atendimento também por simplicidade).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.decorators import papel_requerido, repo
from app.blueprints.pedidos import bp
from app.blueprints.pedidos.forms import PedidoCabecalhoForm
from app.extensions import db
from app.models.cadastros import Cliente
from app.models.pedidos import (
    Pedido,
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import TipoGarrafao
from app.models.logistica import RotaParada
from app.validity import (
    GARRAFAO_VALIDADE_MAX_MESES,
    _add_meses,
    primeiro_dia_do_mes,
)


@bp.context_processor
def _injeta_janela_validade():
    """Calcula min/max de validade aceita pra preencher attrs HTML do <input
    type="month">. Disponível em TODOS os templates do bp pedidos sem precisar
    passar em cada render_template.
    """
    hoje = date.today()
    minimo = primeiro_dia_do_mes(hoje)
    maximo = _add_meses(minimo, GARRAFAO_VALIDADE_MAX_MESES)
    return {
        "validade_min_iso": minimo.strftime("%Y-%m"),  # formato do input type=month
        "validade_max_iso": maximo.strftime("%Y-%m"),
    }
from app.models.pool import LocalEstoque, TipoLocal
from app.repositories.cadastros_repo import ClienteRepository
from app.repositories.pedido_repo import PedidoRepository, PermutaRepository
from app.repositories.pool_repo import LocalEstoqueRepository, TipoGarrafaoRepository
from app.repositories.rota_repo import RotaParadaRepository
from app.services.pedido_service import (
    ItemPedidoInput,
    PedidoInvalidoError,
    PedidoService,
    TransicaoInvalidaError,
)
from app.services.permuta_service import (
    EntregaInvalidaError,
    LinhaEntregaInput,
    PermutaService,
)
from app.services.pool_service import EstoqueInsuficienteError, InvariantePoolViolada
from app.services.rota_service import RotaInvalidaError, RotaService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _pedido_service() -> PedidoService:
    return PedidoService(db.session, current_user.tenant_id, current_user.id)


def _permuta_service() -> PermutaService:
    return PermutaService(db.session, current_user.tenant_id, current_user.id)


def _clientes_choices() -> list[tuple[int, str]]:
    r = repo(ClienteRepository)
    stmt = r.select().where(Cliente.ativo == True).order_by(Cliente.nome)  # noqa: E712
    return [(c.id, f"{c.nome}" + (f" ({c.tipo.value})" if c.tipo else "")) for c in r.all(stmt)]


def _tipos_garrafao_for_select() -> list[TipoGarrafao]:
    r = repo(TipoGarrafaoRepository)
    return r.all(r.select().where(TipoGarrafao.ativo == True)  # noqa: E712
                 .order_by(TipoGarrafao.nome))


def _parse_data(valor: str | None) -> date | None:
    """Aceita 'YYYY-MM' (input type=month) ou 'YYYY-MM-DD' (legado).

    Validade de garrafão é granularidade mês — internamente dia=01.
    """
    if not valor:
        return None
    valor = valor.strip()
    if not valor:
        return None
    # input type="month" envia "YYYY-MM"
    if len(valor) == 7 and valor[4] == "-":
        try:
            return datetime.strptime(valor, "%Y-%m").date().replace(day=1)
        except ValueError:
            raise PedidoInvalidoError(f"data inválida: {valor!r} (use YYYY-MM)")
    # Fallback: YYYY-MM-DD (compat com forms antigos)
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        raise PedidoInvalidoError(f"data inválida: {valor!r} (use YYYY-MM)")


def _parse_decimal(valor: str | None) -> Decimal | None:
    """Wrapper de parse_money que mapeia ValueError → PedidoInvalidoError."""
    from app.money import parse_money
    try:
        return parse_money(valor)
    except ValueError as exc:
        raise PedidoInvalidoError(f"preço inválido: {valor!r}") from exc


def parse_itens_input(form_data) -> list[ItemPedidoInput]:
    """Lê os campos `itens-N-*` do request.form e devolve ItemPedidoInput[].

    Cada linha precisa de tipo_garrafao_id e quantidade > 0. Linhas
    totalmente vazias são ignoradas (UX: usuário apagou no browser sem
    remover o <tr>). Levanta PedidoInvalidoError em campos inválidos.
    """
    # Descobre os índices presentes
    indices = set()
    for key in form_data.keys():
        if key.startswith("itens-") and key.endswith("-tipo_garrafao_id"):
            try:
                indices.add(int(key.split("-")[1]))
            except (ValueError, IndexError):
                continue

    itens: list[ItemPedidoInput] = []
    for i in sorted(indices):
        tipo_raw = (form_data.get(f"itens-{i}-tipo_garrafao_id") or "").strip()
        qtd_raw = (form_data.get(f"itens-{i}-quantidade") or "").strip()
        val_raw = (form_data.get(f"itens-{i}-validade_solicitada") or "").strip()
        preco_raw = (form_data.get(f"itens-{i}-preco_unitario") or "").strip()

        # Linha 100% vazia → ignora
        if not (tipo_raw or qtd_raw or val_raw or preco_raw):
            continue

        if not tipo_raw:
            raise PedidoInvalidoError(f"linha {i + 1}: escolha o tipo de garrafão.")
        if not qtd_raw:
            raise PedidoInvalidoError(f"linha {i + 1}: informe a quantidade.")
        try:
            tipo_id = int(tipo_raw)
            qtd = int(qtd_raw)
        except ValueError:
            raise PedidoInvalidoError(f"linha {i + 1}: tipo/quantidade inválidos.")

        itens.append(ItemPedidoInput(
            tipo_garrafao_id=tipo_id,
            quantidade=qtd,
            validade_solicitada=_parse_data(val_raw),
            preco_unitario=_parse_decimal(preco_raw),
        ))
    return itens


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------


@bp.route("/")
@papel_requerido("admin", "gestor", "atendimento")
def lista():
    status_filter = (request.args.get("status") or "").strip()
    r = repo(PedidoRepository)
    stmt = r.select().order_by(Pedido.criado_em.desc(), Pedido.id.desc())
    if status_filter:
        try:
            stmt = stmt.where(Pedido.status == StatusPedido(status_filter))
        except ValueError:
            status_filter = ""  # ignora silenciosamente
    pedidos = r.all(stmt.limit(200))

    clientes_by_id = {c.id: c for c in repo(ClienteRepository).all()}
    statuses = [(s.value, s.value.replace("_", " ").title()) for s in StatusPedido]

    template = "pedidos/_tabela.html" if _hx() else "pedidos/lista.html"
    return render_template(
        template,
        pedidos=pedidos,
        clientes_by_id=clientes_by_id,
        statuses=statuses,
        status_filter=status_filter,
    )


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------


@bp.route("/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor", "atendimento")
def novo():
    form = PedidoCabecalhoForm()
    form.cliente_id.choices = _clientes_choices()
    tipos = _tipos_garrafao_for_select()

    if not form.cliente_id.choices:
        flash("Cadastre um cliente ativo antes de criar pedidos.", "warning")
    if not tipos:
        flash("Cadastre pelo menos um tipo de garrafão ativo.", "warning")

    if form.validate_on_submit():
        try:
            itens = parse_itens_input(request.form)
            svc = _pedido_service()
            pedido = svc.criar_pedido(itens=itens, **form.to_header_kwargs())
            db.session.commit()
            flash(f"Pedido #{pedido.id} criado.", "success")
            return redirect(url_for("pedidos.detalhe", id=pedido.id))
        except PedidoInvalidoError as e:
            db.session.rollback()
            flash(f"Erro: {e}", "danger")

    # Submetido com erro → preserva linhas digitadas; senão começa 1 linha vazia
    linhas_preservadas = _linhas_para_template(request.form) if request.method == "POST" else None

    return render_template(
        "pedidos/form.html",
        form=form,
        tipos=tipos,
        linhas_preservadas=linhas_preservadas,
    )


def _linhas_para_template(form_data) -> list[dict]:
    """Retorna lista de dicts {idx, tipo, qtd, validade, preco} para
    re-renderizar o form após erro de validação, preservando o que o
    usuário digitou."""
    indices = set()
    for key in form_data.keys():
        if key.startswith("itens-") and "-" in key[6:]:
            try:
                indices.add(int(key.split("-")[1]))
            except (ValueError, IndexError):
                continue
    out = []
    for i in sorted(indices):
        # input type="month" envia/preserva YYYY-MM — só normaliza por garantia
        val = (form_data.get(f"itens-{i}-validade_solicitada") or "").strip()
        if len(val) > 7:  # veio YYYY-MM-DD por algum motivo, trunca
            val = val[:7]
        out.append({
            "idx": i,
            "tipo_garrafao_id": form_data.get(f"itens-{i}-tipo_garrafao_id", ""),
            "quantidade": form_data.get(f"itens-{i}-quantidade", ""),
            "validade_solicitada": val,
            "preco_unitario": form_data.get(f"itens-{i}-preco_unitario", ""),
        })
    return out or [{"idx": 0, "tipo_garrafao_id": "", "quantidade": "",
                    "validade_solicitada": "", "preco_unitario": ""}]


@bp.route("/itens/nova-linha")
@papel_requerido("admin", "gestor", "atendimento")
def nova_linha():
    """Devolve HTML de 1 <tr> vazio para adicionar via HTMX.

    Query: ?idx=N — o N é o índice da nova linha (controlado pelo JS).
    """
    try:
        idx = int(request.args.get("idx", "0"))
    except ValueError:
        idx = 0
    return render_template(
        "pedidos/_linha_item.html",
        tipos=_tipos_garrafao_for_select(),
        linha={"idx": idx, "tipo_garrafao_id": "", "quantidade": "",
               "validade_solicitada": "", "preco_unitario": ""},
    )


# ---------------------------------------------------------------------------
# Detalhe + transições
# ---------------------------------------------------------------------------


@bp.route("/<int:id>")
@papel_requerido("admin", "gestor", "atendimento")
def detalhe(id):
    pedido = repo(PedidoRepository).get(id)
    if pedido is None:
        abort(404)

    cliente = repo(ClienteRepository).get(pedido.cliente_id)
    tipos_by_id = {t.id: t for t in repo(TipoGarrafaoRepository).all()}

    return render_template(
        "pedidos/detalhe.html",
        pedido=pedido,
        cliente=cliente,
        tipos_by_id=tipos_by_id,
    )


@bp.route("/<int:id>/status", methods=["POST"])
@papel_requerido("admin", "gestor", "atendimento")
def transicionar_status(id):
    pedido = repo(PedidoRepository).get(id)
    if pedido is None:
        abort(404)
    novo_str = (request.form.get("novo") or "").strip()
    try:
        novo = StatusPedido(novo_str)
    except ValueError:
        flash(f"Status inválido: {novo_str!r}", "danger")
        return redirect(url_for("pedidos.detalhe", id=id))

    try:
        _pedido_service().transicionar(pedido, novo)
        db.session.commit()
        flash(f"Pedido #{id} → {novo.value.replace('_', ' ')}.", "success")
    except TransicaoInvalidaError as e:
        db.session.rollback()
        flash(f"Transição não permitida: {e}", "danger")
    return redirect(url_for("pedidos.detalhe", id=id))


@bp.route("/<int:id>/cancelar", methods=["POST"])
@papel_requerido("admin", "gestor")
def cancelar(id):
    pedido = repo(PedidoRepository).get(id)
    if pedido is None:
        abort(404)
    try:
        _pedido_service().cancelar(pedido)
        db.session.commit()
        flash(f"Pedido #{id} cancelado.", "info")
    except TransicaoInvalidaError as e:
        db.session.rollback()
        flash(f"Não foi possível cancelar: {e}", "danger")
    return redirect(url_for("pedidos.detalhe", id=id))


# ---------------------------------------------------------------------------
# Tela de entrega — coração do passo 16
# ---------------------------------------------------------------------------


def _veiculos_choices() -> list[tuple[int, str]]:
    r = repo(LocalEstoqueRepository)
    stmt = (
        r.select()
        .where(LocalEstoque.tipo == TipoLocal.veiculo)
        .order_by(LocalEstoque.nome)
    )
    return [(l.id, l.nome) for l in r.all(stmt)]


def _parse_linhas_entrega(form_data) -> list[LinhaEntregaInput]:
    """Lê linhas-N-* do form e devolve LinhaEntregaInput[]. Linhas com
    quantidade 0/vazia são silenciosamente ignoradas (cliente desistiu
    de algum item na entrega)."""
    indices = set()
    for key in form_data.keys():
        if key.startswith("linhas-") and key.endswith("-quantidade"):
            try:
                indices.add(int(key.split("-")[1]))
            except (ValueError, IndexError):
                continue

    linhas: list[LinhaEntregaInput] = []
    for i in sorted(indices):
        qtd_raw = (form_data.get(f"linhas-{i}-quantidade") or "").strip()
        tipo_raw = (form_data.get(f"linhas-{i}-tipo_garrafao_id") or "").strip()
        val_e_raw = (form_data.get(f"linhas-{i}-validade_entregue") or "").strip()
        val_r_raw = (form_data.get(f"linhas-{i}-validade_recebida") or "").strip()

        if not qtd_raw or qtd_raw == "0":
            continue
        if not tipo_raw or not val_e_raw:
            raise EntregaInvalidaError(
                f"linha {i + 1}: tipo e validade entregue obrigatórios."
            )
        try:
            qtd = int(qtd_raw)
            tipo_id = int(tipo_raw)
        except ValueError:
            raise EntregaInvalidaError(f"linha {i + 1}: tipo/quantidade inválidos.")

        try:
            val_e = datetime.strptime(val_e_raw, "%Y-%m-%d").date()
        except ValueError:
            raise EntregaInvalidaError(f"linha {i + 1}: validade entregue inválida.")
        val_r = None
        if val_r_raw:
            try:
                val_r = datetime.strptime(val_r_raw, "%Y-%m-%d").date()
            except ValueError:
                raise EntregaInvalidaError(f"linha {i + 1}: validade recebida inválida.")

        linhas.append(LinhaEntregaInput(
            tipo_garrafao_id=tipo_id,
            quantidade=qtd,
            validade_entregue=val_e,
            validade_recebida=val_r,
        ))
    return linhas


@bp.route("/<int:id>/entregar", methods=["GET", "POST"])
@papel_requerido("admin", "gestor", "atendimento")
def entregar(id):
    pedido = repo(PedidoRepository).get(id)
    if pedido is None:
        abort(404)
    if pedido.status not in (StatusPedido.roteirizado, StatusPedido.em_entrega):
        flash(
            f"Entrega requer status 'roteirizado' ou 'em_entrega'; "
            f"pedido está em '{pedido.status.value}'.",
            "warning",
        )
        return redirect(url_for("pedidos.detalhe", id=id))

    veiculos = _veiculos_choices()
    tipos_by_id = {t.id: t for t in repo(TipoGarrafaoRepository).all()}
    cliente = repo(ClienteRepository).get(pedido.cliente_id)

    # parada_id opcional via query OU form — quando vier de uma rota
    parada = _carregar_parada_da_request(pedido.id)

    if request.method == "POST":
        try:
            veiculo_id = int(request.form.get("veiculo_local_id") or 0)
            if veiculo_id == 0:
                raise EntregaInvalidaError("escolha um veículo.")
            desbalanco = int(request.form.get("desbalanco_garrafoes") or 0)
            obs = (request.form.get("observacao") or "").strip() or None
            linhas = _parse_linhas_entrega(request.form)
            if not linhas:
                raise EntregaInvalidaError("informe pelo menos 1 linha com quantidade > 0.")

            svc = _permuta_service()
            svc.registrar_entrega(
                pedido=pedido, veiculo_local_id=veiculo_id,
                linhas=linhas, desbalanco_garrafoes=desbalanco,
                observacao=obs,
                parada_id=parada.id if parada else None,
            )

            # Orquestração de status: roteirizado → em_entrega → entregue
            ped_svc = _pedido_service()
            if pedido.status == StatusPedido.roteirizado:
                ped_svc.transicionar(pedido, StatusPedido.em_entrega)
            ped_svc.transicionar(pedido, StatusPedido.entregue)

            # Se entrega vem de rota: marca parada como entregue
            if parada is not None:
                qtd_entregue_total = sum(l.quantidade for l in linhas)
                RotaService(db.session, current_user.tenant_id).marcar_parada_entregue(
                    parada,
                    qtd_entregue=qtd_entregue_total,
                    qtd_recolhido=qtd_entregue_total - desbalanco,
                )

            db.session.commit()
            flash(f"Entrega do pedido #{id} registrada.", "success")
            if parada is not None:
                return redirect(url_for("rotas.detalhe", id=parada.rota_id))
            return redirect(url_for("pedidos.detalhe", id=id))

        except (EntregaInvalidaError, EstoqueInsuficienteError,
                InvariantePoolViolada, RotaInvalidaError, ValueError) as e:
            db.session.rollback()
            flash(f"Erro: {e}", "danger")
        except TransicaoInvalidaError as e:
            db.session.rollback()
            flash(f"Erro de status: {e}", "danger")

    # Pre-popula linhas com os itens do pedido
    linhas_pre = [
        {
            "idx": i,
            "tipo_garrafao_id": item.tipo_garrafao_id,
            "tipo_nome": (tipos_by_id.get(item.tipo_garrafao_id).nome
                          if tipos_by_id.get(item.tipo_garrafao_id) else f"#{item.tipo_garrafao_id}"),
            "quantidade_pedida": item.qtd_solicitada,
            "quantidade": item.qtd_solicitada - (item.qtd_atendida or 0),
            # input type="month" espera YYYY-MM, não YYYY-MM-DD
            "validade_entregue": (item.validade_solicitada.strftime("%Y-%m")
                                  if item.validade_solicitada else ""),
            "validade_recebida": "",  # default = igual à entregue (operador edita)
        }
        for i, item in enumerate(pedido.itens)
    ]

    return render_template(
        "pedidos/entregar.html",
        pedido=pedido, cliente=cliente,
        veiculos=veiculos, tipos_by_id=tipos_by_id,
        linhas_pre=linhas_pre,
        parada=parada,
    )


def _carregar_parada_da_request(pedido_id: int) -> "RotaParada | None":
    """Lê `parada_id` de query (GET) ou form (POST) e devolve a RotaParada
    se válida (tenant + pertence ao pedido). Caso contrário, None — entrega
    continua sem vínculo de rota, sem quebrar."""
    raw = request.args.get("parada_id") or request.form.get("parada_id")
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    parada = repo(RotaParadaRepository).get(pid)
    if parada is None or parada.pedido_id != pedido_id:
        return None
    return parada


# ---------------------------------------------------------------------------
# Permutas — histórico simples (KPI casado/concessão vem no passo 18)
# ---------------------------------------------------------------------------


@bp.route("/permutas")
@papel_requerido("admin", "gestor", "atendimento")
def lista_permutas():
    """Histórico das permutas registradas (últimas 200).

    Página simples — o dashboard com KPIs agregados vem no passo 18.
    Aqui é útil para a operação conferir o que foi registrado.
    """
    from app.models.pedidos import Permuta

    r = repo(PermutaRepository)
    stmt = r.select().order_by(Permuta.criado_em.desc(), Permuta.id.desc()).limit(200)
    permutas = r.all(stmt)
    tipos_by_id = {t.id: t for t in repo(TipoGarrafaoRepository).all()}
    clientes_by_id = {c.id: c for c in repo(ClienteRepository).all()}

    return render_template(
        "pedidos/permutas.html",
        permutas=permutas, tipos_by_id=tipos_by_id, clientes_by_id=clientes_by_id,
    )
