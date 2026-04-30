# Audit Dashboard

Página Abstra com duas visões para auditar uma organização:
- **Membros**: atividade recente de cada usuário (logins, ações, projetos criados).
- **Projetos**: nível de uso de cada projeto em produção, com base em sinais de integração externa (connectors, emails, AI).

Útil para revisão de membros inativos e identificação de projetos abandonados ou em uso.

## Aba Membros

### O que aparece na tela

- **Cards de resumo**: total de membros, ativos no período, inativos no período.
- **Cards de atividade**: total de projetos criados e total de ações registradas no período.
- **Tabela**: cada linha é um membro com email, status, último login, última atividade, dias inativo, projetos criados, total de ações e total de logins.
- **Filtros**: busca por email/ID, status (ativos/inativos), e ordenação por status, última atividade, logins, projetos criados ou ações.
- **Janela de tempo**: dois date pickers (De / Até) — qualquer intervalo até 31 dias.
- **Exportação CSV**: botão no canto superior direito da tabela.
- **Atualizar**: busca apenas as entradas novas desde o último carregamento, sem invalidar o cache.

### Classificação de atividade

Cada usuário é classificado como **ACTIVE** ou **INACTIVE** com base apenas no período selecionado:

- **ACTIVE**: pelo menos um login bem-sucedido OU uma ação na janela escolhida.
- **INACTIVE**: nenhuma atividade no período.

## Aba Projetos

### O que aparece na tela

- **Cards de resumo**: total de projetos da organização, projetos ativos no período, e totais por tipo de sinal (connector actions, emails, AI prompts).
- **Tabela**: cada linha é um projeto com nome, pasta, atividade total e a quebra por tipo (connectors, emails, AI prompts), além da data da última atividade.
- **Filtros**: busca por projeto/pasta/ID, filtro ativo/inativo, e várias opções de ordenação (atividade total, recência, ou cada métrica isolada).
- **Janela de tempo**: independente da aba Membros — cada aba pode ter um período diferente selecionado.

### Como medimos "atividade"

Para cada projeto, somamos três sinais que indicam uso em produção:

- **Connector actions** (`source=app`): chamadas a integrações externas (ex: enviar mensagem no Slack, gravar linha em planilha) feitas a partir do app implantado em produção. Não inclui chamadas feitas pelo editor durante desenvolvimento.
- **Emails enviados**: notificações reais disparadas pelo projeto. Exclui emails de teste enviados via "Enviar email de teste" no editor e emails recebidos pela plataforma.
- **AI prompts**: chamadas a LLMs feitas a partir do app em produção. Exclui chamadas feitas por agentes durante testes.

A coluna **Atividade total** é a soma desses três. Projetos com 0 nessa coluna não tiveram atividade em produção no período.

> **O que NÃO está incluído**: o sinal não cobre CPU/RAM nem workflows puros em Python que não chamam serviços externos. Um projeto que roda apenas transformações internas em tabelas Abstra, por exemplo, aparece com 0 atividade mesmo que esteja em uso.

### Carregar mais

A consulta de **connector actions** é a maior das três fontes — em organizações ativas pode passar de centenas de milhares de registros em 30 dias. Para evitar timeouts, carregamos no máximo **20.000 ações por chamada**.

Quando esse limite é atingido:

- Os totais aparecem com um asterisco (`*`) indicando que são parciais.
- Um botão **Carregar mais** aparece abaixo da tabela.
- Cada clique busca os próximos 20.000 a partir de onde paramos — os números crescem somando ao que já está na tela, sem refazer a consulta inteira.

Você pode clicar quantas vezes precisar até carregar tudo (ou até decidir que o que tem já é suficiente).

## Setup

1. No Abstra Console, configure uma conexão do tipo **Abstra Manager**:
   - `organizationToken` com o token da sua organização (gerado pela equipe Abstra).
   - Nomeie a conexão como `abstra-manager` (ou edite a constante `CONNECTION_NAME` em `page_audit_dashboard.py` para o nome que você escolher).
2. Publique o projeto.
3. Abra a página `Audit Dashboard - Authors Activity`.

## Limitações conhecidas

- **Janela máxima de 31 dias por consulta**. Limite imposto pela Manager API. Para auditar períodos maiores, navegue por janelas consecutivas usando os date pickers.
- **Timeout em organizações grandes**. Se o carregamento atingir o limite de 90 segundos, reduza a janela de consulta (por exemplo, de 30 para 7 dias) e tente novamente.
- **Aba Projetos só mede atividade externa**. Projetos com workflows que rodam só em Python ou usam apenas tabelas internas — sem chamar connectors, enviar emails, ou usar AI — aparecem com 0 atividade mesmo estando em uso. Use os números como indicador de "está acontecendo coisa visível para fora", não como medida absoluta de uso.
- **Dados parciais em connectors**. Para organizações com volume alto de connector actions, a tabela começa com os primeiros 20.000 registros e oferece o botão **Carregar mais** para continuar. O asterisco (`*`) ao lado dos números indica que ainda há mais dados a carregar.
