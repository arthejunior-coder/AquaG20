"""Seed de demo — dados realistas para smoke manual no browser.

Cria 1 tenant "AquaDemo" com:
  - 1 usuário admin (admin@demo.com / demo12345)
  - 1 usuário financeiro (fin@demo.com / demo12345)
  - 3 tipos de garrafão (20L PC, 20L PP, 10L PET) com valor_reposicao
  - 1 indústria, 1 CD, 2 veículos
  - 6 clientes (3 atacado + 3 varejo), 1 fornecedor, 2 centros de custo
  - Histórico do pool: compra inicial, envase, transferência CD→veículo
  - 2 pedidos em estados diferentes (1 aberto, 1 entregue com permuta casada)
  - Lançamentos a pagar e a receber

Uso:
    python scripts\\seed_demo.py             # recria do zero (TRUNCATE)
    python scripts\\seed_demo.py --keep      # não trunca — adiciona em cima

NO-TENANT-FILTER: roda fora de request HTTP. Idempotente: re-rodar com
o default (--no-keep) zera o banco antes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from app import create_app
from app.auth.password import hash_password
from app.extensions import db
from app.models.cadastros import CentroCusto, Cliente, Fornecedor, TipoCentroCusto, TipoCliente
from app.models.financeiro import (
    FormaLancamento,
    NaturezaLancamento,
)
from app.models.pedidos import PoliticaPermuta, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.models.tenant import PapelUsuario, PlanoTenant, Tenant, Usuario
from app.services.envase_service import EnvaseService
from app.services.financeiro_service import FinanceiroService
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.permuta_service import LinhaEntregaInput, PermutaService
from app.services.pool_service import PoolService


_TABLES_TO_CLEAR = [
    "permutas",
    "rota_paradas",
    "rotas",
    "pedido_itens",
    "pedidos",
    "garrafao_movimentos",
    "garrafao_saldos",
    "lancamentos",
    "centros_custo",
    "tipos_garrafao",
    "locais_estoque",
    "veiculos",
    "entregadores",
    "fornecedores",
    "clientes",
    "usuarios",
    "tenants",
]


def truncate_all():
    with db.engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in _TABLES_TO_CLEAR:
            conn.execute(text(f"TRUNCATE TABLE {t}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--keep", action="store_true",
                   help="Não trunca antes de inserir (default: trunca tudo)")
    args = p.parse_args()

    app = create_app("dev")
    with app.app_context():
        if not args.keep:
            print("Truncando todas as tabelas...")
            truncate_all()

        print("Criando tenant 'AquaDemo'...")
        tenant = Tenant(razao_social="AquaDemo LTDA", nome_fantasia="AquaDemo",
                        cnpj="00.000.000/0001-00", plano=PlanoTenant.pro)
        db.session.add(tenant)
        db.session.flush()
        tid = tenant.id

        print("  +2 usuários (admin, financeiro)")
        admin = Usuario(tenant_id=tid, nome="Admin Demo", email="admin@demo.com",
                         senha_hash=hash_password("demo12345"),
                         papel=PapelUsuario.admin)
        fin = Usuario(tenant_id=tid, nome="Fin Demo", email="fin@demo.com",
                       senha_hash=hash_password("demo12345"),
                       papel=PapelUsuario.financeiro)
        db.session.add_all([admin, fin])

        print("  +3 tipos de garrafão")
        t20pc = TipoGarrafao(tenant_id=tid, nome="20L PC",
                              material=MaterialGarrafao.PC,
                              capacidade_litros=Decimal("20.00"),
                              valor_reposicao=Decimal("35.00"))
        t20pp = TipoGarrafao(tenant_id=tid, nome="20L PP",
                              material=MaterialGarrafao.PP,
                              capacidade_litros=Decimal("20.00"),
                              valor_reposicao=Decimal("32.00"))
        t10pet = TipoGarrafao(tenant_id=tid, nome="10L PET",
                               material=MaterialGarrafao.PET,
                               capacidade_litros=Decimal("10.00"),
                               valor_reposicao=Decimal("18.00"))
        db.session.add_all([t20pc, t20pp, t10pet])

        print("  +4 locais (CD, indústria, 2 veículos)")
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito Central")
        ind = LocalEstoque(tenant_id=tid, tipo=TipoLocal.industria,
                            nome="Indústria SuperÁgua")
        v1 = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo,
                           nome="Caminhão Alpha")
        v2 = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo,
                           nome="Pickup Beta")
        db.session.add_all([cd, ind, v1, v2])

        print("  +6 clientes (3 atacado + 3 varejo)")
        clientes = [
            Cliente(tenant_id=tid, nome=n, tipo=t)
            for n, t in [
                ("Supermercado Bonsucesso", TipoCliente.atacado),
                ("Mercearia Boa Vista", TipoCliente.atacado),
                ("Restaurante Tropical", TipoCliente.atacado),
                ("Maria da Silva", TipoCliente.varejo),
                ("João Pereira", TipoCliente.varejo),
                ("Padaria Esquina", TipoCliente.final),
            ]
        ]
        db.session.add_all(clientes)

        print("  +1 fornecedor, +2 centros de custo")
        forn = Fornecedor(tenant_id=tid, nome="SuperÁgua Indústria",
                          documento="11.111.111/0001-11")
        cc_op = CentroCusto(tenant_id=tid, nome="Operação",
                             tipo=TipoCentroCusto.operacional)
        cc_adm = CentroCusto(tenant_id=tid, nome="Administrativo",
                              tipo=TipoCentroCusto.administrativo)
        db.session.add_all([forn, cc_op, cc_adm])
        db.session.flush()

        # --- Pool: seed inicial + envase + transferências ---
        hoje = date.today()
        val_2027 = date(2027, 6, 1)
        val_2028 = date(2028, 6, 1)
        val_2029 = date(2029, 6, 1)
        vencido = hoje - timedelta(days=5)

        print("Pool: compra inicial de 500 vazios (validade 2029)...")
        pool = PoolService(db.session, tid, usuario_id=admin.id)
        pool.registrar_compra(
            tipo_garrafao_id=t20pc.id, quantidade=500,
            local_destino_id=cd.id, validade=val_2029,
            observacao="estoque inicial",
        )
        pool.registrar_compra(
            tipo_garrafao_id=t20pp.id, quantidade=200,
            local_destino_id=cd.id, validade=val_2029,
            observacao="estoque inicial PP",
        )

        print("Pool: ajuste — 50 cheios vencidos + 100 cheios 2027 no CD...")
        # Para demo: cheios já vencidos no CD (alerta no dashboard)
        pool.registrar_ajuste(
            tipo_garrafao_id=t20pc.id, quantidade=50, local_id=cd.id,
            estado=EstadoGarrafao.cheio, validade=vencido, sinal=+1,
            observacao="seed demo: lote vencido",
        )
        pool.registrar_ajuste(
            tipo_garrafao_id=t20pc.id, quantidade=100, local_id=cd.id,
            estado=EstadoGarrafao.cheio, validade=val_2027, sinal=+1,
            observacao="seed demo: lote 2027",
        )

        print("Pool: transferência 300 vazios CD → indústria + envase...")
        pool.registrar_transferencia(
            tipo_garrafao_id=t20pc.id, quantidade=300,
            local_origem_id=cd.id, local_destino_id=ind.id,
            estado=EstadoGarrafao.vazio, validade=val_2029,
            observacao="indo envasar",
        )
        EnvaseService(db.session, tid, usuario_id=admin.id).registrar_envase(
            tipo_garrafao_id=t20pc.id, quantidade=300,
            local_industria_id=ind.id, validade=val_2029,
            observacao="lote do dia",
        )

        print("Pool: transferência 200 cheios indústria → CD + 50 ao veículo 1...")
        pool.registrar_transferencia(
            tipo_garrafao_id=t20pc.id, quantidade=200,
            local_origem_id=ind.id, local_destino_id=cd.id,
            estado=EstadoGarrafao.cheio, validade=val_2029,
        )
        pool.registrar_transferencia(
            tipo_garrafao_id=t20pc.id, quantidade=50,
            local_origem_id=cd.id, local_destino_id=v1.id,
            estado=EstadoGarrafao.cheio, validade=val_2029,
        )

        print("Pool: descarte 8 vencidos (movimenta KPI de custo)...")
        pool.registrar_descarte(
            tipo_garrafao_id=t20pc.id, quantidade=8,
            local_origem_id=cd.id, estado=EstadoGarrafao.cheio,
            validade=vencido, observacao="vencidos descartados",
        )
        pool.registrar_avaria(
            tipo_garrafao_id=t20pp.id, quantidade=3, local_id=cd.id,
            estado_origem=EstadoGarrafao.vazio, validade=val_2029,
            observacao="quebrou no manuseio",
        )

        # --- Pedidos ---
        print("Pedidos: 1 atacado entregue (permuta casada) + 1 atacado aberto...")
        ped_svc = PedidoService(db.session, tid, usuario_id=admin.id)
        perm_svc = PermutaService(db.session, tid, usuario_id=admin.id)

        # Pedido 1: atacado entregue
        p1 = ped_svc.criar_pedido(
            cliente_id=clientes[0].id, politica_permuta=PoliticaPermuta.casar,
            itens=[
                ItemPedidoInput(t20pc.id, 30, val_2029, Decimal("18.00")),
                ItemPedidoInput(t20pc.id, 10, val_2027, Decimal("18.00")),
            ],
        )
        ped_svc.transicionar(p1, StatusPedido.roteirizado)
        perm_svc.registrar_entrega(
            pedido=p1, veiculo_local_id=v1.id,
            linhas=[
                LinhaEntregaInput(t20pc.id, 30, val_2029),  # casada
                LinhaEntregaInput(t20pc.id, 10, val_2027,
                                  validade_recebida=val_2027),  # casada
            ],
        )
        ped_svc.transicionar(p1, StatusPedido.em_entrega)
        ped_svc.transicionar(p1, StatusPedido.entregue)

        # Pedido 2: atacado aberto (operação amanhã)
        ped_svc.criar_pedido(
            cliente_id=clientes[1].id, politica_permuta=PoliticaPermuta.casar,
            itens=[
                ItemPedidoInput(t20pc.id, 20, val_2029, Decimal("17.00")),
            ],
        )

        # --- Financeiro ---
        print("Financeiro: 3 a receber + 2 a pagar...")
        fin_svc = FinanceiroService(db.session, tid)
        # A receber
        fin_svc.criar(natureza=NaturezaLancamento.receber,
                       descricao=f"Pedido #{p1.id} — Supermercado Bonsucesso",
                       valor=p1.valor_total,
                       vencimento=hoje + timedelta(days=15),
                       cliente_id=clientes[0].id, pedido_id=p1.id,
                       centro_custo_id=cc_op.id)
        l_pago = fin_svc.criar(natureza=NaturezaLancamento.receber,
                                descricao="Venda à vista — Padaria Esquina",
                                valor=Decimal("36.00"),
                                vencimento=hoje - timedelta(days=2),
                                cliente_id=clientes[5].id,
                                centro_custo_id=cc_op.id)
        fin_svc.marcar_pago(l_pago, pago_em=hoje - timedelta(days=2),
                            forma=FormaLancamento.dinheiro)
        fin_svc.criar(natureza=NaturezaLancamento.receber,
                       descricao="Mensalidade Mercearia Boa Vista",
                       valor=Decimal("450.00"),
                       vencimento=hoje + timedelta(days=10),
                       cliente_id=clientes[1].id)
        # A pagar
        fin_svc.criar(natureza=NaturezaLancamento.pagar,
                       descricao="Água + envase — indústria",
                       valor=Decimal("1500.00"),
                       vencimento=hoje + timedelta(days=20),
                       fornecedor_id=forn.id,
                       centro_custo_id=cc_op.id)
        l_pag_pago = fin_svc.criar(natureza=NaturezaLancamento.pagar,
                                    descricao="Aluguel CD",
                                    valor=Decimal("3000.00"),
                                    vencimento=hoje - timedelta(days=5),
                                    centro_custo_id=cc_adm.id)
        fin_svc.marcar_pago(l_pag_pago, pago_em=hoje - timedelta(days=5),
                            forma=FormaLancamento.transferencia)

        db.session.commit()

        print("\nSeed concluído.")
        print(f"  Tenant id={tid}, login: admin@demo.com / demo12345")
        print(f"  Acesse: http://127.0.0.1:5000")
        return 0


if __name__ == "__main__":
    sys.exit(main())
