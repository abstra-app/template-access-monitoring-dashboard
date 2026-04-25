"""
Audit Dashboard Page - Exibe relação de authors x última atividade.
"""

from abstra.pages import register_function
from abstra.connectors import run_connection_action
from lib_jinja import render_template
from datetime import datetime, timedelta


CONNECTION_NAME = "abstra-manager"  # Nome da conexão no Abstra Console


def get_date_range(days: int = 30):
    """Retorna o range de datas para consulta (últimos N dias)."""
    now = datetime.utcnow()
    from_date = now - timedelta(days=days)
    return {
        "from": from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    }


@register_function
def get_authors_activity():
    """
    Cruza membros da organização com logs de auth para retornar
    última atividade por author, incluindo quem nunca logou no período.
    """
    print("Consultando members + auth logs")

    try:
        date_range = get_date_range(days=30)

        members = run_connection_action(CONNECTION_NAME, "get_members", {}) or []
        members_by_email = {m["email"]: m for m in members if m.get("email")}
        print(f"Recebidos {len(members_by_email)} members")

        all_logs = []
        cursor = None
        while True:
            params = {
                "from": date_range["from"],
                "to": date_range["to"],
                "limit": 500,
            }
            if cursor:
                params["cursor"] = cursor

            result = run_connection_action(CONNECTION_NAME, "get_auth_attempt_logs", params)
            logs = result.get("entries", []) or []
            all_logs.extend(logs)
            print(f"Recebidos {len(logs)} logs (total acumulado: {len(all_logs)})")

            cursor = result.get("nextCursor")
            if not cursor or len(logs) == 0:
                break

        authors_map = {}
        for log in all_logs:
            email = log.get("email", "")
            created_at = log.get("createdAt")
            status = log.get("status")
            if not email:
                continue

            if email not in authors_map:
                member = members_by_email.get(email)
                authors_map[email] = {
                    "author_id": member["id"] if member else "",
                    "author_email": email,
                    "author_name": email.split("@")[0] if "@" in email else email,
                    "last_activity": created_at,
                    "total_logins": 0,
                    "success_count": 0,
                    "failure_count": 0,
                }

            authors_map[email]["total_logins"] += 1
            if status == "success":
                authors_map[email]["success_count"] += 1
            else:
                authors_map[email]["failure_count"] += 1

            if created_at and created_at > authors_map[email]["last_activity"]:
                authors_map[email]["last_activity"] = created_at

        for email, member in members_by_email.items():
            if email in authors_map:
                continue
            authors_map[email] = {
                "author_id": member["id"],
                "author_email": email,
                "author_name": email.split("@")[0] if "@" in email else email,
                "last_activity": None,
                "total_logins": 0,
                "success_count": 0,
                "failure_count": 0,
            }

        records = list(authors_map.values())
        records.sort(key=lambda x: x["last_activity"] or "", reverse=True)

        print(f"Retornados {len(records)} authors ({sum(1 for r in records if not r['last_activity'])} sem login)")
        return records

    except Exception as e:
        print(f"Erro ao consultar connector: {e}")
        raise Exception(f"Erro na consulta: {str(e)}")


@register_function
def discover_tables():
    """
    Retorna informações sobre os logs disponíveis via connector.
    """
    try:
        date_range = get_date_range(days=7)
        
        # Busca amostra de auth logs
        auth_result = run_connection_action(
            CONNECTION_NAME,
            "get_auth_attempt_logs",
            {
                "from": date_range["from"],
                "to": date_range["to"],
                "limit": 10
            }
        )
        print("auth result", auth_result)
        
        # Busca amostra de action logs
        action_result = run_connection_action(
            CONNECTION_NAME,
            "get_action_logs",
            {
                "from": date_range["from"],
                "to": date_range["to"],
                "limit": 10
            }
        )
        
        auth_logs = auth_result.get("entries", []) or []
        action_logs = action_result.get("entries", []) or []

        return {
            "auth_logs_sample": auth_logs[:3],
            "action_logs_sample": action_logs[:3],
            "date_range": date_range
        }
        
    except Exception as e:
        return {"error": str(e)}


@register_function
def test_query(log_type: str = "auth", days: int = 7):
    """
    Permite testar uma consulta aos logs via connector.
    """
    try:
        date_range = get_date_range(days=days)
        
        action_map = {
            "auth": "get_auth_attempt_logs",
            "action": "get_action_logs",
            "email": "get_email_notification_logs",
            "connector": "get_connector_action_logs",
            "ai": "get_ai_prompt_logs"
        }
        
        action_name = action_map.get(log_type, "get_auth_attempt_logs")
        
        result = run_connection_action(
            CONNECTION_NAME,
            action_name,
            {
                "from": date_range["from"],
                "to": date_range["to"],
                "limit": 100
            }
        )
        print(result)

        logs = result.get("entries", []) or []

        return {
            "success": True,
            "log_type": log_type,
            "action_used": action_name,
            "row_count": len(logs),
            "has_more": result.get("nextCursor") is not None,
            "sample": logs[:5]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@register_function
def __render__():
    """Renderiza a página HTML."""
    return render_template("audit_dashboard.html")
