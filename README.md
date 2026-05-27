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
- Para off-site backup, use o script dedicado abaixo (sem dependência do aws CLI — usa boto3).

## Off-site backup (S3-compatível: AWS / Backblaze B2 / Oracle OCI / Cloudflare R2 / MinIO)

Script `scripts/sync_backups_s3.py` envia os `.sql.gz` locais pra um bucket S3 — proteção contra incêndio/ransomware no servidor. Idempotente: pula arquivos cujo nome já existe no bucket.

**Mesmo script serve qualquer provider S3-compatível** — basta setar `S3_BACKUP_ENDPOINT_URL` apontando pro provider escolhido. Sem ele, vai pra AWS S3 nativo.

### Qual provider escolher

| | **Backblaze B2** ⭐ | **Oracle OCI** | **AWS S3** | **Cloudflare R2** |
|---|---|---|---|---|
| Free tier | 10 GB permanente | **20 GB permanente** + 10 GB Archive | 5 GB / 12 meses | 10 GB permanente |
| Custo/GB/mês (pago) | **$0.006** | $0.0255 | $0.023 | $0.015 |
| Egress | $0.01/GB | 10 TB free/mês | $0.09/GB | **$0** |
| DC Brasil | ❌ (EUA/EU) | ✅ sa-saopaulo-1 | ✅ sa-east-1 | ❌ (Atlanta) |
| Setup | **5 min** | 15 min, UI confusa | 10 min | 10 min |
| Object Lock | ✅ | ✅ | ✅ | ✅ |

**Recomendação pro caso solo-dev/MVP**: Backblaze B2 — mais simples, mais barato, S3-compat, sem vendor lock-in.

### Setup por provider

<details>
<summary><strong>Backblaze B2</strong> (5 min — recomendado)</summary>

1. https://www.backblaze.com/b2/sign-up.html — conta grátis (10 GB permanente)
2. **My Account → App Keys** → **Add a New Application Key**:
   - Name: `aquag20-backup`
   - Allow access to: **All buckets** (ou só o seu se já criou)
   - Type of Access: **Read and Write**
   - Create New Key → **copie agora** (mostra 1x só):
     - `keyID` (vai como `AWS_ACCESS_KEY_ID`)
     - `applicationKey` (vai como `AWS_SECRET_ACCESS_KEY`)
3. **Buckets → Create a Bucket**:
   - Name: `aquag20-backups-arthejunior` (precisa ser globalmente único)
   - Files in Bucket: **Private**
   - Default Encryption: **Enable** (SSE-B2)
   - Object Lock: **Enable** + Compliance mode se quiser anti-ransomware
4. Veja o endpoint do bucket (mostra na info do bucket, algo tipo `s3.us-west-002.backblazeb2.com`)
5. `.env`:
```bash
S3_BACKUP_BUCKET=aquag20-backups-arthejunior
S3_BACKUP_ENDPOINT_URL=https://s3.us-west-002.backblazeb2.com
S3_BACKUP_STORAGE_CLASS=                      # vazio — B2 não usa StorageClass
AWS_ACCESS_KEY_ID=<keyID do B2>
AWS_SECRET_ACCESS_KEY=<applicationKey do B2>
AWS_REGION=us-west-002                         # região do endpoint
```
</details>

<details>
<summary><strong>Oracle OCI</strong> (15 min — free tier mais generoso)</summary>

1. https://signup.cloud.oracle.com — Always Free tier (20 GB Object + 10 GB Archive permanente)
2. **Object Storage → Buckets → Create Bucket** na compartment `root`:
   - Name: `aquag20-backups`
   - Storage tier: Standard (ou Archive pra retenção longa)
3. Na info do bucket, copie o **Namespace** (string tipo `axqz3lkqwxyz`)
4. **Identity → Users → Create User**:
   - Name: `aquag20-backup-uploader`
   - Type: IAM (não Federated)
5. Atribua a política mínima (Identity → Policies → Create Policy):
```
Allow user aquag20-backup-uploader to manage object-family in compartment id <COMPARTMENT_OCID> where target.bucket.name='aquag20-backups'
```
6. Volte no user → **Customer Secret Keys** → Generate → copie agora (mostra 1x):
   - `Access Key` (vai como `AWS_ACCESS_KEY_ID`)
   - `Secret Key` (vai como `AWS_SECRET_ACCESS_KEY`)
7. `.env`:
```bash
S3_BACKUP_BUCKET=aquag20-backups
S3_BACKUP_ENDPOINT_URL=https://<NAMESPACE>.compat.objectstorage.sa-saopaulo-1.oraclecloud.com
S3_BACKUP_STORAGE_CLASS=                      # vazio — OCI usa storage tier no bucket, não por objeto
AWS_ACCESS_KEY_ID=<Customer Access Key>
AWS_SECRET_ACCESS_KEY=<Customer Secret Key>
AWS_REGION=sa-saopaulo-1
```
</details>

<details>
<summary><strong>Cloudflare R2</strong> (10 min — egress zero)</summary>

1. https://dash.cloudflare.com → R2 → Activate (pede cartão, mas 10 GB free permanente)
2. **R2 → Create bucket** → Name: `aquag20-backups`
3. Na sidebar → **Manage R2 API Tokens** → Create API Token:
   - Permissions: **Object Read & Write**
   - Specify bucket: `aquag20-backups`
   - TTL: Forever
   - Copie: `Access Key ID` + `Secret Access Key` + `Endpoint for S3 Clients`
4. `.env`:
```bash
S3_BACKUP_BUCKET=aquag20-backups
S3_BACKUP_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
S3_BACKUP_STORAGE_CLASS=                      # vazio — R2 só tem 1 tier
AWS_ACCESS_KEY_ID=<R2 access key>
AWS_SECRET_ACCESS_KEY=<R2 secret key>
AWS_REGION=auto                                # R2 ignora region
```
</details>

<details>
<summary><strong>AWS S3</strong> (10 min — standard da indústria)</summary>

1. https://aws.amazon.com → Create AWS Account
2. **S3 → Create bucket** → name único, region `sa-east-1`, Block all public access ✅, Versioning ✅
3. **IAM → Users → Create user** `aquag20-backup-uploader` (sem console access)
4. Attach policy (JSON, troque o bucket):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::meu-bucket"},
    {"Effect": "Allow", "Action": ["s3:PutObject","s3:DeleteObject"], "Resource": "arn:aws:s3:::meu-bucket/aquag20-backups/*"}
  ]
}
```
5. Security credentials → Create access key → Application running outside AWS → copie
6. `.env`:
```bash
S3_BACKUP_BUCKET=meu-bucket
S3_BACKUP_PREFIX=aquag20-backups/
S3_BACKUP_STORAGE_CLASS=STANDARD_IA            # opcional — IA/GLACIER_IR pra economizar
# S3_BACKUP_ENDPOINT_URL=                       # vazio = AWS default
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=sa-east-1
```
</details>

### Uso (igual em qualquer provider)

```powershell
# Default: envia tudo de ./backups que ainda não está no bucket
python scripts\sync_backups_s3.py

# Dry-run pra ver o que seria enviado
python scripts\sync_backups_s3.py --dry-run

# Smoke test end-to-end (sobe arquivo fake, verifica, apaga)
.\scripts\test_s3_e2e.ps1

# Rotação no bucket (sem flag, backups acumulam — default seguro)
python scripts\sync_backups_s3.py --s3-retention-days 365

# Override por CLI (útil pra staging)
python scripts\sync_backups_s3.py --bucket outro --endpoint-url https://...
```

### Agendamento (rode logo após `backup_db.py`)

```bash
# Linux/cron
0 2 * * * cd /path/AquaG20 && /path/.venv/bin/python scripts/backup_db.py >> backup.log 2>&1
5 2 * * * cd /path/AquaG20 && /path/.venv/bin/python scripts/sync_backups_s3.py >> backup.log 2>&1
```

```powershell
# Windows — Task Scheduler (gatilho diário, ~5min após o backup)
# Programa: C:\AquaG20\.venv\Scripts\python.exe
# Argumentos: C:\AquaG20\scripts\sync_backups_s3.py
# Iniciar em: C:\AquaG20
```

**Decisões:**
- `ServerSideEncryption=AES256` enviado sempre (AWS/B2 honram; R2/OCI ignoram silenciosamente — encriptam por default).
- `ContentType=application/gzip` — download direto serve com encoding correto.
- Skip por nome de arquivo (não por hash): backups têm timestamp único, sobrescrever seria perda silenciosa.
- `S3_BACKUP_STORAGE_CLASS=""` (vazio) omite o param — necessário pra B2/R2 que não suportam o conceito.
- Sem `--s3-retention-days`, **nada é deletado do bucket** — estratégia segura por default. Off-site deve sobreviver a um attacker que apaga local + rotação.

### Object Lock (proteção anti-ransomware)

Suporte opcional a Object Lock per-arquivo via env vars. Quando ativado, cada upload sai com retention COMPLIANCE/GOVERNANCE de N dias — **nem você consegue apagar antes da data**, mesmo com credenciais válidas. Proteção real contra: ransomware que pega sua key B2/AWS, conta cloud roubada, atacante que apagaria backups antes da rotação.

**Modos:**
- **GOVERNANCE**: pode ser bypassed por keys com permissão `bypassGovernance`. Flexível, mas se atacante pegar a master/admin, ainda apaga.
- **COMPLIANCE**: **IRREVERSÍVEL**. Nem o suporte do provider consegue apagar antes da retention expirar. Proteção máxima, sem volta.

**B2 (Backblaze) só oferece COMPLIANCE** via UI. AWS S3 oferece os dois.

**Setup:**

1. **No bucket** (uma vez): habilite Object Lock no momento da criação do bucket. Em buckets existentes a feature **não pode ser adicionada** (recriar bucket é o caminho).

2. **No `.env`** — adicione 2 vars:
```bash
S3_BACKUP_LOCK_MODE=COMPLIANCE         # ou GOVERNANCE (se provider suportar)
S3_BACKUP_LOCK_RETENTION_DAYS=7        # dias de retention por arquivo
```

3. **Sem essas vars, comportamento é idêntico ao anterior** (sem lock). Opt-in puro.

**Guardrail crítico:**

O script tem um cap interno `_MAX_LOCK_RETENTION_DAYS=35` em [scripts/sync_backups_s3.py](scripts/sync_backups_s3.py). Qualquer valor acima é rejeitado **antes** da chamada à API. Razão: bug que envia 30 ANOS em vez de 30 dias (zero a mais por engano) com COMPLIANCE multiplica seu custo por 365 e não tem volta. Se você deliberadamente precisa de retention maior, edite o cap no source (e revise os testes).

**Trade-offs:**

| | Sem lock | Com lock |
|---|---|---|
| Atacante pega sua key | Apaga tudo | Só apaga arquivos fora da retention |
| Você comete erro de config (typo) | Reversível | Custo fica preso N dias |
| Rotação automática (`--s3-retention-days`) | Funciona | Falha silenciosa em arquivos lockados |
| Custo | Só pelo que está no bucket | Pelo que está no bucket + lock pending |

**Boa prática**: configure `S3_BACKUP_LOCK_RETENTION_DAYS` < `--s3-retention-days`. Ex: lock=7 dias + retention=14 dias significa que cada backup fica protegido por 1 semana, depois pode ser removido pela rotação normal.

**Verificar metadata de um arquivo:**

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(); import os, boto3; c=boto3.client('s3', endpoint_url=os.environ['S3_BACKUP_ENDPOINT_URL']); r=c.head_object(Bucket=os.environ['S3_BACKUP_BUCKET'], Key='aquag20-backups/SEU-ARQUIVO.sql.gz'); print(r.get('ObjectLockMode'), r.get('ObjectLockRetainUntilDate'))"
```

Deve imprimir tipo `COMPLIANCE 2026-06-03 14:52:02+00:00`.

## Off-site backup (Google Drive) — alternativa gratuita ao S3

Script `scripts/sync_backups_gdrive.py` faz o mesmo que o do S3, mas pra uma pasta do Google Drive. Vantagens: **gratuito até 15 GB**, UI familiar pra browsing/restore. Desvantagens vs S3: setup OAuth um pouco mais chato (browser na 1ª vez), sem object lock (proteção anti-ransomware mais fraca).

Quando usar: dev solo, MVP, sem requisito de compliance. Migre pra S3 quando entrar dinheiro de cliente ou volume > 2 TB.

### Setup uma vez

**1) Criar projeto no Google Cloud Console:**
- Acesse https://console.cloud.google.com → **Select a project** → **New Project** → nome `aquag20-backups` (ou qualquer outro) → Create.
- Na barra de busca topo: digite "**Google Drive API**" → clique no resultado → **Enable**.

**2) Criar OAuth client:**
- Menu lateral → **APIs & Services** → **OAuth consent screen**:
  - **User type**: External → Create
  - **App name**: `AquaG20 Backup` / **User support email**: seu email / **Developer contact**: seu email → Save and continue
  - **Scopes**: pule (Save and continue)
  - **Test users**: clique **+ Add Users** → adicione seu próprio email Gmail → Save and continue
- Menu lateral → **Credentials** → **+ Create Credentials** → **OAuth client ID**:
  - **Application type**: **Desktop app**
  - **Name**: `AquaG20 Desktop` → Create
  - **Download JSON** → salve como `gdrive_credentials.json` na raiz do projeto (`c:\AquaG20\gdrive_credentials.json`).
  - Esse arquivo está no `.gitignore` — não vai pra git.

**3) Criar pasta no Drive e pegar o ID:**
- Acesse https://drive.google.com → botão **+ New** → **New folder** → nome `AquaG20 Backups` → Create.
- Abra a pasta. A URL fica `https://drive.google.com/drive/folders/<ID_LONGO>`.
- Copie esse `<ID_LONGO>`.

**4) Preencher `.env`:**

```bash
GDRIVE_FOLDER_ID=1aBcDeFgHiJkLmNoPqRsTuVwXyZ123456     # o ID que você copiou
# Opcionais (defaults funcionam):
# GDRIVE_CLIENT_SECRETS=gdrive_credentials.json
# GDRIVE_TOKEN_FILE=gdrive_token.json
```

**5) Primeira execução (INTERATIVA — abre browser):**

```powershell
python scripts\sync_backups_gdrive.py --dry-run
```

Vai abrir seu browser → "Sign in with Google" → escolha sua conta → **"Google hasn't verified this app"** clique **Advanced** → **Go to AquaG20 Backup (unsafe)** (é "unsafe" porque é seu app não publicado — normal) → **Continue** → **Continue** novamente pra dar permissão de Drive.

Depois disso é gerado `gdrive_token.json` (também gitignored) com refresh token. **Roda headless pra sempre** — só vai precisar repetir esse passo se você revogar o acesso no Drive.

### Uso normal

```powershell
# Upload de tudo que ainda não está na pasta
python scripts\sync_backups_gdrive.py

# Dry-run
python scripts\sync_backups_gdrive.py --dry-run

# Rotação (default: nada é apagado — backups acumulam)
python scripts\sync_backups_gdrive.py --gdrive-retention-days 365
```

### Agendamento (Windows Task Scheduler)

Igual ao do S3 — uma tarefa pra `backup_db.py` às 2h, outra pra `sync_backups_gdrive.py` às 2h05. Detalhes na seção do S3 acima (mesmos passos, só troca o nome do script).

### Decisões

- Scope `drive.file` — app só vê arquivos que ELA criou. Não enxerga seus outros arquivos do Drive. Princípio do menor privilégio.
- Skip por **filename** (não hash) — backups têm timestamp único.
- Resumable upload (`MediaFileUpload(resumable=True)`) — sobrevive a glitches de rede em arquivos grandes.
- `gdrive_credentials.json` e `gdrive_token.json` no `.gitignore`. O token contém refresh_token = acesso permanente à pasta; cuide bem dele.
- Sem `--gdrive-retention-days`, **nada é apagado**. Default seguro contra ransomware/conta roubada (limitado — atacante com seu cookie do Drive ainda consegue apagar via web).

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
