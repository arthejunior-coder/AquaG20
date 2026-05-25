"""Lint estático: caça queries que provavelmente esquecem o filtro `tenant_id`.

REGRA DE OURO do projeto: toda query da aplicação filtra por `tenant_id`.
Subir um endpoint sem isso = vazamento de dados entre tenants.

Heurística:
  - Pattern A: `db.session.query(<Model>)` em código de blueprints/ ou
    services/ — toda query deveria ir via repository, que injeta o filtro.
  - Pattern B: `select(<Model>)...where(...)` em código de blueprints/ ou
    services/ SEM `tenant_id` na clausula.

Falsos-positivos esperados (whitelisted via marcador):
  # NO-TENANT-FILTER  → para scripts admin, CLI, ou seed
  Coloque esse comentário na MESMA LINHA do select para silenciar.

Exit code:
  0 — limpo
  1 — encontrou potenciais vazamentos

Uso:
    python scripts\\audit_tenant_filter.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_TARGET_DIRS = [_ROOT / "app" / "blueprints", _ROOT / "app" / "services"]

_PATTERN_QUERY = re.compile(r"\.query\(\s*([A-Z]\w+)\s*\)")
# select(Modelo) com ou sem .where; capturamos pra inspecionar
_PATTERN_SELECT = re.compile(r"\bselect\(\s*([A-Z]\w+)")
_MARKER = "NO-TENANT-FILTER"

# Lista de "models" cujo SELECT precisa de filtro tenant. Tudo que tem
# TenantMixin. Reusamos um set hardcoded pra evitar importar a app (linter
# precisa rodar mesmo sem DB disponível).
_TENANT_MODELS = {
    "Cliente", "Fornecedor", "CentroCusto",
    "Veiculo", "Entregador",
    "TipoGarrafao", "LocalEstoque", "GarrafaoSaldo", "GarrafaoMovimento",
    "Pedido", "PedidoItem", "Permuta",
    "Rota", "RotaParada",
    "Lancamento",
    "Usuario",
}


def _scan_file(path: Path) -> list[str]:
    """Retorna lista de strings 'path:linha: msg' para cada suspeito."""
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _MARKER in line:
            continue
        stripped = line.strip()
        # Pula comentários puros
        if stripped.startswith("#"):
            continue

        # Pattern A: .query(Model)
        m = _PATTERN_QUERY.search(line)
        if m and m.group(1) in _TENANT_MODELS:
            findings.append(
                f"{path.relative_to(_ROOT)}:{lineno}: .query({m.group(1)}) — use "
                f"repository com filtro tenant_id"
            )
            continue

        # Pattern B: select(Model) — checar se há tenant_id no MESMO bloco
        # (heurística: olhar 5 linhas seguintes pelo `.where(...)` com tenant_id).
        sm = _PATTERN_SELECT.search(line)
        if sm and sm.group(1) in _TENANT_MODELS:
            bloco = "\n".join(text.splitlines()[lineno - 1: lineno + 6])
            if "tenant_id" not in bloco and _MARKER not in bloco:
                findings.append(
                    f"{path.relative_to(_ROOT)}:{lineno}: select({sm.group(1)}) "
                    f"sem filtro tenant_id nas 6 linhas seguintes"
                )

    return findings


def main():
    todos: list[str] = []
    for d in _TARGET_DIRS:
        if not d.exists():
            continue
        for py in d.rglob("*.py"):
            todos.extend(_scan_file(py))

    if todos:
        print("[!] Potenciais queries sem filtro tenant_id:\n")
        for line in todos:
            print(f"  {line}")
        print(f"\nTotal: {len(todos)} ocorrência(s).")
        print("Marque falsos-positivos com '# NO-TENANT-FILTER' na linha.")
        return 1

    print("OK — nenhuma query suspeita em blueprints/ + services/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
