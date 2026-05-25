"""Rotas dos cadastros base (clientes, fornecedores, centros de custo).

Padrão de cada CRUD:
    GET  /cadastros/<entidade>           lista (full page; partial se HX-Request)
    GET  /cadastros/<entidade>/novo      form criar
    POST /cadastros/<entidade>/novo      cria + redirect lista
    GET  /cadastros/<entidade>/<id>      form editar (pré-preenchido)
    POST /cadastros/<entidade>/<id>      atualiza + redirect lista
    POST /cadastros/<entidade>/<id>/toggle  inverte `ativo` (soft delete)

Autorização: admin + gestor. Listagem aceita query string `?q=` para busca
por nome (e documento, quando aplicável).
"""

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import or_

from app.auth.decorators import papel_requerido, repo
from app.blueprints.cadastros import bp
from app.blueprints.cadastros.forms import (
    CentroCustoForm,
    ClienteForm,
    FornecedorForm,
    LocalEstoqueForm,
    TipoGarrafaoForm,
)
from app.extensions import db
from app.models.cadastros import CentroCusto, Cliente, Fornecedor
from app.models.frota import Veiculo
from app.models.pool import LocalEstoque, TipoGarrafao
from app.repositories.cadastros_repo import (
    CentroCustoRepository,
    ClienteRepository,
    FornecedorRepository,
)
from app.repositories.pool_repo import (
    LocalEstoqueRepository,
    TipoGarrafaoRepository,
)


def _hx() -> bool:
    """True se a requisição veio do HTMX."""
    return request.headers.get("HX-Request") == "true"


# ---------------------------------------------------------------------------
# Index — redireciona para a primeira aba (clientes)
# ---------------------------------------------------------------------------

@bp.route("/")
@papel_requerido("admin", "gestor")
def index():
    return redirect(url_for("cadastros.lista_clientes"))


# ===========================================================================
# CLIENTES
# ===========================================================================

@bp.route("/clientes")
@papel_requerido("admin", "gestor")
def lista_clientes():
    q = request.args.get("q", "").strip()
    r = repo(ClienteRepository)
    stmt = r.select().order_by(Cliente.ativo.desc(), Cliente.nome)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Cliente.nome.ilike(like), Cliente.documento.ilike(like)))
    clientes = r.all(stmt)
    template = (
        "cadastros/_tabela_clientes.html" if _hx() else "cadastros/clientes_lista.html"
    )
    return render_template(template, clientes=clientes, q=q)


@bp.route("/clientes/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_cliente():
    form = ClienteForm()
    if form.validate_on_submit():
        r = repo(ClienteRepository)
        r.add(**form.to_model_kwargs())
        db.session.commit()
        flash("Cliente criado.", "success")
        return redirect(url_for("cadastros.lista_clientes"))
    return render_template("cadastros/clientes_form.html", form=form, cliente=None)


@bp.route("/clientes/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def editar_cliente(id):
    r = repo(ClienteRepository)
    cliente = r.get(id)
    if cliente is None:
        abort(404)

    if request.method == "POST":
        form = ClienteForm()
        if form.validate_on_submit():
            for k, v in form.to_model_kwargs().items():
                setattr(cliente, k, v)
            db.session.commit()
            flash("Cliente atualizado.", "success")
            return redirect(url_for("cadastros.lista_clientes"))
    else:
        form = ClienteForm(obj=cliente, tipo=cliente.tipo.value)

    return render_template("cadastros/clientes_form.html", form=form, cliente=cliente)


@bp.route("/clientes/<int:id>/toggle", methods=["POST"])
@papel_requerido("admin", "gestor")
def toggle_cliente(id):
    r = repo(ClienteRepository)
    cliente = r.get(id)
    if cliente is None:
        abort(404)
    cliente.ativo = not cliente.ativo
    db.session.commit()
    flash(f"Cliente {'reativado' if cliente.ativo else 'desativado'}.", "info")
    return redirect(url_for("cadastros.lista_clientes"))


# ===========================================================================
# FORNECEDORES
# ===========================================================================

@bp.route("/fornecedores")
@papel_requerido("admin", "gestor")
def lista_fornecedores():
    q = request.args.get("q", "").strip()
    r = repo(FornecedorRepository)
    stmt = r.select().order_by(Fornecedor.nome)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Fornecedor.nome.ilike(like), Fornecedor.documento.ilike(like)))
    fornecedores = r.all(stmt)
    template = (
        "cadastros/_tabela_fornecedores.html" if _hx() else "cadastros/fornecedores_lista.html"
    )
    return render_template(template, fornecedores=fornecedores, q=q)


@bp.route("/fornecedores/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_fornecedor():
    form = FornecedorForm()
    if form.validate_on_submit():
        r = repo(FornecedorRepository)
        r.add(**form.to_model_kwargs())
        db.session.commit()
        flash("Fornecedor criado.", "success")
        return redirect(url_for("cadastros.lista_fornecedores"))
    return render_template("cadastros/fornecedores_form.html", form=form, fornecedor=None)


@bp.route("/fornecedores/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def editar_fornecedor(id):
    r = repo(FornecedorRepository)
    fornecedor = r.get(id)
    if fornecedor is None:
        abort(404)

    if request.method == "POST":
        form = FornecedorForm()
        if form.validate_on_submit():
            for k, v in form.to_model_kwargs().items():
                setattr(fornecedor, k, v)
            db.session.commit()
            flash("Fornecedor atualizado.", "success")
            return redirect(url_for("cadastros.lista_fornecedores"))
    else:
        form = FornecedorForm(obj=fornecedor)

    return render_template("cadastros/fornecedores_form.html", form=form, fornecedor=fornecedor)


# ===========================================================================
# CENTROS DE CUSTO
# ===========================================================================

@bp.route("/centros-custo")
@papel_requerido("admin", "gestor")
def lista_centros_custo():
    q = request.args.get("q", "").strip()
    r = repo(CentroCustoRepository)
    stmt = r.select().order_by(CentroCusto.ativo.desc(), CentroCusto.nome)
    if q:
        stmt = stmt.where(CentroCusto.nome.ilike(f"%{q}%"))
    centros = r.all(stmt)
    template = (
        "cadastros/_tabela_centros_custo.html" if _hx() else "cadastros/centros_custo_lista.html"
    )
    return render_template(template, centros=centros, q=q)


@bp.route("/centros-custo/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_centro_custo():
    form = CentroCustoForm()
    if form.validate_on_submit():
        r = repo(CentroCustoRepository)
        r.add(**form.to_model_kwargs())
        db.session.commit()
        flash("Centro de custo criado.", "success")
        return redirect(url_for("cadastros.lista_centros_custo"))
    return render_template("cadastros/centros_custo_form.html", form=form, centro=None)


@bp.route("/centros-custo/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def editar_centro_custo(id):
    r = repo(CentroCustoRepository)
    centro = r.get(id)
    if centro is None:
        abort(404)

    if request.method == "POST":
        form = CentroCustoForm()
        if form.validate_on_submit():
            for k, v in form.to_model_kwargs().items():
                setattr(centro, k, v)
            db.session.commit()
            flash("Centro de custo atualizado.", "success")
            return redirect(url_for("cadastros.lista_centros_custo"))
    else:
        form = CentroCustoForm(obj=centro, tipo=centro.tipo.value)

    return render_template("cadastros/centros_custo_form.html", form=form, centro=centro)


@bp.route("/centros-custo/<int:id>/toggle", methods=["POST"])
@papel_requerido("admin", "gestor")
def toggle_centro_custo(id):
    r = repo(CentroCustoRepository)
    centro = r.get(id)
    if centro is None:
        abort(404)
    centro.ativo = not centro.ativo
    db.session.commit()
    flash(f"Centro {'reativado' if centro.ativo else 'desativado'}.", "info")
    return redirect(url_for("cadastros.lista_centros_custo"))


# ===========================================================================
# TIPOS DE GARRAFÃO — catálogo de tipos (PC/PP/PET + capacidade)
# ===========================================================================

@bp.route("/tipos-garrafao")
@papel_requerido("admin", "gestor")
def lista_tipos_garrafao():
    q = request.args.get("q", "").strip()
    r = repo(TipoGarrafaoRepository)
    stmt = r.select().order_by(TipoGarrafao.ativo.desc(), TipoGarrafao.nome)
    if q:
        stmt = stmt.where(TipoGarrafao.nome.ilike(f"%{q}%"))
    tipos = r.all(stmt)
    template = "cadastros/_tabela_tipos_garrafao.html" if _hx() else "cadastros/tipos_garrafao_lista.html"
    return render_template(template, tipos=tipos, q=q)


@bp.route("/tipos-garrafao/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_tipo_garrafao():
    form = TipoGarrafaoForm()
    if form.validate_on_submit():
        r = repo(TipoGarrafaoRepository)
        r.add(**form.to_model_kwargs())
        db.session.commit()
        flash("Tipo de garrafão criado.", "success")
        return redirect(url_for("cadastros.lista_tipos_garrafao"))
    return render_template("cadastros/tipos_garrafao_form.html", form=form, tipo=None)


@bp.route("/tipos-garrafao/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def editar_tipo_garrafao(id):
    r = repo(TipoGarrafaoRepository)
    tipo = r.get(id)
    if tipo is None:
        abort(404)
    if request.method == "POST":
        form = TipoGarrafaoForm()
        if form.validate_on_submit():
            for k, v in form.to_model_kwargs().items():
                setattr(tipo, k, v)
            db.session.commit()
            flash("Tipo atualizado.", "success")
            return redirect(url_for("cadastros.lista_tipos_garrafao"))
    else:
        form = TipoGarrafaoForm(obj=tipo, material=tipo.material.value)
    return render_template("cadastros/tipos_garrafao_form.html", form=form, tipo=tipo)


@bp.route("/tipos-garrafao/<int:id>/toggle", methods=["POST"])
@papel_requerido("admin", "gestor")
def toggle_tipo_garrafao(id):
    r = repo(TipoGarrafaoRepository)
    tipo = r.get(id)
    if tipo is None:
        abort(404)
    tipo.ativo = not tipo.ativo
    db.session.commit()
    flash(f"Tipo {'reativado' if tipo.ativo else 'desativado'}.", "info")
    return redirect(url_for("cadastros.lista_tipos_garrafao"))


# ===========================================================================
# LOCAIS DE ESTOQUE — depósito, veículos, indústria, descarte
# ===========================================================================

class _VeiculoRepository:
    """Repo trivial só para o select de veículos."""
    pass


def _veiculos_choices():
    """Choices p/ select de veículo no LocalEstoqueForm."""
    from app.repositories.base import BaseRepository

    class _Repo(BaseRepository):
        model = Veiculo

    veiculos = repo(_Repo).all()
    return [("", "—")] + [
        (str(v.id), f"{v.tipo.value} · {v.placa or v.descricao or f'#{v.id}'}")
        for v in veiculos
    ]


@bp.route("/locais")
@papel_requerido("admin", "gestor")
def lista_locais():
    r = repo(LocalEstoqueRepository)
    locais = r.all(r.select().order_by(LocalEstoque.tipo, LocalEstoque.nome))
    template = "cadastros/_tabela_locais.html" if _hx() else "cadastros/locais_lista.html"
    return render_template(template, locais=locais, q="")


@bp.route("/locais/novo", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def novo_local():
    form = LocalEstoqueForm()
    form.veiculo_id.choices = _veiculos_choices()
    if form.validate_on_submit():
        r = repo(LocalEstoqueRepository)
        r.add(**form.to_model_kwargs())
        db.session.commit()
        flash("Local criado.", "success")
        return redirect(url_for("cadastros.lista_locais"))
    return render_template("cadastros/locais_form.html", form=form, local=None)


@bp.route("/locais/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin", "gestor")
def editar_local(id):
    r = repo(LocalEstoqueRepository)
    local = r.get(id)
    if local is None:
        abort(404)
    form = LocalEstoqueForm()
    form.veiculo_id.choices = _veiculos_choices()
    if request.method == "POST":
        if form.validate_on_submit():
            for k, v in form.to_model_kwargs().items():
                setattr(local, k, v)
            db.session.commit()
            flash("Local atualizado.", "success")
            return redirect(url_for("cadastros.lista_locais"))
    else:
        form = LocalEstoqueForm(obj=local, tipo=local.tipo.value,
                                 veiculo_id=str(local.veiculo_id) if local.veiculo_id else "")
        form.veiculo_id.choices = _veiculos_choices()
    return render_template("cadastros/locais_form.html", form=form, local=local)
