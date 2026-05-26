# AquaG20

![CI](https://github.com/arthejunior-coder/AquaG20/actions/workflows/ci.yml/badge.svg)

SaaS multi-tenant para distribuidores de água (garrafões 20L). Plataforma de gestão do pool de vasilhames em regime de permuta, controle por faixa de validade, atendimento, pedidos e financeiro.

Plano completo da implementação em `C:\Users\Usuário\.claude\plans\sim-lazy-crab.md`.
Documento de projeto e schema MySQL em [files/](files/).

## Pré-requisitos

- Python 3.12+
- MySQL 8 (charset utf8mb4)
- PowerShell (Windows) ou shell POSIX
- Node.js **não é necessário** — usamos o binário standalone do Tailwind (instalado via `scripts/install_tailwind.ps1`)

## Instalação

```powershell
# Clone / abra o diretório do projeto
cd c:\AquaG20

# Crie e ative o virtualenv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Instale as dependências
pip install -r requirements.txt

# Copie e edite o arquivo de variáveis de ambiente
Copy-Item .env.example .env
# Edite .env: ajuste SECRET_KEY (use python -c "import secrets; print(secrets.token_hex(32))")
# Ajuste DATABASE_URL com a senha real do usuário MySQL aquag20
```

## Inicialização do banco

O schema é a fonte da verdade e está em [files/schema_aguaG20.sql](files/schema_aguaG20.sql) — **nunca alterar**. A primeira instalação carrega o schema diretamente e marca o Alembic como já-aplicado.

```powershell
# 1) Crie o usuário e o database no MySQL (uma vez, como root)
mysql -u root -p -e "CREATE USER 'aquag20'@'localhost' IDENTIFIED BY 'senha';"
mysql -u root -p -e "GRANT ALL PRIVILEGES ON aquag20.* TO 'aquag20'@'localhost';"
mysql -u root -p -e "GRANT ALL PRIVILEGES ON aquag20_test.* TO 'aquag20'@'localhost';"

# 2) Crie os databases e importe o schema (helper PowerShell)
.\scripts\init_db.ps1

# 3) Inicialize o Alembic e marque o schema como aplicado
flask db init
flask db stamp head
```

Migrações futuras (Fase 2 em diante) são geradas com `flask db migrate -m "..."` + `flask db upgrade`.

## Estrutura do projeto

```
app/
  __init__.py        # factory create_app()
  extensions.py      # db, migrate, login_manager, csrf
  config.py          # Dev / Prod / Test config
  models/            # SQLAlchemy
  repositories/      # Acesso a dados com isolamento tenant_id
  services/          # Lógica de negócio (PoolService, PermutaService, KPIs)
  blueprints/        # Rotas Flask (auth, dashboard, cadastros, pool, pedidos, financeiro)
  auth/              # Argon2, decorators, helper repo()
  templates/         # Jinja2 + HTMX
  static/            # CSS (Tailwind) e JS (HTMX)
migrations/          # Alembic (gerado por flask db init)
tests/               # pytest
scripts/             # CLI helpers (seed, reconstruir saldos, init_db)
files/               # schema + documento de projeto (não alterar)
```

## Frontend (Tailwind + HTMX)

Sem Node.js — usamos o binário standalone do Tailwind v3 e o HTMX servido direto de `app/static/js/`.

```powershell
# Uma vez por checkout: baixa o tailwindcss.exe em scripts/
.\scripts\install_tailwind.ps1

# Em dev: deixa o Tailwind compilando enquanto mexe nos templates
.\scripts\tailwind_watch.ps1

# Build de produção (minificado)
.\scripts\tailwindcss.exe -i .\app\static\css\input.css -o .\app\static\css\tailwind.css --minify
```

O HTMX (49 KB) está committado em `app/static/js/htmx.min.js`. Atualizar versão é um download manual.

## Comandos úteis

```powershell
# Criar tenant + admin inicial
flask create-tenant --razao "Distribuidora X" --cnpj "00.000.000/0001-00" `
    --admin-nome "Maria" --admin-email "maria@x.com"

# Criar usuário em tenant existente
flask create-user --tenant-id 1 --nome "Joao" --email "joao@x.com" --papel atendimento

# Listar tenants
flask list-tenants

# Rodar servidor de desenvolvimento
flask run --port 5000

# Inspecionar rotas registradas
flask routes

# Rodar testes
pytest -v

# Auditoria: reconstrói saldos do livro-razão e compara
python scripts\reconstruir_saldos.py --tenant 1                # dry-run
python scripts\reconstruir_saldos.py --tenant 1 --apply        # grava correção

# Lint: caça queries sem filtro tenant_id em blueprints/services
python scripts\audit_tenant_filter.py

# Seed de demo (TRUNCATE + dados realistas pra smoke manual)
python scripts\seed_demo.py
# Login: admin@demo.com / demo12345
```

## Smoke manual (recomendado pós-deploy)

1. `python scripts\seed_demo.py` — popula um tenant `AquaDemo` com pedidos, permutas, saldos vencidos e lançamentos.
2. `.\scripts\tailwind_watch.ps1` (terminal 1) + `flask run` (terminal 2).
3. Acesse `http://127.0.0.1:5000`, login com `admin@demo.com / demo12345`.
4. Verifique:
   - Dashboard: 3 KPIs preenchidos (envelhecimento com badge "vencido", casamento, custo de reposição).
   - Pool → Saldos: agrupamento por tipo + local + validade.
   - Pedidos: lista com 2 itens, um "entregue" e um "aberto".
   - Financeiro: fluxo de caixa mensal preenchido.
   - Logout + login com `fin@demo.com / demo12345`: vê só /financeiro e /; atendimento (se criado) recebe **403** em /financeiro.

## Status do MVP

19 passos do roadmap concluídos + 5 frentes de Fase 2 (auth/admin, roteirização, hardening, observabilidade, SMTP). **~295 testes automatizados** (todas as superfícies HTTP + serviços + isolamento por tenant em cada blueprint + teste E2E criar→entregar→KPI). Próximas frentes possíveis: storage Redis para Flask-Limiter (multi-worker), dashboards Grafana prontos.

## Email (reset de senha e similares)

Backend selecionado por `MAIL_BACKEND`:

- **`log`** (default em dev/test) — dump no `app.logger`, visível no terminal `flask run`. Útil para inspeção manual sem provedor real.
- **`smtp`** — envia via SMTP usando `smtplib` stdlib. Funciona com qualquer provedor SMTP (Amazon SES, SendGrid, Postmark, Mailgun, Gmail).

Configuração de produção (exemplo Amazon SES, região us-east-1):

```bash
MAIL_BACKEND=smtp
SMTP_HOST=email-smtp.us-east-1.amazonaws.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=AKIA...                        # SMTP credentials do SES
SMTP_PASSWORD=...
SMTP_FROM_ADDR=no-reply@aquag20.com          # domínio verificado no SES
SMTP_FROM_NAME=AquaG20
SMTP_TIMEOUT=30
```

SSL direto (porta 465) — defina `SMTP_USE_SSL=true` e `SMTP_USE_TLS=false`.

## Rate limiting em produção (multi-worker)

O default `RATELIMIT_STORAGE_URI=memory://` funciona em **1 worker apenas**. Com `gunicorn -w 4`, cada worker tem contador próprio, então o limite efetivo fica 4× o configurado.

Para produção real, use Redis (pacote `redis==5.0.7` já está em `requirements.txt`):

```bash
RATELIMIT_STORAGE_URI=redis://redis-host:6379/0
# ou com TLS:
RATELIMIT_STORAGE_URI=rediss://redis.prod.internal:6380/0
# ou com auth:
RATELIMIT_STORAGE_URI=redis://user:password@redis-host:6379/1
```

Outros backends suportados pelo `limits` (lib base do Flask-Limiter): `memcached://`, `mongodb://`, etc. Veja [docs do flask-limiter](https://flask-limiter.readthedocs.io/) para a lista completa.

## Backup do MySQL

Script `scripts/backup_db.py` faz dump via `mysqldump` + gzip + rotação. Precisa de `mysqldump` no PATH.

```powershell
# Default: ./backups, retenção 30 dias
python scripts\backup_db.py

# Customizado
python scripts\backup_db.py --output D:\backups --retention-days 14

# Manter tudo (sem rotação) — útil pra arquivamento manual
python scripts\backup_db.py --keep-all

# Override da URL (default lê DATABASE_URL do .env)
python scripts\backup_db.py --database-url "mysql://user:pass@host/db"
```

Agendamento sugerido (todo dia 2h da manhã):

```bash
# Linux/cron
0 2 * * * cd /path/AquaG20 && /path/.venv/bin/python scripts/backup_db.py >> backup.log 2>&1
```

```powershell
# Windows — Task Scheduler (tarefa básica)
# Programa: C:\AquaG20\.venv\Scripts\python.exe
# Argumentos: C:\AquaG20\scripts\backup_db.py
# Iniciar em: C:\AquaG20
```

**Decisões:**
- Senha vai via env `MYSQL_PWD` — não aparece em `ps aux` / Task Manager.
- `--single-transaction` (InnoDB consistente sem lock de tabela), `--routines`, `--triggers`, `--default-character-set=utf8mb4`.
- Output: `<dbname>-YYYYMMDD-HHMMSS.sql.gz` — fácil de listar/ordenar/parsear.
- Rotação por `mtime` (não confia no nome — protege contra timezone/drift).
- Para off-site backup: rode `aws s3 sync backups/ s3://meu-bucket/aquag20/` no cron logo após.

## CI/CD (GitHub Actions)

Pipeline em [.github/workflows/ci.yml](.github/workflows/ci.yml). Roda em push/PR para `main`/`master`/`develop` (ou via `workflow_dispatch` manualmente).

O que faz:

1. Sobe MySQL 8.0 como service container, cria DBs `aquag20` + `aquag20_test`, importa o schema oficial.
2. Smoke do app factory (`create_app('test')` precisa funcionar).
3. **Auditoria `audit_tenant_filter.py`** — falha o build se houver query sem filtro tenant_id.
4. **Pytest** completo (`--maxfail=5` pra falhar rápido com primeiro erro evidente).

Notas:

- Python 3.12 nos runners (dev local roda 3.14; a app suporta os dois).
- `cancel-in-progress: true` por branch — push novo cancela run anterior, economiza minutos.
- Argon2 com custos reduzidos em CI (`ARGON2_TIME_COST=1`, memory 8MB) — login fica rápido sem mudar a lógica.
- Secret `SECRET_KEY` previsível só pro CI; **NUNCA** copiar pra prod.

Para usar:

1. `git init && git remote add origin https://github.com/<voce>/aquag20.git`
2. `git add . && git commit -m "initial"`
3. `git push -u origin main` — o workflow dispara automaticamente.

Badge no README (após primeira execução):

```markdown
![CI](https://github.com/<voce>/aquag20/actions/workflows/ci.yml/badge.svg)
```

## Dashboards Grafana

Em [dashboards/grafana/](dashboards/grafana/):

- `aquag20-operations.json` — dashboard operacional com 9 painéis (requests/seg por status, latência p50/p95/p99, taxa de erro 5xx %, top endpoints, erros por endpoint).
- `prometheus.yml.example` — config de scrape para o Prometheus apontar pro `/metrics` da app.

### Como importar

1. Garanta que a app está com observabilidade ligada (`OBSERVABILITY_METRICS_ENABLED=true`, default em `ProdConfig`).
2. Configure o Prometheus pra fazer scrape (veja `prometheus.yml.example` — substitua os hosts pelos seus).
3. No Grafana: **Dashboards → New → Import → Upload JSON file** → escolha `aquag20-operations.json`.
4. Quando pedir, selecione seu datasource Prometheus.

Métricas usadas (expostas pelo `/metrics`):
- `aquag20_requests_total{method, endpoint, status}` — Counter
- `aquag20_request_latency_seconds{method, endpoint}` — Histogram

O dashboard está blindado por testes (`tests/test_grafana_dashboard.py`) que verificam: JSON parseável, `uid` estável, painéis presentes, e — **importante** — todas as queries referenciam só métricas que a app realmente expõe. Renomear um Counter na app vai fazer o CI quebrar antes do deploy quebrar o dashboard em prod.
