# Login Dashboard

Página Abstra que mostra a relação de membros da organização x última vez que cada um fez login, incluindo quem nunca logou. Útil para auditoria de acesso e revisão de membros inativos.

## O que aparece na tela

- **Cards de resumo**: Total de membros, ativos nos últimos 30 dias, inativos, e quem nunca logou.
- **Tabela**: Cada linha é um membro com email, última atividade, dias inativo, total de logins (sucessos + falhas) e status.
- **Filtros**: Por busca livre (email/ID), por status, e ordenação por última atividade ou total de logins.
- **Exportação CSV**: Botão no canto superior direito da tabela.

## Como funciona

A página usa a conexão **Abstra Manager** para puxar dois conjuntos de dados:

1. `get_members` — lista todos os membros da organização.
2. `get_auth_attempt_logs` — lista as tentativas de login dos últimos 30 dias (paginado).

O backend cruza os dois: para cada membro, encontra a última tentativa de login. Quem aparece em `get_members` mas não tem nenhum login no período é classificado como **Never Logged In**.

## Setup

1. No Abstra Console, configure uma conexão do tipo **Abstra Manager**:
   - Preencha o campo `organizationToken` com o token da sua organização.
   - Dê o nome `abstra-manager` à conexão (ou edite a constante `CONNECTION_NAME` em `page_audit_dashboard.py` para o nome que você escolher).
2. Publique o projeto.
3. Abra a página `Audit Dashboard - Authors Activity`.

## Limitações

- O período é fixo em **30 dias**. Para alterar, edite o parâmetro `days=30` em `get_authors_activity` (`page_audit_dashboard.py`).
- "Última atividade" reflete apenas **logins**, não ações dentro do editor.
