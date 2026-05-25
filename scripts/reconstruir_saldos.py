"""CLI de auditoria — reconstrói garrafao_saldos a partir do livro-razão
e compara com os saldos atuais.

Uso:
    python scripts\reconstruir_saldos.py --tenant 1                 # dry-run (default)
    python scripts\reconstruir_saldos.py --tenant 1 --apply         # GRAVA correção
    python scripts\reconstruir_saldos.py --tenant 1 --apply --yes   # sem confirmar

NO-TENANT-FILTER: este script roda como super-admin e DEVE receber
--tenant explicitamente. Sem isso, aborta.
"""

import argparse
import sys

from app import create_app
from app.extensions import db
from app.services.pool_service import PoolService


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tenant", type=int, required=True, help="ID do tenant")
    p.add_argument("--apply", action="store_true", help="Gravar correção (default: dry-run)")
    p.add_argument("--yes", action="store_true", help="Não pedir confirmação com --apply")
    args = p.parse_args()

    app = create_app("dev")
    with app.app_context():
        svc = PoolService(db.session, tenant_id=args.tenant)
        divergencias = svc.reconstruir_saldos(dry_run=True)

        if not divergencias:
            print(f"OK — saldos do tenant {args.tenant} batem com o livro-razão.")
            return 0

        print(f"\n{len(divergencias)} divergência(s) encontrada(s) no tenant {args.tenant}:\n")
        print(f"{'tipo_gar':>8} {'local':>6} {'estado':10} {'validade':10} {'esperado':>10} {'real':>10}")
        print("-" * 60)
        for d in divergencias:
            print(
                f"{d.tipo_garrafao_id:>8} {d.local_id:>6} {d.estado.value:10} "
                f"{str(d.validade):10} {d.esperado:>10} {d.real:>10}"
            )

        if not args.apply:
            print("\n[dry-run] — passe --apply para gravar a correção.")
            return 1

        if not args.yes:
            resp = input("\nAplicar correção? Isso ZERA e regrava todos os saldos do tenant. [s/N]: ")
            if resp.strip().lower() != "s":
                print("Cancelado.")
                return 1

        svc.reconstruir_saldos(dry_run=False)
        db.session.commit()
        print(f"\nSaldos do tenant {args.tenant} regravados.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
