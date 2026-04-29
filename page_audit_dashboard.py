"""
Audit Dashboard Page - per-author activity for an arbitrary 31-day window.

Status semantics (window-relative, no all-time history):
- ACTIVE: had at least one login or action in the selected window
- INACTIVE: no activity in the selected window

Cache:
- Per-process in-memory cache. Each Abstra worker pod has its own copy; we
  cannot share across pods without external storage.
- Multi-window per log type: each fetched (from, to) range is stored as its
  own entry. Subsequent requests check whether any cached window contains
  the requested range and serve via local filtering when possible.
- LRU eviction by entry count. When a new fetch would push a log type past
  MAX_CACHED_ENTRIES_PER_TYPE, the oldest-timestamp entry is dropped.
"""

from abstra.pages import register_function
from abstra.connectors import run_connection_action
from lib_jinja import render_template
from datetime import datetime, timedelta
import time


CONNECTION_NAME = "abstra-manager"
CACHE_TTL_SECONDS = 300  # 5 minutes
MAX_CACHED_ENTRIES_PER_TYPE = 200_000  # rough memory cap; ~40-50 MB per type
MAX_WINDOW_DAYS = 31  # mirrors the server-side cap

# Each log type's cache is a list of {from, to, data, timestamp} entries.
# `members` is a singleton because it's not window-scoped.
_cache = {
    "auth_logs": [],
    "action_logs": [],
    "members": {"data": None, "timestamp": 0},
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _default_window(days: int = 30):
    """Returns the last `days` days as a (from_iso, to_iso) pair."""
    now = datetime.utcnow()
    return (
        (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


def _validate_window(from_iso: str, to_iso: str) -> None:
    """Raises ValueError if the window is invalid or exceeds the server cap."""
    from_dt = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    if from_dt > to_dt:
        raise ValueError("`from` must be before or equal to `to`")
    if (to_dt - from_dt).days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"Window exceeds {MAX_WINDOW_DAYS} days. Pick a smaller range."
        )


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _filter_by_window(logs: list, from_iso: str, to_iso: str) -> list:
    """Returns logs whose createdAt falls inside [from_iso, to_iso] (inclusive).
    Relies on ISO-8601 lex-sortability with zero-padded fields."""
    return [log for log in logs if from_iso <= log.get("createdAt", "") <= to_iso]


def _cache_lookup(log_type: str, from_iso: str, to_iso: str):
    """If any non-expired cached window contains the requested range, returns
    the filtered logs and bumps that entry's timestamp. Otherwise None."""
    now = time.time()
    entries = _cache[log_type]
    for entry in entries:
        if now - entry["timestamp"] > CACHE_TTL_SECONDS:
            continue
        # Subset check: requested range fits inside cached range
        if entry["from"] <= from_iso and entry["to"] >= to_iso:
            entry["timestamp"] = now  # LRU bump
            return _filter_by_window(entry["data"], from_iso, to_iso)
    return None


def _cache_insert(log_type: str, from_iso: str, to_iso: str, data: list) -> None:
    """Appends a new cached window. Evicts oldest entries if the per-type
    entry-count cap would be exceeded."""
    entries = _cache[log_type]
    entries.append(
        {"from": from_iso, "to": to_iso, "data": data, "timestamp": time.time()}
    )
    # Evict oldest until under the cap
    while sum(len(e["data"]) for e in entries) > MAX_CACHED_ENTRIES_PER_TYPE:
        if len(entries) <= 1:
            break  # don't evict the only window
        oldest = min(range(len(entries)), key=lambda i: entries[i]["timestamp"])
        entries.pop(oldest)


# ---------------------------------------------------------------------------
# Connector calls
# ---------------------------------------------------------------------------

PAGINATION_TIMEOUT_SECONDS = 25  # 5s margin under the frontend's 30s


class PaginationTimeoutError(Exception):
    """Raised when paginating through a log type takes too long."""
    pass


def _log_type_for_action(action_name: str) -> str:
    return "auth_logs" if "auth" in action_name else "action_logs"


def _paginate(action_name: str, from_iso: str, to_iso: str) -> list:
    """Pages through every entry the connector returns for the given window.
    Raises PaginationTimeoutError if total time exceeds the budget."""
    all_logs: list = []
    cursor = None
    pages_fetched = 0
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > PAGINATION_TIMEOUT_SECONDS:
            print(
                f"  {action_name}: TIMEOUT after {elapsed:.1f}s "
                f"({pages_fetched} pages, {len(all_logs)} logs)"
            )
            raise PaginationTimeoutError(
                f"Log fetch took longer than {PAGINATION_TIMEOUT_SECONDS}s. "
                f"Try a smaller window."
            )

        params = {"from": from_iso, "to": to_iso, "limit": 500}
        if cursor:
            params["cursor"] = cursor

        result = run_connection_action(CONNECTION_NAME, action_name, params)
        logs = result.get("entries", []) or []
        all_logs.extend(logs)
        pages_fetched += 1
        print(
            f"  {action_name}: page {pages_fetched} - {len(logs)} logs "
            f"(total: {len(all_logs)}, elapsed: {time.time() - start_time:.1f}s)"
        )

        cursor = result.get("nextCursor")
        if not cursor or len(logs) == 0:
            break
    return all_logs


def fetch_logs(action_name: str, from_iso: str, to_iso: str) -> list:
    """Cache-first read. Returns logs for the requested window, fetching from
    the connector only if no cached entry covers the range."""
    log_type = _log_type_for_action(action_name)

    cached = _cache_lookup(log_type, from_iso, to_iso)
    if cached is not None:
        print(f"  {action_name}: cache hit ({len(cached)} logs)")
        return cached

    all_logs = _paginate(action_name, from_iso, to_iso)
    _cache_insert(log_type, from_iso, to_iso, all_logs)
    return all_logs


def fetch_logs_incremental(action_name: str, from_iso: str, to_iso: str) -> list:
    """Refresh-friendly read. If a cached entry's `from` <= requested `from`,
    only the delta from `cached.to` to `to_iso` is fetched and appended (audit
    logs are append-only, so existing cached rows can't change). Falls back to
    a full fetch if no usable cache entry exists."""
    log_type = _log_type_for_action(action_name)

    # Pick the candidate cache entry: any entry whose `from` covers the start.
    # Prefer the one with the latest `to` (so the delta we have to fetch is
    # smallest).
    candidate = None
    for entry in _cache[log_type]:
        if entry["from"] <= from_iso:
            if candidate is None or entry["to"] > candidate["to"]:
                candidate = entry

    if candidate is None:
        all_logs = _paginate(action_name, from_iso, to_iso)
        _cache_insert(log_type, from_iso, to_iso, all_logs)
        return all_logs

    if candidate["to"] < to_iso:
        delta = _paginate(action_name, candidate["to"], to_iso)
        candidate["data"].extend(delta)
        candidate["to"] = to_iso
        print(f"  {action_name}: appended {len(delta)} new entries to cache")
    candidate["timestamp"] = time.time()  # LRU bump

    return _filter_by_window(candidate["data"], from_iso, to_iso)


def get_members_cached(force_refresh: bool = False) -> list:
    """Members aren't window-scoped. Single cache slot with TTL.
    `force_refresh=True` (used on the explicit Atualizar action) bypasses
    the cache so additions/removals are reflected immediately."""
    entry = _cache["members"]
    if (
        not force_refresh
        and entry["data"] is not None
        and time.time() - entry["timestamp"] <= CACHE_TTL_SECONDS
    ):
        return entry["data"]

    members = run_connection_action(CONNECTION_NAME, "get_members", {}) or []
    _cache["members"] = {"data": members, "timestamp": time.time()}
    return members


# ---------------------------------------------------------------------------
# Public functions exposed to the page
# ---------------------------------------------------------------------------

@register_function
def get_detailed_user_activity(
    from_iso: str = "",
    to_iso: str = "",
    incremental: bool = False,
):
    """Aggregates per-author activity within a (from_iso, to_iso) window.

    If no dates are passed, defaults to the last 30 days. The window may be
    any range up to MAX_WINDOW_DAYS; the server rejects anything wider.

    `incremental=True` is what the Atualizar button calls: instead of doing a
    cache-first read, it appends only rows that arrived after the cache's
    current `to`. Members are also re-fetched (membership can change). Used
    when the user explicitly asks for fresh data.

    Returns:
        users:  list of per-author records (see _build_user_record)
        stats:  totals across users
        window: {from, to} echoed back
    """
    if not from_iso or not to_iso:
        from_iso, to_iso = _default_window(days=30)

    try:
        _validate_window(from_iso, to_iso)
    except ValueError as e:
        raise Exception(f"INVALID_WINDOW: {e}")

    print(f"=== Per-author activity for {from_iso} -> {to_iso} (incremental={incremental}) ===")

    fetcher = fetch_logs_incremental if incremental else fetch_logs

    try:
        members = get_members_cached(force_refresh=incremental)
        members_by_email = {m["email"]: m for m in members if m.get("email")}
        members_by_author_id = {
            m["authorId"]: m for m in members if m.get("authorId")
        }
        print(f"  Members: {len(members_by_email)}")

        auth_logs = fetcher("get_auth_attempt_logs", from_iso, to_iso)
        action_logs = fetcher("get_action_logs", from_iso, to_iso)

        # Auth logs are keyed by email and have a status field.
        auth_by_email: dict = {}
        for log in auth_logs:
            email = log.get("email", "")
            created_at = log.get("createdAt")
            status = log.get("status")
            if not email:
                continue
            bucket = auth_by_email.setdefault(
                email,
                {
                    "last_login": None,
                    "total_logins": 0,
                    "success_count": 0,
                    "failure_count": 0,
                },
            )
            bucket["total_logins"] += 1
            if status == "success":
                bucket["success_count"] += 1
                if not bucket["last_login"] or created_at > bucket["last_login"]:
                    bucket["last_login"] = created_at
            else:
                bucket["failure_count"] += 1

        # Action logs are keyed by authorId; resolve email via members_by_author_id.
        # Logs whose author is no longer a member are skipped.
        action_by_email: dict = {}
        for log in action_logs:
            author_id = log.get("authorId", "")
            if not author_id:
                continue
            member = members_by_author_id.get(author_id)
            if not member or not member.get("email"):
                continue
            email = member["email"]
            event = (log.get("event") or "").lower()
            created_at = log.get("createdAt")
            bucket = action_by_email.setdefault(
                email,
                {
                    "last_activity": None,
                    "total_actions": 0,
                    "projects_created": 0,
                    "last_project_created": None,
                },
            )
            bucket["total_actions"] += 1
            if not bucket["last_activity"] or created_at > bucket["last_activity"]:
                bucket["last_activity"] = created_at

            if event == "createproject":
                bucket["projects_created"] += 1
                if (
                    not bucket["last_project_created"]
                    or created_at > bucket["last_project_created"]
                ):
                    bucket["last_project_created"] = created_at

        # Combine member list with the per-email aggregates.
        all_users = []
        for email, member in members_by_email.items():
            all_users.append(
                _build_user_record(email, member, auth_by_email, action_by_email)
            )

        # Sort: ACTIVE first, then by last_activity desc, then email
        all_users.sort(
            key=lambda u: (
                0 if u["status"] == "ACTIVE" else 1,
                -(_iso_to_epoch(u["last_activity"]) or 0),
                u["author_email"],
            )
        )

        stats = {
            "total": len(all_users),
            "active": sum(1 for u in all_users if u["status"] == "ACTIVE"),
            "inactive": sum(1 for u in all_users if u["status"] == "INACTIVE"),
            "total_projects": sum(u["projects_created"] for u in all_users),
            "total_actions": sum(u["total_actions"] for u in all_users),
        }
        print(
            f"=== {stats['total']} users, {stats['active']} active, "
            f"{stats['inactive']} inactive ==="
        )
        return {"users": all_users, "stats": stats, "window": {"from": from_iso, "to": to_iso}}

    except PaginationTimeoutError as e:
        raise Exception(f"TIMEOUT_PAGINATION: {e}")
    except Exception as e:
        if str(e).startswith(("INVALID_WINDOW:", "TIMEOUT_PAGINATION:")):
            raise
        import traceback
        traceback.print_exc()
        raise Exception(f"Erro na consulta: {e}")


def _build_user_record(
    email: str,
    member: dict,
    auth_by_email: dict,
    action_by_email: dict,
) -> dict:
    auth = auth_by_email.get(
        email,
        {"last_login": None, "total_logins": 0, "success_count": 0, "failure_count": 0},
    )
    action = action_by_email.get(
        email,
        {
            "last_activity": None,
            "total_actions": 0,
            "projects_created": 0,
            "last_project_created": None,
        },
    )

    last_login = auth["last_login"]
    last_action = action["last_activity"]
    if last_login and last_action:
        last_activity = max(last_login, last_action)
    else:
        last_activity = last_login or last_action

    status = "ACTIVE" if last_activity else "INACTIVE"

    return {
        "author_id": member.get("id", ""),
        "author_email": email,
        "author_name": email.split("@")[0] if "@" in email else email,
        "status": status,
        "last_login": last_login,
        "last_activity": last_activity,
        "total_logins": auth["total_logins"],
        "success_count": auth["success_count"],
        "failure_count": auth["failure_count"],
        "projects_created": action["projects_created"],
        "last_project_created": action["last_project_created"],
        "total_actions": action["total_actions"],
    }


def _iso_to_epoch(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Diagnostic helpers (kept from the original; useful for debugging)
# ---------------------------------------------------------------------------

@register_function
def discover_tables():
    """Returns a small sample plus the set of distinct event types seen in the
    last 7 days. Useful when wiring up a new dashboard."""
    try:
        from_iso, to_iso = _default_window(days=7)
        auth_result = run_connection_action(
            CONNECTION_NAME,
            "get_auth_attempt_logs",
            {"from": from_iso, "to": to_iso, "limit": 10},
        )
        action_result = run_connection_action(
            CONNECTION_NAME,
            "get_action_logs",
            {"from": from_iso, "to": to_iso, "limit": 10},
        )
        auth_logs = auth_result.get("entries", []) or []
        action_logs = action_result.get("entries", []) or []
        event_types = sorted({log.get("event") for log in action_logs if log.get("event")})
        return {
            "auth_logs_sample": auth_logs[:3],
            "action_logs_sample": action_logs[:3],
            "event_types_found": event_types,
            "window": {"from": from_iso, "to": to_iso},
        }
    except Exception as e:
        return {"error": str(e)}


@register_function
def __render__():
    return render_template("audit_dashboard.html")
