# Audit Dashboard

Página Abstra que mostra os membros de uma organização e a atividade recente de cada um, em uma janela de tempo configurável. Útil para auditoria de acesso e revisão de membros inativos.

## O que aparece na tela

- **Cards de resumo**: total de membros, ativos no período, inativos no período.
- **Cards de atividade**: total de projetos criados e total de ações registradas no período.
- **Tabela**: cada linha é um membro com email, status, último login, última atividade, dias inativo, projetos criados, total de ações e total de logins.
- **Filtros**: busca por email/ID, status (ativos/inativos), e ordenação por status, atividade, logins, projetos criados ou ações.
- **Janela de tempo**: dois date pickers (De / Até) — qualquer intervalo até 31 dias.
- **Exportação CSV**: botão no canto superior direito da tabela.
- **Atualizar**: faz refresh incremental — busca apenas as entradas novas desde o último carregamento, sem invalidar o cache.

## Classificação de atividade

Cada usuário é classificado como **ACTIVE** ou **INACTIVE** com base apenas no período selecionado:

- **ACTIVE**: pelo menos um login bem-sucedido OU uma ação na janela escolhida.
- **INACTIVE**: nenhuma atividade no período.

## Como funciona

A página usa a conexão **Abstra Manager** com três actions:

1. `get_members` — membros da organização (id, authorId, email, folders).
2. `get_auth_attempt_logs` — tentativas de login no período (paginado).
3. `get_action_logs` — ações registradas via Manager API no período (paginado). Hoje são contabilizados `createProject` e o total de ações; outros eventos específicos serão expostos conforme a perf do endpoint melhorar.

O backend cruza os três conjuntos:
- Auth logs são agrupados por `email`.
- Action logs são agrupados por `authorId` e mapeados para email via os membros.
- Cada membro recebe um registro consolidado com status, último login, última atividade e contadores.

## Cache

Cache em memória, por processo de worker do Abstra:

- **Members**: TTL de 5 min, slot único.
- **Auth/action logs**: cache multi-janela. Cada `(from, to)` consultado é armazenado como uma entry separada por 5 min; consultas cuja janela esteja contida em uma entry existente são servidas localmente sem nova chamada ao connector. Eviction LRU quando o total de entradas passa do limite por tipo de log.

O botão **Atualizar** faz refresh incremental: localiza a janela cached que cobre o início do intervalo selecionado e busca apenas o delta `(cache.to → agora)`, anexando ao cache. Possível porque audit logs são append-only — entradas existentes não mudam.

## Setup

1. No Abstra Console, configure uma conexão do tipo **Abstra Manager**:
   - `organizationToken` com o token da sua organização (gerado pela equipe Abstra).
   - Nomeie a conexão como `abstra-manager` (ou edite a constante `CONNECTION_NAME` em `page_audit_dashboard.py` para o nome que você escolher).
2. Publique o projeto.
3. Abra a página `Audit Dashboard - Authors Activity`.

## Limitações conhecidas

- **Janela máxima de 31 dias por consulta**. Limite imposto pela Manager API. Para auditar períodos maiores, navegue por janelas consecutivas usando os date pickers.
- **Timeout em organizações grandes**. Se o carregamento atingir o limite de 30 segundos, reduza a janela de consulta (por exemplo, de 30 para 7 dias) e tente novamente.
