-- =====================================================================
--  ERP Logística Água G20 — Schema MySQL inicial (MVP)
--  Modelo: SaaS multi-tenant (isolamento por tenant_id)
--  Conceito-chave: o GARRAFÃO (20L) é um ATIVO RETORNÁVEL, não um
--  produto consumível. Rastreamos o vasilhame em todo o ciclo.
--  Charset: utf8mb4 / Engine: InnoDB (FKs + transações)
-- =====================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- =====================================================================
--  BLOCO 0 — TENANT / USUÁRIOS / ACESSO
-- =====================================================================

-- Cada distribuidor é um tenant do SaaS.
CREATE TABLE tenants (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    razao_social    VARCHAR(160) NOT NULL,
    nome_fantasia   VARCHAR(160) NULL,
    cnpj            VARCHAR(18)  NULL,
    plano           ENUM('trial','basico','pro','enterprise') NOT NULL DEFAULT 'trial',
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_tenant_cnpj (cnpj)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Usuários: SEMPRE vinculados a um tenant. Login = email + senha (hash).
CREATE TABLE usuarios (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    nome            VARCHAR(120) NOT NULL,
    email           VARCHAR(160) NOT NULL,
    senha_hash      VARCHAR(255) NOT NULL,           -- bcrypt/argon2
    papel           ENUM('admin','gestor','atendimento','motorista','financeiro') NOT NULL DEFAULT 'atendimento',
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    ultimo_login    DATETIME NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_usuario_email (email),
    KEY idx_usuario_tenant (tenant_id),
    CONSTRAINT fk_usuario_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
--  BLOCO 1 — CADASTROS BÁSICOS
-- =====================================================================

-- Clientes do distribuidor: pode ser revendedor atacado OU consumidor final.
CREATE TABLE clientes (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    tipo            ENUM('atacado','varejo','final') NOT NULL DEFAULT 'final',
    nome            VARCHAR(160) NOT NULL,
    nome_fantasia   VARCHAR(160) NULL,
    documento       VARCHAR(18)  NULL,              -- CPF/CNPJ
    telefone        VARCHAR(20)  NULL,              -- ATENÇÃO: lembrar do overflow do ERP BV; manter 20
    email           VARCHAR(160) NULL,
    -- Endereço + geocoding (essencial para rotas)
    endereco        VARCHAR(200) NULL,
    bairro          VARCHAR(100) NULL,
    cidade          VARCHAR(100) NULL,
    uf              CHAR(2) NULL,
    cep             VARCHAR(9) NULL,
    latitude        DECIMAL(10,7) NULL,
    longitude       DECIMAL(10,7) NULL,
    -- MODELO DE PERMUTA: garrafão cheio é trocado pelo vazio do cliente (1-por-1).
    -- Este saldo NÃO é "posse rastreada"; é só o desbalanço acumulado quando a
    -- troca não fecha (ex.: cliente novo levou 2 cheios e não tinha vazios).
    -- Em operação normal de permuta, tende a ficar perto de zero.
    saldo_garrafoes INT NOT NULL DEFAULT 0,
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_cliente_tenant (tenant_id),
    KEY idx_cliente_cidade (tenant_id, cidade, bairro),
    CONSTRAINT fk_cliente_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Indústria(s) fornecedora(s) onde se compra a água.
CREATE TABLE fornecedores (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    nome            VARCHAR(160) NOT NULL,
    documento       VARCHAR(18) NULL,
    telefone        VARCHAR(20) NULL,
    endereco        VARCHAR(200) NULL,
    latitude        DECIMAL(10,7) NULL,
    longitude       DECIMAL(10,7) NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_fornecedor_tenant (tenant_id),
    CONSTRAINT fk_fornecedor_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
--  BLOCO 2 — FROTA E EQUIPE DE ENTREGA (cadeia de 3 níveis)
-- =====================================================================

CREATE TABLE veiculos (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    tipo            ENUM('caminhao','pickup','moto') NOT NULL,
    placa           VARCHAR(8) NULL,
    descricao       VARCHAR(120) NULL,
    -- Capacidade em número de garrafões — base para validar carga
    capacidade_garrafoes INT NOT NULL DEFAULT 0,
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    KEY idx_veiculo_tenant (tenant_id),
    CONSTRAINT fk_veiculo_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Motorista/entregador. Pode ou não ter login (usuario_id opcional).
CREATE TABLE entregadores (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    usuario_id      BIGINT UNSIGNED NULL,
    nome            VARCHAR(120) NOT NULL,
    telefone        VARCHAR(20) NULL,
    cnh             VARCHAR(20) NULL,
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    KEY idx_entregador_tenant (tenant_id),
    CONSTRAINT fk_entregador_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_entregador_usuario FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
--  BLOCO 3 — GARRAFÕES EM REGIME DE PERMUTA (POOL)  ⭐ CORAÇÃO DO SISTEMA
--  Modelo real: na entrega o garrafão cheio é PERMUTADO pelo vazio do
--  cliente (troca 1-por-1). Os vasilhames se misturam num pool comum —
--  um garrafão novo do distribuidor pode voltar trocado por outro com
--  2 anos de uso. NÃO se rastreia posse nem "idade por unidade".
--
--  O que importa controlar é a SAÚDE DO POOL:
--    (a) VALIDADE — perfil de envelhecimento (quantos vencem por mês);
--    (b) REPOSIÇÃO — ~15% do pool AO MÊS sai por avaria ou vencimento
--        e precisa ser comprado novo. O pool se renova em ~6-7 meses,
--        então antecipar validade é a maior alavanca de economia.
--  Por isso o controle é por FAIXA DE VALIDADE (lote), não por unidade.
-- =====================================================================

-- Catálogo de tipos de garrafão do distribuidor. Define material e
-- capacidade. Validade é controlada por lote (ver garrafao_saldos),
-- não aqui, pois um mesmo tipo tem vasilhames de várias idades no pool.
CREATE TABLE tipos_garrafao (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    nome            VARCHAR(80) NOT NULL,           -- ex: "Garrafão 20L Policarbonato"
    material        ENUM('PC','PP','PET') NOT NULL, -- PC=policarbonato, PP=polipropileno, PET
    capacidade_litros DECIMAL(5,2) NOT NULL DEFAULT 20.00,
    valor_reposicao DECIMAL(10,2) NULL,             -- custo de comprar 1 vasilhame novo p/ repor o pool
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    KEY idx_tipogar_tenant (tenant_id),
    UNIQUE KEY uq_tipogar (tenant_id, nome),
    CONSTRAINT fk_tipogar_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- "Locais de estoque" são lógicos: depósito, cada veículo, cliente,
-- indústria (garrafões do distribuidor lá para envase) e descarte.
-- Isso permite saber EXATAMENTE onde cada garrafão do pool está —
-- inclusive os que estão fisicamente na indústria aguardando envase,
-- que continuam sendo PROPRIEDADE do distribuidor.
CREATE TABLE locais_estoque (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    tipo            ENUM('cd','veiculo','industria','cliente','descarte') NOT NULL,
    nome            VARCHAR(120) NOT NULL,
    veiculo_id      BIGINT UNSIGNED NULL,   -- se tipo='veiculo'
    KEY idx_local_tenant (tenant_id),
    CONSTRAINT fk_local_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_local_veiculo FOREIGN KEY (veiculo_id) REFERENCES veiculos(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ESTRATÉGIA DE RASTREIO (escolha de design importante):
-- Como há PERMUTA com o cliente (vasilhames se misturam no pool), o
-- rastreio é AGREGADO por (tipo, local, estado, faixa de validade).
-- Não faz sentido rastrear unidade, pois a posse junto ao cliente não
-- é estável. A faixa de validade é o eixo central: é o que permite
-- saber quanto do pool está envelhecendo e quanto precisa ser reposto
-- (~15% ao mês). Observação: o garrafão é SEMPRE propriedade do
-- distribuidor — inclusive enquanto está na indústria para envase.

CREATE TABLE garrafao_saldos (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    tipo_garrafao_id BIGINT UNSIGNED NOT NULL,      -- material/capacidade vêm daqui
    local_id        BIGINT UNSIGNED NOT NULL,
    estado          ENUM('cheio','vazio','avariado') NOT NULL,
    -- Faixa de validade do lote. Em permuta, ao receber um vazio o
    -- distribuidor classifica em qual faixa de validade ele entra
    -- (estampada no fundo do vasilhame). É o coração do controle.
    validade        DATE NULL,
    quantidade      INT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_saldo (tenant_id, tipo_garrafao_id, local_id, estado, validade),
    KEY idx_saldo_tenant (tenant_id),
    KEY idx_saldo_validade (tenant_id, validade),  -- envelhecimento do pool + giro FEFO
    CONSTRAINT fk_saldo_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_saldo_tipo   FOREIGN KEY (tipo_garrafao_id) REFERENCES tipos_garrafao(id),
    CONSTRAINT fk_saldo_local  FOREIGN KEY (local_id)  REFERENCES locais_estoque(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- LIVRO-RAZÃO de movimentos de garrafão (auditável, nunca se apaga).
-- Tipos de movimento e seu significado:
--   envase        = INDUSTRIALIZAÇÃO. O garrafão do distribuidor é lavado
--                   e cheio na indústria (vazio→cheio). NÃO muda o tamanho
--                   do pool; gera custo de ÁGUA+SERVIÇO, não de vasilhame.
--   compra        = entrada de vasilhame NOVO (reposição dos ~15%/mês que
--                   saíram). Custo de REPOSIÇÃO — KPI financeiro central.
--   permuta       = troca cheio↔vazio na entrega ao cliente.
--   transferencia = deslocamento físico entre locais (ex.: depósito↔indústria,
--                   depósito↔veículo). Não muda estado nem tamanho do pool.
--   avaria        = garrafão passa para o estado avariado.
--   descarte      = garrafão sai do pool (vencido/avariado).
--   ajuste        = correção de inventário.
CREATE TABLE garrafao_movimentos (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    tipo_garrafao_id BIGINT UNSIGNED NOT NULL,      -- qual tipo/material movimentou
    tipo            ENUM('envase','compra','permuta','transferencia',
                         'avaria','descarte','ajuste') NOT NULL,
    local_origem_id BIGINT UNSIGNED NULL,
    local_destino_id BIGINT UNSIGNED NULL,
    estado          ENUM('cheio','vazio','avariado') NOT NULL,
    validade        DATE NULL,
    quantidade      INT NOT NULL,
    referencia_tipo VARCHAR(40) NULL,       -- ex: 'pedido', 'rota'
    referencia_id   BIGINT UNSIGNED NULL,
    usuario_id      BIGINT UNSIGNED NULL,
    observacao      VARCHAR(255) NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_mov_tenant (tenant_id, criado_em),
    KEY idx_mov_tipogar (tenant_id, tipo_garrafao_id),
    KEY idx_mov_tipo (tenant_id, tipo),    -- p/ separar custo de água x reposição
    CONSTRAINT fk_mov_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_mov_tipo   FOREIGN KEY (tipo_garrafao_id) REFERENCES tipos_garrafao(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
--  BLOCO 4 — ATENDIMENTO, PEDIDOS E ROTAS
--  PERMUTA CASADA POR VALIDADE: o cliente pede por faixa de validade
--  (ex.: 10x2027, 15x2028, 35x2029) e o distribuidor TENTA entregar e
--  receber vazios da mesma validade, para não degradar o perfil do
--  pool. A regra NÃO é rígida: concessões são permitidas (entregar
--  outra validade para não perder a venda) e ficam REGISTRADAS como
--  descasamento, virando KPI. Atacado casa validade; varejo é flexível.
-- =====================================================================

-- Cabeçalho do pedido (totais e dados gerais).
CREATE TABLE pedidos (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    cliente_id      BIGINT UNSIGNED NOT NULL,
    status          ENUM('aberto','roteirizado','em_entrega','entregue','cancelado')
                        NOT NULL DEFAULT 'aberto',
    -- Política de permuta: 'casar' tenta manter validade (atacado);
    -- 'flexivel' aceita qualquer validade no retorno (varejo/consumidor).
    politica_permuta ENUM('casar','flexivel') NOT NULL DEFAULT 'casar',
    qtd_total       INT NOT NULL DEFAULT 0,          -- soma dos itens (cheios a entregar)
    valor_total     DECIMAL(12,2) NOT NULL DEFAULT 0,
    forma_pagamento ENUM('dinheiro','pix','cartao','prazo') NULL,
    canal           ENUM('telefone','whatsapp','app','balcao') NULL,
    observacao      VARCHAR(255) NULL,
    criado_por      BIGINT UNSIGNED NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_pedido_tenant_status (tenant_id, status),
    KEY idx_pedido_cliente (cliente_id),
    CONSTRAINT fk_pedido_tenant  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_pedido_cliente FOREIGN KEY (cliente_id) REFERENCES clientes(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Itens do pedido POR FAIXA DE VALIDADE. Esta é a granularidade que
-- viabiliza a permuta casada. Um item = "X garrafões da validade Y".
--   - Atacado: várias linhas (10x2027, 15x2028, 35x2029).
--   - Varejo:  uma linha só, validade NULL (sem exigência de casar).
-- A flexibilidade vem da comparação entre qtd_solicitada (o que o
-- cliente pediu naquela validade) e qtd_atendida (o que de fato saiu).
CREATE TABLE pedido_itens (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    pedido_id       BIGINT UNSIGNED NOT NULL,
    tipo_garrafao_id BIGINT UNSIGNED NOT NULL,
    validade_solicitada DATE NULL,                  -- validade pedida (NULL = sem exigência)
    qtd_solicitada  INT NOT NULL DEFAULT 0,
    qtd_atendida    INT NOT NULL DEFAULT 0,         -- preenchido na separação/entrega
    preco_unitario  DECIMAL(10,2) NULL,
    KEY idx_item_pedido (pedido_id),
    CONSTRAINT fk_item_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_item_pedido FOREIGN KEY (pedido_id) REFERENCES pedidos(id),
    CONSTRAINT fk_item_tipo   FOREIGN KEY (tipo_garrafao_id) REFERENCES tipos_garrafao(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rota = agrupamento de pedidos atribuído a um veículo/entregador num dia.
CREATE TABLE rotas (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    data_rota       DATE NOT NULL,
    veiculo_id      BIGINT UNSIGNED NULL,
    entregador_id   BIGINT UNSIGNED NULL,
    status          ENUM('planejada','em_andamento','concluida','cancelada')
                        NOT NULL DEFAULT 'planejada',
    distancia_km    DECIMAL(8,2) NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_rota_tenant_data (tenant_id, data_rota),
    CONSTRAINT fk_rota_tenant     FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_rota_veiculo    FOREIGN KEY (veiculo_id) REFERENCES veiculos(id),
    CONSTRAINT fk_rota_entregador FOREIGN KEY (entregador_id) REFERENCES entregadores(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Paradas da rota (ordem de entrega). Liga rota <-> pedido.
CREATE TABLE rota_paradas (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    rota_id         BIGINT UNSIGNED NOT NULL,
    pedido_id       BIGINT UNSIGNED NOT NULL,
    ordem           INT NOT NULL DEFAULT 0,
    status          ENUM('pendente','entregue','falhou') NOT NULL DEFAULT 'pendente',
    entregue_em     DATETIME NULL,
    qtd_entregue    INT NULL,
    qtd_recolhido   INT NULL,                       -- vazios efetivamente recolhidos
    KEY idx_parada_rota (rota_id, ordem),
    CONSTRAINT fk_parada_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_parada_rota   FOREIGN KEY (rota_id)   REFERENCES rotas(id),
    CONSTRAINT fk_parada_pedido FOREIGN KEY (pedido_id) REFERENCES pedidos(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- PERMUTAS REALIZADAS NA ENTREGA  ⭐ onde o descasamento é medido.
-- Cada linha = uma troca efetiva: saiu 1+ cheios de uma validade,
-- entrou 1+ vazios de outra (ou da mesma). 'casado' indica se a
-- validade recebida bateu com a entregue. Este é o dado que alimenta
-- o KPI de taxa de casamento e o impacto no envelhecimento do pool.
CREATE TABLE permutas (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    parada_id       BIGINT UNSIGNED NULL,           -- vínculo com a entrega (rota_paradas)
    pedido_id       BIGINT UNSIGNED NULL,
    cliente_id      BIGINT UNSIGNED NOT NULL,
    tipo_garrafao_id BIGINT UNSIGNED NOT NULL,
    quantidade      INT NOT NULL,
    validade_entregue DATE NULL,                    -- validade do cheio que saiu
    validade_recebida DATE NULL,                    -- validade do vazio que entrou
    casado          TINYINT(1) NOT NULL DEFAULT 0,  -- 1 se validade_recebida == validade_entregue
    concessao       TINYINT(1) NOT NULL DEFAULT 0,  -- 1 se houve concessão p/ não perder a venda
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_permuta_tenant (tenant_id, criado_em),
    KEY idx_permuta_casado (tenant_id, casado),
    CONSTRAINT fk_permuta_tenant  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_permuta_parada  FOREIGN KEY (parada_id) REFERENCES rota_paradas(id),
    CONSTRAINT fk_permuta_pedido  FOREIGN KEY (pedido_id) REFERENCES pedidos(id),
    CONSTRAINT fk_permuta_cliente FOREIGN KEY (cliente_id) REFERENCES clientes(id),
    CONSTRAINT fk_permuta_tipo    FOREIGN KEY (tipo_garrafao_id) REFERENCES tipos_garrafao(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE centros_custo (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    nome            VARCHAR(120) NOT NULL,
    tipo            ENUM('operacional','administrativo','comercial','frota') NOT NULL DEFAULT 'operacional',
    ativo           TINYINT(1) NOT NULL DEFAULT 1,
    KEY idx_cc_tenant (tenant_id),
    CONSTRAINT fk_cc_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Lançamentos financeiros: a pagar e a receber em uma tabela só,
-- diferenciados por 'natureza'. Simplifica fluxo de caixa.
CREATE TABLE lancamentos (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    natureza        ENUM('receber','pagar') NOT NULL,
    centro_custo_id BIGINT UNSIGNED NULL,
    cliente_id      BIGINT UNSIGNED NULL,           -- se receber
    fornecedor_id   BIGINT UNSIGNED NULL,           -- se pagar
    pedido_id       BIGINT UNSIGNED NULL,           -- vínculo opcional
    descricao       VARCHAR(200) NOT NULL,
    valor           DECIMAL(12,2) NOT NULL,
    vencimento      DATE NOT NULL,
    pago_em         DATE NULL,
    valor_pago      DECIMAL(12,2) NULL,
    status          ENUM('pendente','pago','parcial','cancelado') NOT NULL DEFAULT 'pendente',
    forma           ENUM('dinheiro','pix','cartao','boleto','transferencia') NULL,
    criado_em       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_lanc_tenant_venc (tenant_id, vencimento),
    KEY idx_lanc_status (tenant_id, status),
    CONSTRAINT fk_lanc_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    CONSTRAINT fk_lanc_cc      FOREIGN KEY (centro_custo_id) REFERENCES centros_custo(id),
    CONSTRAINT fk_lanc_cliente FOREIGN KEY (cliente_id) REFERENCES clientes(id),
    CONSTRAINT fk_lanc_fornec  FOREIGN KEY (fornecedor_id) REFERENCES fornecedores(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;

-- =====================================================================
--  NOTAS DE DESIGN
--  1. TODA query da aplicação DEVE filtrar por tenant_id. Forçar isso
--     na camada de repositório/ORM — nunca confiar no programador.
--  2. garrafao_saldos é o "estado atual"; garrafao_movimentos é o
--     histórico imutável. O saldo deve ser sempre reconstruível a
--     partir dos movimentos (fonte da verdade = movimentos).
--  3. MODELO DE PERMUTA: o garrafão cheio é trocado pelo vazio do
--     cliente (1-por-1). Não se rastreia posse nem idade por unidade.
--     saldo_garrafoes em clientes é apenas o DESBALANÇO quando a troca
--     não fecha (ex.: cliente novo sem vazio); tende a zero no normal.
--  4. Fluxo de caixa = SELECT em lancamentos agrupado por período e
--     natureza. Realizado (pago_em not null) vs previsto (vencimento).
--  5. CONTROLE DE VALIDADE É O FOCO E A MAIOR ALAVANCA DE ECONOMIA.
--     A taxa de reposição é de ~15% AO MÊS (não ao ano): o pool se
--     renova por completo em ~6-7 meses. Cada ponto percentual a menos
--     nessa taxa representa economia direta e relevante. Por isso o
--     eixo central é a faixa de validade em garrafao_saldos.
--     Relatórios-chave (todos de alta prioridade):
--       (a) Envelhecimento do pool: quantos garrafões vencem por
--           semana/mês — visão quase em tempo real para agir antes.
--       (b) Giro FEFO (First-Expire, First-Out): despachar SEMPRE os
--           garrafões de validade mais próxima primeiro, para reduzir
--           perda por vencimento. A montagem de carga deve priorizar
--           os saldos com validade menor.
--       (c) Taxa de reposição mensal: movimentos 'avaria' + 'descarte'
--           sobre o tamanho do pool. valor_reposicao em tipos_garrafao
--           converte isso em custo (R$/mês) — KPI financeiro central.
--  6. MATERIAL e capacidade vivem em tipos_garrafao; saldos e
--     movimentos referenciam o tipo. (Sem ciclos por unidade — não se
--     aplica em permuta, pois a posse do vasilhame não é estável.)
--  7. PERMUTA CASADA POR VALIDADE (estratégia do distribuidor):
--     o pedido tem itens por validade (pedido_itens). Na entrega,
--     tenta-se devolver vazios da MESMA validade entregue, para não
--     degradar o perfil do pool. A regra é FLEXÍVEL: o sistema permite
--     concessão (entregar/receber outra validade p/ não perder a
--     venda) e a registra em permutas.concessao. A tabela permutas
--     guarda validade_entregue x validade_recebida e o flag casado.
--     KPI central: taxa de casamento = permutas casadas / total, que
--     pode ser quebrada por canal, cliente e entregador. Atacado
--     (politica_permuta='casar') visa alta taxa; varejo ('flexivel')
--     naturalmente terá taxa menor, e tudo bem.
--  8. INDUSTRIALIZAÇÃO (ENVASE) NA INDÚSTRIA: o garrafão é SEMPRE
--     propriedade do distribuidor. Na indústria NÃO há permuta nem
--     compra de vasilhame: o distribuidor descarrega seus vazios,
--     a indústria lava e enche OS MESMOS garrafões, e ele os recebe
--     cheios na expedição. Modelagem do ciclo:
--       - transferencia: depósito/veículo -> local 'industria' (vazios);
--       - envase: no local 'industria', vazio -> cheio (mesma qtd, mesmo
--         tipo). Gera lançamento a pagar de ÁGUA+SERVIÇO (não vasilhame);
--       - transferencia: 'industria' -> depósito/veículo (cheios).
--     O local 'industria' deixa visível quantos garrafões do pool estão
--     parados lá aguardando processo. O tamanho do pool NÃO muda no envase.
--     SOMENTE 'compra' (vasilhame novo) e 'descarte' alteram o pool — é o
--     que isola o custo de reposição (~15%/mês) do custo de água.
--  9. INTEGRAÇÃO COM ERP BV (indústria): o AquaG20 é sistema separado.
--     O ENVASE feito na indústria pode sincronizar via API REST — o ERP
--     BV expõe a ordem de produção/nota de serviço; o AquaG20 registra o
--     movimento 'envase' e o lançamento a pagar (água+serviço). A compra
--     de vasilhame novo é evento à parte. Bancos separados; integrar
--     pelas bordas.
-- =====================================================================

-- ---------------------------------------------------------------------
--  SEED DE EXEMPLO — tipos de garrafão (ajustar por distribuidor)
--  Material e capacidade apenas; validade é controlada por lote.
-- ---------------------------------------------------------------------
-- INSERT INTO tipos_garrafao
--   (tenant_id, nome, material, capacidade_litros, valor_reposicao)
-- VALUES
--   (1, 'Garrafão 20L Policarbonato',  'PC',  20.00, 35.00),
--   (1, 'Garrafão 20L Polipropileno',  'PP',  20.00, 30.00),
--   (1, 'Garrafão 20L PET',            'PET', 20.00, 25.00);
-- =====================================================================
