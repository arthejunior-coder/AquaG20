"""CLI Flask para operações administrativas que não têm rota web.

NO-TENANT-FILTER: estes comandos NÃO passam pela camada de Repository —
manipulam tenants/usuários diretamente. São o único caminho documentado
para criar o primeiro tenant + admin de um distribuidor.
"""

import click
from flask import Flask
from flask.cli import with_appcontext
from sqlalchemy import select

from app.auth.password import hash_password
from app.extensions import db
from app.models.tenant import PapelUsuario, PlanoTenant, Tenant, Usuario


@click.command("create-tenant")
@click.option("--razao", required=True, help="Razão social")
@click.option("--nome-fantasia", default=None)
@click.option("--cnpj", default=None)
@click.option(
    "--plano",
    type=click.Choice([p.value for p in PlanoTenant]),
    default=PlanoTenant.trial.value,
)
@click.option("--admin-nome", required=True)
@click.option("--admin-email", required=True)
@click.option(
    "--admin-senha",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Senha do admin (omita para prompt seguro)",
)
@with_appcontext
def create_tenant_cmd(razao, nome_fantasia, cnpj, plano, admin_nome, admin_email, admin_senha):
    """Cria um tenant + usuário admin inicial."""
    admin_email = admin_email.strip().lower()

    existing = db.session.scalar(select(Usuario).where(Usuario.email == admin_email))
    if existing:
        raise click.ClickException(f"Email {admin_email} já existe (id={existing.id}).")

    tenant = Tenant(
        razao_social=razao.strip(),
        nome_fantasia=nome_fantasia.strip() if nome_fantasia else None,
        cnpj=cnpj.strip() if cnpj else None,
        plano=PlanoTenant(plano),
    )
    db.session.add(tenant)
    db.session.flush()  # garante tenant.id antes de criar o admin

    admin = Usuario(
        tenant_id=tenant.id,
        nome=admin_nome.strip(),
        email=admin_email,
        senha_hash=hash_password(admin_senha),
        papel=PapelUsuario.admin,
    )
    db.session.add(admin)
    db.session.commit()

    click.echo(f"Tenant criado: id={tenant.id} razao={razao!r}")
    click.echo(f"Admin criado: id={admin.id} email={admin_email}")


@click.command("create-user")
@click.option("--tenant-id", type=int, required=True)
@click.option("--nome", required=True)
@click.option("--email", required=True)
@click.option(
    "--papel",
    type=click.Choice([p.value for p in PapelUsuario]),
    required=True,
)
@click.option(
    "--senha",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
)
@with_appcontext
def create_user_cmd(tenant_id, nome, email, papel, senha):
    """Cria um usuário em um tenant existente."""
    email = email.strip().lower()

    if not db.session.get(Tenant, tenant_id):
        raise click.ClickException(f"Tenant id={tenant_id} não existe.")

    existing = db.session.scalar(select(Usuario).where(Usuario.email == email))
    if existing:
        raise click.ClickException(f"Email {email} já existe (id={existing.id}).")

    user = Usuario(
        tenant_id=tenant_id,
        nome=nome.strip(),
        email=email,
        senha_hash=hash_password(senha),
        papel=PapelUsuario(papel),
    )
    db.session.add(user)
    db.session.commit()

    click.echo(f"Usuário criado: id={user.id} email={email} papel={papel}")


@click.command("list-tenants")
@with_appcontext
def list_tenants_cmd():
    """Lista todos os tenants e quantos usuários cada um tem."""
    from sqlalchemy import func

    stmt = (
        select(Tenant, func.count(Usuario.id).label("qtd_usuarios"))
        .outerjoin(Usuario, Usuario.tenant_id == Tenant.id)
        .group_by(Tenant.id)
        .order_by(Tenant.id)
    )
    rows = db.session.execute(stmt).all()
    if not rows:
        click.echo("Nenhum tenant cadastrado.")
        return
    click.echo(f"{'ID':>4}  {'PLANO':12}  {'USUÁRIOS':>9}  RAZÃO SOCIAL")
    for tenant, qtd in rows:
        click.echo(f"{tenant.id:>4}  {tenant.plano.value:12}  {qtd:>9}  {tenant.razao_social}")


def register_cli(app: Flask) -> None:
    app.cli.add_command(create_tenant_cmd)
    app.cli.add_command(create_user_cmd)
    app.cli.add_command(list_tenants_cmd)
