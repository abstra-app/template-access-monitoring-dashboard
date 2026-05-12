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
# `members` and `organization` are singletons because they're not window-scoped.
_cache = {
    "auth_logs": [],
    "org_action_logs": [],
    "connector_action_logs": [],
    "email_notifications": [],
    "ai_prompts": [],
    "members": {"data": None, "timestamp": 0},
    "organization": {"data": None, "timestamp": 0},
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

PAGINATION_TIMEOUT_SECONDS = 85  # 5s margin under the frontend's 90s
# Default row cap for the connector_action_logs pagination, applied alongside
# the time budget. Trips PaginationTimeoutError (with partial_logs populated)
# once exceeded; the dashboard surfaces a "Load more" button so the user can
# explicitly opt into a larger cap.
DEFAULT_CONNECTOR_MAX_ROWS = 20_000


class PaginationTimeoutError(Exception):
    """Raised when paginating through a log type stops early — either the
    time budget elapsed or a row cap (max_rows) was reached.

    `partial_logs` carries everything _paginate managed to fetch before
    stopping. `next_cursor` is the connector cursor we'd resume from on a
    follow-up call (the Projetos tab uses this to power the "Carregar mais"
    button — fetches the NEXT chunk instead of refetching everything)."""

    def __init__(
        self,
        message: str,
        partial_logs: list | None = None,
        next_cursor: str | None = None,
    ):
        super().__init__(message)
        self.partial_logs = partial_logs or []
        self.next_cursor = next_cursor


_LOG_TYPE_BY_ACTION = {
    "get_auth_attempt_logs": "auth_logs",
    "get_organization_action_logs": "org_action_logs",
    "get_connector_action_logs": "connector_action_logs",
    "get_email_notification_logs": "email_notifications",
    "get_ai_prompt_logs": "ai_prompts",
}


def _log_type_for_action(action_name: str) -> str:
    return _LOG_TYPE_BY_ACTION.get(action_name, "org_action_logs")


def _paginate(
    action_name: str,
    from_iso: str,
    to_iso: str,
    extra_params: dict | None = None,
    max_rows: int | None = None,
    start_cursor: str | None = None,
) -> list:
    """Pages through entries the connector returns for the given window.

    `extra_params` is merged into each paginated request so callers can apply
    server-side filters (e.g. `{"source": "app"}` on connector_action_logs).

    `max_rows`, when set, stops pagination once that many rows have been
    accumulated and raises PaginationTimeoutError with the partial data and
    the cursor where we stopped (so a continuation call can resume from
    there).

    `start_cursor`, when set, resumes pagination from that point instead of
    starting from page 1 — used by the Projetos tab's "Carregar mais" flow.

    Raises PaginationTimeoutError if total time exceeds the per-action budget
    or the row cap is exceeded; the exception carries `partial_logs` and
    `next_cursor` for resumption."""
    all_logs: list = []
    cursor = start_cursor
    pages_fetched = 0
    start_time = time.time()
    label = action_name + (
        f" [{','.join(f'{k}={v}' for k, v in extra_params.items())}]" if extra_params else ""
    )

    while True:
        elapsed = time.time() - start_time
        if elapsed > PAGINATION_TIMEOUT_SECONDS:
            print(
                f"  {label}: TIMEOUT after {elapsed:.1f}s "
                f"({pages_fetched} pages, {len(all_logs)} logs) — returning partial"
            )
            raise PaginationTimeoutError(
                f"Log fetch took longer than {PAGINATION_TIMEOUT_SECONDS}s. "
                f"Try a smaller window.",
                partial_logs=all_logs,
                next_cursor=cursor,
            )
        if max_rows is not None and len(all_logs) >= max_rows:
            print(
                f"  {label}: ROW CAP reached at {len(all_logs)} logs "
                f"({pages_fetched} pages, {elapsed:.1f}s) — returning partial"
            )
            raise PaginationTimeoutError(
                f"Row cap of {max_rows} reached.",
                partial_logs=all_logs,
                next_cursor=cursor,
            )

        params: dict = {"from": from_iso, "to": to_iso, "limit": 500}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor

        result = run_connection_action(CONNECTION_NAME, action_name, params)
        logs = result.get("entries", []) or []
        all_logs.extend(logs)
        pages_fetched += 1
        print(
            f"  {label}: page {pages_fetched} - {len(logs)} logs "
            f"(total: {len(all_logs)}, elapsed: {time.time() - start_time:.1f}s)"
        )

        cursor = result.get("nextCursor")
        if not cursor or len(logs) == 0:
            break
    return all_logs


def _paginate_connector_production(
    from_iso: str,
    to_iso: str,
    max_rows: int | None = None,
    start_cursor: str | None = None,
) -> list:
    """Server-side filtered fetch for connector_action_logs, source=app only.

    `source=app` represents deployed-runtime invocations (workflows running in
    production env). Other sources are excluded:
    - `editor`: developer testing in the IDE.
    - `ai`: agent-initiated calls, may run in either dev or prod.
    - `api`: external API callers hitting the project's endpoints — these are
      consumers OF the project, not the project running, so they're not the
      "is this project being executed in production" signal we're after.

    Single paginated call (vs. two with source=app+api) — cuts both data
    volume and total time roughly in half. `max_rows` caps how much we'll
    accumulate before stopping (see _paginate).
    """
    return _paginate(
        "get_connector_action_logs",
        from_iso,
        to_iso,
        {"source": "app"},
        max_rows=max_rows,
        start_cursor=start_cursor,
    )


def fetch_logs(
    action_name: str,
    from_iso: str,
    to_iso: str,
    max_rows: int | None = None,
) -> list:
    """Cache-first read. Returns logs for the requested window, fetching from
    the connector only if no cached entry covers the range.

    For connector_action_logs we use a server-side production filter — see
    _paginate_connector_production. The cache slot stores production-only
    data; nothing in the dashboard reads the unfiltered version, so we don't
    need a separate cache slot.

    `max_rows` (only used by the connector path) caps how much we'll fetch
    before raising PaginationTimeoutError with partial data."""
    log_type = _log_type_for_action(action_name)

    cached = _cache_lookup(log_type, from_iso, to_iso)
    if cached is not None:
        print(f"  {action_name}: cache hit ({len(cached)} logs)")
        return cached

    if action_name == "get_connector_action_logs":
        all_logs = _paginate_connector_production(from_iso, to_iso, max_rows=max_rows)
    else:
        all_logs = _paginate(action_name, from_iso, to_iso)
    _cache_insert(log_type, from_iso, to_iso, all_logs)
    return all_logs


def fetch_logs_incremental(
    action_name: str,
    from_iso: str,
    to_iso: str,
    max_rows: int | None = None,
) -> list:
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

    paginate = (
        (lambda f, t: _paginate_connector_production(f, t, max_rows=max_rows))
        if action_name == "get_connector_action_logs"
        else (lambda f, t: _paginate(action_name, f, t))
    )

    if candidate is None:
        all_logs = paginate(from_iso, to_iso)
        _cache_insert(log_type, from_iso, to_iso, all_logs)
        return all_logs

    if candidate["to"] < to_iso:
        delta = paginate(candidate["to"], to_iso)
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


def get_organization_cached(force_refresh: bool = False) -> dict:
    """Organization metadata (folders + projects). Single cache slot with TTL.
    Used by the Projetos tab to resolve project_id -> {name, folder_name}."""
    entry = _cache["organization"]
    if (
        not force_refresh
        and entry["data"] is not None
        and time.time() - entry["timestamp"] <= CACHE_TTL_SECONDS
    ):
        return entry["data"]

    org = run_connection_action(CONNECTION_NAME, "get_organization", {}) or {}
    _cache["organization"] = {"data": org, "timestamp": time.time()}
    return org


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
        action_logs = fetcher("get_organization_action_logs", from_iso, to_iso)

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
# Per-project activity proxy (production signal across 3 log types)
# ---------------------------------------------------------------------------

# Sources that count as production-side activity, used to filter rows whose
# `source` field tells us where the call came from. Entries from `editor`
# (devs testing in the IDE) or `ai` (agent-initiated) are excluded so the
# count reflects deployed-app traffic.
#
# For connector_action_logs we apply this filter SERVER-SIDE via two
# paginated calls (see _paginate_connector_production). For ai_prompts the
# connector doesn't expose a source filter, so this helper is used
# client-side after the fetch.
_PRODUCTION_SOURCES = {"app", "api"}


def _is_production_source(source: str | None) -> bool:
    return source is None or source in _PRODUCTION_SOURCES


# email_notifications.kind identifies the type of mail. We exclude two cases:
# - kinds ending in `:mock` are sent by the editor's "Test email" feature
#   (sendMockEmail, controllers/email.ts:58), not real production mail.
# - `received` records inbound mail the system received, not production
#   outbound activity.
def _is_production_email_kind(kind: str | None) -> bool:
    if not kind:
        return False
    if kind.endswith(":mock"):
        return False
    if kind == "received":
        return False
    return True


def _empty_project_bucket() -> dict:
    return {
        "connector_actions": 0,
        "emails_sent": 0,
        "ai_prompts": 0,
        "last_activity": None,
    }


def _accumulate_project(per_project: dict, log: dict, count_field: str) -> None:
    pid = log.get("projectId")
    if not pid:
        return
    bucket = per_project.setdefault(pid, _empty_project_bucket())
    bucket[count_field] += 1
    created_at = log.get("createdAt")
    if created_at and (not bucket["last_activity"] or created_at > bucket["last_activity"]):
        bucket["last_activity"] = created_at


def _resolve_project_metadata(force_refresh: bool) -> dict:
    """Returns {project_id: {name, folder_name}} from get_organization."""
    org_data = get_organization_cached(force_refresh=force_refresh)
    projects_meta: dict = {}
    for folder in (org_data or {}).get("folders", []) or []:
        folder_name = folder.get("name", "")
        for proj in folder.get("projects", []) or []:
            projects_meta[proj["id"]] = {
                "name": proj.get("name", ""),
                "folder_name": folder_name,
            }
    return projects_meta


@register_function
def get_project_activity_lite(
    from_iso: str = "",
    to_iso: str = "",
    incremental: bool = False,
):
    """Fast pass for the Projetos tab. Aggregates emails sent + AI prompts
    per project (the two smaller log types). Connector actions are NOT
    included here — fetch them separately via get_project_connector_activity
    in the background, since connector_action_logs is much larger and slower.

    Returns the same shape as get_project_connector_activity merge target,
    with `connector_actions` always 0. Frontend should call
    get_project_connector_activity afterwards and merge the results.
    """
    if not from_iso or not to_iso:
        from_iso, to_iso = _default_window(days=30)

    try:
        _validate_window(from_iso, to_iso)
    except ValueError as e:
        raise Exception(f"INVALID_WINDOW: {e}")

    print(f"=== Project activity LITE for {from_iso} -> {to_iso} (incremental={incremental}) ===")

    fetcher = fetch_logs_incremental if incremental else fetch_logs

    try:
        projects_meta = _resolve_project_metadata(force_refresh=incremental)
        print(f"  Projects: {len(projects_meta)}")

        email_logs = fetcher("get_email_notification_logs", from_iso, to_iso)
        prompt_logs = fetcher("get_ai_prompt_logs", from_iso, to_iso)

        per_project: dict = {}
        for log in email_logs:
            if not _is_production_email_kind(log.get("kind")):
                continue
            _accumulate_project(per_project, log, "emails_sent")
        for log in prompt_logs:
            if not _is_production_source(log.get("source")):
                continue
            _accumulate_project(per_project, log, "ai_prompts")

        all_projects = []
        for pid, meta in projects_meta.items():
            bucket = per_project.get(pid) or _empty_project_bucket()
            all_projects.append(_build_project_record(pid, meta, bucket))
        for pid, bucket in per_project.items():
            if pid in projects_meta:
                continue
            all_projects.append(
                _build_project_record(
                    pid,
                    {"name": "(unknown)", "folder_name": "(unknown)"},
                    bucket,
                )
            )

        all_projects.sort(
            key=lambda p: (
                -p["total_activity"],
                -(_iso_to_epoch(p["last_activity"]) or 0),
                p["project_name"],
            )
        )

        stats = {
            "total_projects": len(all_projects),
            "active_projects": sum(1 for p in all_projects if p["total_activity"] > 0),
            "total_connector_actions": 0,  # filled in by the connector pass
            "total_emails_sent": sum(p["emails_sent"] for p in all_projects),
            "total_ai_prompts": sum(p["ai_prompts"] for p in all_projects),
        }
        return {
            "projects": all_projects,
            "stats": stats,
            "window": {"from": from_iso, "to": to_iso},
        }

    except PaginationTimeoutError as e:
        raise Exception(f"TIMEOUT_PAGINATION: {e}")
    except Exception as e:
        if str(e).startswith(("INVALID_WINDOW:", "TIMEOUT_PAGINATION:")):
            raise
        import traceback
        traceback.print_exc()
        raise Exception(f"Erro na consulta: {e}")


@register_function
def get_project_connector_activity(
    from_iso: str = "",
    to_iso: str = "",
    incremental: bool = False,
    max_rows: int | None = None,
    start_cursor: str | None = None,
):
    """Slow pass for the Projetos tab. Aggregates connector_action_logs
    (filtered to source=app) by project. Designed to be called in the
    background after get_project_activity_lite has already rendered the
    table; the frontend merges the result by project_id.

    `max_rows` defaults to DEFAULT_CONNECTOR_MAX_ROWS. When pagination hits
    that cap (or the time budget), the response is returned with
    `partial=true`, the truncated data, and `next_cursor` pointing to where
    the next page would start. The dashboard's "Carregar mais" button calls
    back with that cursor in `start_cursor` to fetch the NEXT chunk —
    instead of refetching from page 1 with a larger cap.

    Cache behavior:
    - `start_cursor=None` (initial fresh call): goes through fetch_logs,
      cache-aware. If a covering window is already cached AND complete, we
      return it instantly.
    - `start_cursor=<cursor>` (continuation call): bypasses cache and
      paginates directly from the given cursor. The response represents the
      DELTA fetched in this call only; the frontend is responsible for
      merging into accumulated state.

    Returns:
        by_project:  {project_id: {count: int, last_activity: iso}}
                     — counts in THIS call only (delta semantics)
        total:       count fetched in THIS call
        partial:     true if pagination hit the row cap or time budget
        next_cursor: cursor to continue from (only meaningful when partial)
        window:      {from, to} echoed back
    """
    if max_rows is None:
        max_rows = DEFAULT_CONNECTOR_MAX_ROWS
    if not from_iso or not to_iso:
        from_iso, to_iso = _default_window(days=30)

    try:
        _validate_window(from_iso, to_iso)
    except ValueError as e:
        raise Exception(f"INVALID_WINDOW: {e}")

    print(
        f"=== Project connector activity for {from_iso} -> {to_iso} "
        f"(incremental={incremental}, max_rows={max_rows}, "
        f"continuation={start_cursor is not None}) ==="
    )

    # connector_action_logs can have very high volume (tens of thousands of
    # rows in 30 days for a busy org). When pagination exceeds the budget we
    # accept the truncated data and flag it `partial=true` instead of erroring
    # — the dashboard renders the partial counts with a marker so the user
    # knows what they're looking at, and offers a "Carregar mais" button to
    # continue from the saved cursor.
    partial = False
    next_cursor = None
    try:
        if start_cursor:
            # Continuation: bypass cache, paginate directly from cursor.
            # Cache only stores complete windows; partial data is never
            # cached, so a continuation call always re-paginates from where
            # we left off rather than from page 1.
            connector_logs = _paginate_connector_production(
                from_iso, to_iso, max_rows=max_rows, start_cursor=start_cursor,
            )
        else:
            # Fresh call: cache-aware path.
            # Server-side filtered to source=app via the connector's `source`
            # param (see _paginate_connector_production). No need to filter again.
            fetcher = fetch_logs_incremental if incremental else fetch_logs
            connector_logs = fetcher(
                "get_connector_action_logs", from_iso, to_iso, max_rows=max_rows,
            )
    except PaginationTimeoutError as e:
        connector_logs = e.partial_logs
        next_cursor = e.next_cursor
        partial = True
        print(
            f"  PARTIAL: {len(connector_logs)} connector logs accumulated "
            f"before stop; next_cursor={'<set>' if next_cursor else '<none>'}"
        )
    except Exception as e:
        if str(e).startswith(("INVALID_WINDOW:", "TIMEOUT_PAGINATION:")):
            raise
        import traceback
        traceback.print_exc()
        raise Exception(f"Erro na consulta: {e}")

    by_project: dict = {}
    total = 0
    for log in connector_logs:
        pid = log.get("projectId")
        if not pid:
            continue
        entry = by_project.setdefault(pid, {"count": 0, "last_activity": None})
        entry["count"] += 1
        total += 1
        created_at = log.get("createdAt")
        if created_at and (
            not entry["last_activity"] or created_at > entry["last_activity"]
        ):
            entry["last_activity"] = created_at

    print(
        f"  {total} connector actions across {len(by_project)} projects "
        f"(partial={partial}, next_cursor={'<set>' if next_cursor else '<none>'})"
    )
    return {
        "by_project": by_project,
        "total": total,
        "partial": partial,
        "next_cursor": next_cursor,
        "window": {"from": from_iso, "to": to_iso},
    }


def _build_project_record(pid: str, meta: dict, bucket: dict) -> dict:
    return {
        "project_id": pid,
        "project_name": meta.get("name", ""),
        "folder_name": meta.get("folder_name", ""),
        "connector_actions": bucket["connector_actions"],
        "emails_sent": bucket["emails_sent"],
        "ai_prompts": bucket["ai_prompts"],
        "total_activity": bucket["connector_actions"]
        + bucket["emails_sent"]
        + bucket["ai_prompts"],
        "last_activity": bucket["last_activity"],
    }


# ---------------------------------------------------------------------------
# Per-project developer activity (project-scoped audit log)
# ---------------------------------------------------------------------------

# Event categories for the dev-activity rollup. Lowercased to match the
# normalization the per-author code already uses (`event.lower()` at the
# accumulator). Reads (describeTable/listTables/getRows/listUsers/...) are
# deliberately excluded — these are mutations only, since this is an
# "activity" signal, not an audit-trail surface.
_BUILD_EVENTS = {"createbuild"}
_TABLE_OP_EVENTS = {
    "createtable",
    "updatetable",
    "deletetable",
    "createrow",
    "updaterow",
    "deleterow",
    "executequery",
}
_FILE_OP_EVENTS = {"movefile", "uploadfile", "createfolder"}
_ACCESS_CHANGE_EVENTS = {
    "adduser",
    "removeuser",
    "addrole",
    "removerole",
    "updaterole",
}

# get_project_action_logs caps projectIds at 100 (Postgres index degrades past
# ~150). Larger orgs are split into multiple sequential batches.
_PROJECT_ACTIONS_BATCH_SIZE = 100


def _empty_dev_bucket() -> dict:
    return {
        "dev_actions": 0,
        "builds": 0,
        "table_ops": 0,
        "file_ops": 0,
        "access_changes": 0,
        "last_dev_activity": None,
    }


def _accumulate_dev_event(by_project: dict, log: dict) -> None:
    pid = log.get("projectId")
    if not pid:
        return
    event = (log.get("event") or "").lower()
    bucket = by_project.setdefault(pid, _empty_dev_bucket())
    bucket["dev_actions"] += 1
    if event in _BUILD_EVENTS:
        bucket["builds"] += 1
    elif event in _TABLE_OP_EVENTS:
        bucket["table_ops"] += 1
    elif event in _FILE_OP_EVENTS:
        bucket["file_ops"] += 1
    elif event in _ACCESS_CHANGE_EVENTS:
        bucket["access_changes"] += 1
    created_at = log.get("createdAt")
    if created_at and (
        not bucket["last_dev_activity"] or created_at > bucket["last_dev_activity"]
    ):
        bucket["last_dev_activity"] = created_at


@register_function
def get_project_dev_activity(from_iso: str = "", to_iso: str = ""):
    """Per-project developer-activity rollup from the project-scoped audit log
    (get_project_action_logs).

    For each project in the organization, aggregates events within the window:
      dev_actions:       total project-scoped audit events
      builds:            count of createBuild
      table_ops:         table mutations (create/update/delete table or row, executeQuery)
      file_ops:          moveFile / uploadFile / createFolder
      access_changes:    addUser / removeUser / addRole / removeRole / updateRole
      last_dev_activity: latest createdAt seen for the project

    Project ids are chunked into batches of _PROJECT_ACTIONS_BATCH_SIZE (the
    connector hard cap) and paginated sequentially. A global wall-time budget
    of PAGINATION_TIMEOUT_SECONDS applies across all batches — when exceeded,
    later batches are skipped and `partial=true` is returned with whatever
    completed batches accumulated. No caching for now (cache key would need a
    projectIds dimension); fresh paginate on every call.

    Returns the same envelope as get_project_connector_activity so the
    frontend can merge results by project_id.
    """
    if not from_iso or not to_iso:
        from_iso, to_iso = _default_window(days=30)

    try:
        _validate_window(from_iso, to_iso)
    except ValueError as e:
        raise Exception(f"INVALID_WINDOW: {e}")

    print(f"=== Project dev activity for {from_iso} -> {to_iso} ===")

    try:
        projects_meta = _resolve_project_metadata(force_refresh=False)
        project_ids = list(projects_meta.keys())
        print(f"  Projects: {len(project_ids)}")

        by_project: dict = {}
        total = 0
        partial = False
        start_time = time.time()

        for i in range(0, len(project_ids), _PROJECT_ACTIONS_BATCH_SIZE):
            elapsed = time.time() - start_time
            if elapsed > PAGINATION_TIMEOUT_SECONDS:
                print(
                    f"  GLOBAL TIMEOUT at batch {i // _PROJECT_ACTIONS_BATCH_SIZE} "
                    f"after {elapsed:.1f}s — remaining batches skipped"
                )
                partial = True
                break

            batch = project_ids[i : i + _PROJECT_ACTIONS_BATCH_SIZE]
            try:
                logs = _paginate(
                    "get_project_action_logs",
                    from_iso,
                    to_iso,
                    {"projectIds": batch},
                )
            except PaginationTimeoutError as e:
                logs = e.partial_logs
                partial = True

            for log in logs:
                _accumulate_dev_event(by_project, log)
                total += 1

            if partial:
                # A batch timeout means later batches would also likely be slow
                # and we're already over budget for one of them — stop here.
                break

        print(
            f"  {total} dev actions across {len(by_project)} projects "
            f"(partial={partial})"
        )
        return {
            "by_project": by_project,
            "total": total,
            "partial": partial,
            "window": {"from": from_iso, "to": to_iso},
        }

    except Exception as e:
        if str(e).startswith("INVALID_WINDOW:"):
            raise
        import traceback

        traceback.print_exc()
        raise Exception(f"Erro na consulta: {e}")


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
            "get_organization_action_logs",
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
