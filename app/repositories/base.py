"""Base de todos os repositories da aplicação.

A regra de ouro do projeto: TODA query da aplicação filtra por `tenant_id`.
Este BaseRepository é o ponto onde essa regra vira código — e não convenção.
Subclasses só declaram `model = SeuModel`; nada mais é necessário para que
o filtro seja aplicado.

NUNCA fazer `db.session.execute(select(SeuModel))` direto em blueprints ou
services. Use o repositório, sempre obtido via `repo(SeuRepository)` do
módulo `app.auth.decorators` (que injeta o tenant da sessão Flask).

Para operações verdadeiramente sem filtro de tenant (criar tenant inicial,
super-admin), use `db.session` diretamente em `scripts/` ou `app/cli.py`
com o marcador de comentário `# NO-TENANT-FILTER`.
"""

from sqlalchemy import func, select

from app.extensions import db


class BaseRepository:
    """Repositório base com filtro automático por tenant_id.

    Uso:
        class ClienteRepository(BaseRepository):
            model = Cliente

        repo = ClienteRepository(db.session, current_user.tenant_id)
        ativos = repo.all(repo.select().where(Cliente.ativo == True))
    """

    model = None  # subclasse define

    def __init__(self, session, tenant_id: int):
        if self.model is None:
            raise NotImplementedError(
                f"{type(self).__name__}.model precisa apontar para um SQLAlchemy model."
            )
        self.session = session
        self.tenant_id = tenant_id

    # ---- construção de query ----

    def select(self):
        """Retorna um `select()` SQLAlchemy 2 já filtrado por tenant_id.

        Estenda com `.where(...)`, `.order_by(...)`, etc, e execute via
        `repo.all(stmt)` ou `repo.first(stmt)`.
        """
        return select(self.model).where(self.model.tenant_id == self.tenant_id)

    # ---- leitura ----

    def all(self, stmt=None):
        """Lista todos os resultados (ou de um stmt customizado)."""
        return list(self.session.scalars(stmt if stmt is not None else self.select()).all())

    def first(self, stmt=None):
        """Primeiro resultado (ou None)."""
        return self.session.scalars(stmt if stmt is not None else self.select()).first()

    def get(self, id_):
        """Busca pelo id; retorna None se não existir OU pertencer a outro
        tenant. Nunca devolver 403 — não confirmar existência ao atacante.
        """
        stmt = self.select().where(self.model.id == id_)
        return self.session.scalar(stmt)

    def count(self, stmt=None) -> int:
        base = stmt if stmt is not None else self.select()
        # Reescreve o select para virar SELECT COUNT(*) preservando os filtros
        count_stmt = (
            select(func.count())
            .select_from(base.subquery())
        )
        return int(self.session.scalar(count_stmt) or 0)

    # ---- escrita ----

    def add(self, **kwargs):
        """Cria, adiciona à sessão e devolve o objeto.

        O `tenant_id` é SEMPRE forçado ao do repositório — qualquer valor
        passado em kwargs é ignorado, prevenindo escrita cruzada acidental.
        Não faz commit; quem orquestra a transação é o service/view.
        """
        kwargs["tenant_id"] = self.tenant_id
        obj = self.model(**kwargs)
        self.session.add(obj)
        return obj

    def delete(self, obj) -> None:
        """Remove o objeto após validar o tenant.

        Levanta `PermissionError` se o objeto pertence a outro tenant —
        é um bug de programação, não uma situação esperada em runtime.
        """
        obj_tenant = getattr(obj, "tenant_id", None)
        if obj_tenant != self.tenant_id:
            raise PermissionError(
                f"{type(obj).__name__}(id={getattr(obj, 'id', '?')}) pertence ao "
                f"tenant {obj_tenant!r}; repositório está no tenant {self.tenant_id!r}."
            )
        self.session.delete(obj)

    def commit(self) -> None:
        """Atalho conveniente; serviços que orquestram transações múltiplas
        devem usar `db.session.commit()` diretamente."""
        self.session.commit()

    def flush(self) -> None:
        self.session.flush()
