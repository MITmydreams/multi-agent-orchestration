"""Standalone dashboard HTTP server.

Independent process that reads stats directly from PostgreSQL and exposes
JSON endpoints. Zero coupling to src.brain / src.agents.

Run:
    PYTHONPATH=. .venv/bin/python -m dashboard.server
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from sqlalchemy import text

from src.models.base import async_session_factory

HOST = "127.0.0.1"
PORT = 8765


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _scalar(session, sql: str, params: dict | None = None) -> int:
    result = await session.execute(text(sql), params or {})
    value = result.scalar()
    return int(value) if value is not None else 0


async def _group_by(session, sql: str) -> dict[str, int]:
    result = await session.execute(text(sql))
    out: dict[str, int] = {}
    for key, count in result.all():
        if key is None:
            continue
        out[str(key)] = int(count)
    return out


def _mask_phone(phone: str) -> str:
    """Mask phone number: show first 3 and last 4, mask the rest."""
    if len(phone) <= 7:
        return phone
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


def _row_to_dict(row, columns: list[str]) -> dict[str, Any]:
    """Convert a SQLAlchemy Row to a dict given column names."""
    return {col: _serialize(getattr(row, col, None)) for col in columns}


def _serialize(value: Any) -> Any:
    """Make a value JSON-serializable."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ---------------------------------------------------------------------------
# Static file handler
# ---------------------------------------------------------------------------

async def index_handler(request: web.Request) -> web.Response:
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(index_path)


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    db_status = "connected"
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_status = f"error: {exc.__class__.__name__}"
    return web.json_response(
        {"status": "ok", "ts": _utc_now_iso(), "db": db_status}
    )


async def handle_stats(request: web.Request) -> web.Response:
    try:
        async with async_session_factory() as session:
            # Groups
            groups_total = await _scalar(session, "SELECT COUNT(*) FROM groups")
            groups_by_status = await _group_by(
                session, "SELECT status, COUNT(*) FROM groups GROUP BY status"
            )
            groups_by_grade = await _group_by(
                session, "SELECT grade, COUNT(*) FROM groups GROUP BY grade"
            )
            groups_discovered_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM groups "
                "WHERE created_at > NOW() - INTERVAL '24 hours'",
            )

            # Group accounts
            ga_total = await _scalar(session, "SELECT COUNT(*) FROM group_accounts")
            ga_joined_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM group_accounts "
                "WHERE joined_at > NOW() - INTERVAL '24 hours'",
            )
            ga_by_phase = await _group_by(
                session,
                "SELECT phase, COUNT(*) FROM group_accounts GROUP BY phase",
            )

            # Accounts
            acc_total = await _scalar(session, "SELECT COUNT(*) FROM accounts")
            acc_active = await _scalar(
                session, "SELECT COUNT(*) FROM accounts WHERE status='active'"
            )
            acc_veteran = await _scalar(
                session,
                "SELECT COUNT(*) FROM accounts WHERE account_age_days >= 365",
            )

            # Agent tasks
            tasks_completed_total = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks WHERE status='completed'",
            )
            tasks_completed_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks WHERE status='completed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )
            tasks_by_type = await _group_by(
                session,
                "SELECT task_type, COUNT(*) FROM agent_tasks GROUP BY task_type",
            )

            # Scout
            scout_eval_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type='evaluate_group' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )

            # Errors
            join_failed_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type='join_failed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )
            join_flood_wait_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type='join_flood_wait' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": str(exc), "ts": _utc_now_iso()}, status=500
        )

    payload: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "groups": {
            "total": groups_total,
            "by_status": groups_by_status,
            "by_grade": groups_by_grade,
            "discovered_24h": groups_discovered_24h,
        },
        "group_accounts": {
            "total": ga_total,
            "joined_24h": ga_joined_24h,
            "by_phase": ga_by_phase,
        },
        "accounts": {
            "total": acc_total,
            "active": acc_active,
            "veteran": acc_veteran,
        },
        "tasks": {
            "completed_total": tasks_completed_total,
            "completed_24h": tasks_completed_24h,
            "by_type": tasks_by_type,
        },
        "scout": {
            "evaluate_group_24h": scout_eval_24h,
        },
        "errors_24h": {
            "join_failed": join_failed_24h,
            "join_flood_wait": join_flood_wait_24h,
        },
    }
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# New API endpoints
# ---------------------------------------------------------------------------

async def handle_groups(request: web.Request) -> web.Response:
    """GET /api/groups -- paginated group list with account counts."""
    try:
        page = max(1, int(request.query.get("page", "1")))
        per_page = min(100, max(1, int(request.query.get("per_page", "50"))))
    except ValueError:
        page, per_page = 1, 50

    grade_filter = request.query.get("grade")
    status_filter = request.query.get("status")

    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if grade_filter:
        where_clauses.append("g.grade = :grade")
        params["grade"] = grade_filter
    if status_filter:
        if status_filter == "speakable":
            # "可发言" = evaluated or active (anything except readonly)
            where_clauses.append("g.status != 'readonly'")
        else:
            where_clauses.append("g.status = :status")
            params["status"] = status_filter

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    count_sql = f"SELECT COUNT(*) FROM groups g {where_sql}"
    sort = request.query.get("sort", "joined")  # joined | score | members
    order_clause = {
        "joined": "COALESCE(ga_info.first_joined, '1970-01-01') DESC",
        "score": "g.score DESC",
        "members": "g.member_count DESC",
    }.get(sort, "COALESCE(ga_info.first_joined, '1970-01-01') DESC")

    data_sql = (
        f"SELECT g.id, g.tg_group_id, g.title, g.member_count, g.grade, "
        f"g.score, g.status, g.notes, "
        f"COALESCE(ga_info.cnt, 0) AS our_accounts, "
        f"ga_info.first_joined AS first_joined_at, "
        f"g.created_at "
        f"FROM groups g "
        f"LEFT JOIN ("
        f"  SELECT group_id, COUNT(*) AS cnt, MIN(joined_at) AS first_joined "
        f"  FROM group_accounts GROUP BY group_id"
        f") ga_info ON ga_info.group_id = g.id "
        f"{where_sql} "
        f"ORDER BY {order_clause} "
        f"LIMIT :limit OFFSET :offset"
    )
    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page

    try:
        async with async_session_factory() as session:
            total = await _scalar(session, count_sql, params)
            result = await session.execute(text(data_sql), params)
            rows = result.all()
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    columns = [
        "id", "tg_group_id", "title", "member_count", "grade",
        "score", "status", "notes", "our_accounts", "first_joined_at", "created_at",
    ]
    groups = [_row_to_dict(row, columns) for row in rows]

    return web.json_response({
        "total": total,
        "page": page,
        "per_page": per_page,
        "groups": groups,
    })


async def handle_accounts(request: web.Request) -> web.Response:
    """GET /api/accounts -- account list with joined-group counts."""
    sql = (
        "SELECT a.id, a.phone, a.status, a.account_age_days, "
        "COALESCE(ga_cnt.cnt, 0) AS groups_joined, "
        "a.messages_sent_today, a.outreach_messages_today, "
        "a.risk_score, a.proxy_id "
        "FROM accounts a "
        "LEFT JOIN ("
        "  SELECT account_id, COUNT(*) AS cnt FROM group_accounts GROUP BY account_id"
        ") ga_cnt ON ga_cnt.account_id = a.id "
        "ORDER BY a.id"
    )

    try:
        async with async_session_factory() as session:
            result = await session.execute(text(sql))
            rows = result.all()
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    columns = [
        "id", "phone", "status", "account_age_days", "groups_joined",
        "messages_sent_today", "outreach_messages_today", "risk_score", "proxy_id",
    ]
    accounts = []
    for row in rows:
        d = _row_to_dict(row, columns)
        d["phone"] = _mask_phone(str(d["phone"])) if d["phone"] else ""
        accounts.append(d)

    # Fetch per-account group details
    groups_sql = (
        "SELECT ga.account_id, g.tg_group_id, g.title, g.member_count, "
        "g.grade, g.status, ga.joined_at, "
        "CASE "
        "  WHEN (NOW() - ga.joined_at) < INTERVAL '1 day' THEN '潜水' "
        "  WHEN (NOW() - ga.joined_at) < INTERVAL '3 days' THEN '闲聊' "
        "  ELSE '触达' "
        "END AS phase "
        "FROM group_accounts ga "
        "JOIN groups g ON ga.group_id = g.id "
        "ORDER BY ga.account_id, g.member_count DESC"
    )
    try:
        async with async_session_factory() as session:
            gresult = await session.execute(text(groups_sql))
            grows = gresult.all()
    except Exception:
        grows = []

    gcols = ["account_id", "tg_group_id", "title", "member_count", "grade", "status", "joined_at", "phase"]
    account_groups: dict[int, list] = {}
    for grow in grows:
        gd = _row_to_dict(grow, gcols)
        aid = gd.pop("account_id")
        if gd.get("joined_at"):
            gd["joined_at"] = gd["joined_at"].isoformat() + "Z" if hasattr(gd["joined_at"], "isoformat") else str(gd["joined_at"])
        account_groups.setdefault(aid, []).append(gd)

    for a in accounts:
        a["groups"] = account_groups.get(a["id"], [])

    return web.json_response({"accounts": accounts})


async def handle_activity(request: web.Request) -> web.Response:
    """GET /api/activity -- recent completed tasks (last 100)."""
    type_filter = request.query.get("type")

    where_clause = "WHERE t.status = 'completed'"
    params: dict[str, Any] = {}
    if type_filter:
        where_clause += " AND t.task_type = :task_type"
        params["task_type"] = type_filter

    sql = (
        f"SELECT t.id, t.agent_type, t.task_type, t.account_id, "
        f"t.group_id, t.payload, t.completed_at "
        f"FROM agent_tasks t "
        f"{where_clause} "
        f"ORDER BY t.completed_at DESC "
        f"LIMIT 100"
    )

    try:
        async with async_session_factory() as session:
            result = await session.execute(text(sql), params)
            rows = result.all()
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    columns = [
        "id", "agent_type", "task_type", "account_id",
        "group_id", "payload", "completed_at",
    ]
    activities = [_row_to_dict(row, columns) for row in rows]

    return web.json_response({"activities": activities})


async def handle_promo_stats(request: web.Request) -> web.Response:
    """GET /api/promo_stats -- outreach effectiveness statistics."""
    try:
        async with async_session_factory() as session:
            total_sent = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs WHERE is_outreach = true",
            )
            total_link_replies = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type = 'link_reply' AND status = 'completed'",
            )
            total_send_errors = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type = 'send_message' AND status = 'failed'",
            )
            success_rate = round(
                total_sent / (total_sent + total_send_errors) if (total_sent + total_send_errors) > 0 else 0,
                2,
            )

            # Errors by type (extract from error field)
            err_rows = await session.execute(text(
                "SELECT error, COUNT(*) "
                "FROM agent_tasks "
                "WHERE status = 'failed' AND error IS NOT NULL "
                "GROUP BY error ORDER BY COUNT(*) DESC"
            ))
            by_error_type: dict[str, int] = {}
            for err_text, cnt in err_rows.all():
                # Extract the exception class name from the error text
                label = str(err_text).split(":")[0].split(".")[-1].strip() if err_text else "Unknown"
                by_error_type[label] = by_error_type.get(label, 0) + int(cnt)

            # Messages by group (top 20)
            grp_rows = await session.execute(text(
                "SELECT g.tg_group_id, COUNT(*) AS cnt "
                "FROM message_logs m "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE m.is_outreach = true "
                "GROUP BY g.tg_group_id ORDER BY cnt DESC LIMIT 20"
            ))
            messages_by_group = [
                {"group_id": str(gid), "count": int(c)} for gid, c in grp_rows.all()
            ]

            # Messages by account
            acct_rows = await session.execute(text(
                "SELECT account_id, COUNT(*) AS cnt "
                "FROM message_logs WHERE is_outreach = true "
                "GROUP BY account_id ORDER BY cnt DESC"
            ))
            messages_by_account = [
                {"account_id": int(aid), "count": int(c)} for aid, c in acct_rows.all()
            ]

    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    return web.json_response({
        "total_messages_sent": total_sent,
        "total_link_replies": total_link_replies,
        "total_send_errors": total_send_errors,
        "success_rate": success_rate,
        "by_error_type": by_error_type,
        "messages_by_group": messages_by_group,
        "messages_by_account": messages_by_account,
    })


async def handle_phase_distribution(request: web.Request) -> web.Response:
    """GET /api/phase_distribution -- phase and status distribution."""
    try:
        async with async_session_factory() as session:
            phase_rows = await session.execute(text(
                "SELECT phase, COUNT(*) FROM group_accounts GROUP BY phase ORDER BY COUNT(*) DESC"
            ))
            distribution = [
                {"phase": str(p), "count": int(c)} for p, c in phase_rows.all()
            ]

            groups_by_status = await _group_by(
                session, "SELECT status, COUNT(*) FROM groups GROUP BY status"
            )
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    return web.json_response({
        "distribution": distribution,
        "groups_by_status": groups_by_status,
    })


async def handle_logs(request: web.Request) -> web.Response:
    """GET /api/logs?lines=100 -- tail of ops-orchestrator.jsonl log file."""
    try:
        lines_requested = min(500, max(1, int(request.query.get("lines", "100"))))
    except ValueError:
        lines_requested = 100

    log_path = Path(__file__).resolve().parent.parent / "data" / "logs" / "ops-orchestrator.jsonl"
    if not log_path.exists():
        return web.json_response({"lines": [], "error": "Log file not found"})

    try:
        with open(log_path, "rb") as f:
            # Read from end efficiently
            f.seek(0, 2)
            file_size = f.tell()
            # Read last 512KB at most (should be plenty for 500 lines)
            read_size = min(file_size, 512 * 1024)
            f.seek(file_size - read_size)
            raw = f.read().decode("utf-8", errors="replace")

        raw_lines = raw.strip().split("\n")
        # Take last N lines
        tail = raw_lines[-lines_requested:]
        parsed = []
        for line in reversed(tail):  # newest first
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                parsed.append({"raw": line})
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"lines": [], "error": str(exc)}, status=500)

    return web.json_response({"lines": parsed})


async def handle_kpi(request: web.Request) -> web.Response:
    """GET /api/kpi -- system effectiveness report (战力报告)."""
    try:
        async with async_session_factory() as session:
            # ---- funnel --------------------------------------------------
            groups_discovered = await _scalar(
                session, "SELECT COUNT(*) FROM groups"
            )
            groups_writable = await _scalar(
                session,
                "SELECT COUNT(*) FROM groups "
                "WHERE status != 'readonly'",
            )
            groups_joined = await _scalar(
                session, "SELECT COUNT(DISTINCT group_id) FROM group_accounts"
            )

            # Phase distribution based on joined_at elapsed time
            phase_rows = await session.execute(text(
                "SELECT "
                "  CASE "
                "    WHEN (NOW() - ga.joined_at) < INTERVAL '1 day' THEN 'lurking' "
                "    WHEN (NOW() - ga.joined_at) < INTERVAL '3 days' THEN 'trust_building' "
                "    ELSE 'soft_outreach' "
                "  END AS phase, "
                "  COUNT(*) "
                "FROM group_accounts ga "
                "JOIN groups g ON ga.group_id = g.id "
                "WHERE g.status != 'readonly' "
                "GROUP BY 1"
            ))
            phase_map: dict[str, int] = {}
            for phase_name, cnt in phase_rows.all():
                phase_map[str(phase_name)] = int(cnt)

            accounts_in_trust = phase_map.get("trust_building", 0)
            accounts_in_promo = phase_map.get("soft_outreach", 0)

            messages_today = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE sent_at > NOW() - INTERVAL '24 hours'",
            )
            outreach_messages_today = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE is_outreach = true AND sent_at > NOW() - INTERVAL '24 hours'",
            )
            links_shared_today = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE sent_at > NOW() - INTERVAL '24 hours' "
                "AND (content LIKE '%http://%' OR content LIKE '%https://%' OR content LIKE '%t.me/%')",
            )

            # ---- message funnel ------------------------------------------
            # Total infiltrate tasks that entered trust/promote phase (24h)
            phase_attempts = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type = 'infiltrate_task' "
                "AND completed_at > NOW() - INTERVAL '24 hours' "
                "AND (payload::text LIKE '%trust_building%' "
                "     OR payload::text LIKE '%soft_outreach%')",
            )

            # Read-message failures: tasks where recent_messages fetch failed
            # Approximate via infiltrate tasks that completed but no message_log
            # We count tasks with send_failed logged
            send_failed_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type = 'send_failed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )

            # Content generation failures
            content_gen_failed_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type IN ('trust_message', 'promo_message') "
                "AND status = 'failed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )

            # Telegram rejected (flood_wait, chat_write_forbidden, etc.)
            telegram_rejected_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_type IN ('join_flood_wait', 'send_message') "
                "AND status = 'failed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'",
            )

            # Successfully sent messages (24h)
            messages_sent_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE sent_at > NOW() - INTERVAL '24 hours'",
            )

            # Success rate
            mf_success_rate = (
                f"{messages_sent_24h / phase_attempts * 100:.1f}%"
                if phase_attempts > 0
                else "N/A"
            )

            # ---- trends --------------------------------------------------
            messages_1h = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE sent_at > NOW() - INTERVAL '1 hour'",
            )
            messages_6h = await _scalar(
                session,
                "SELECT COUNT(*) FROM message_logs "
                "WHERE sent_at > NOW() - INTERVAL '6 hours'",
            )
            messages_24h = messages_today  # same window

            joins_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM group_accounts "
                "WHERE joined_at > NOW() - INTERVAL '24 hours'",
            )
            new_groups_24h = await _scalar(
                session,
                "SELECT COUNT(*) FROM groups "
                "WHERE created_at > NOW() - INTERVAL '24 hours'",
            )

            # ---- pulse ---------------------------------------------------
            last_message_result = await session.execute(
                text("SELECT MAX(sent_at) FROM message_logs")
            )
            last_message_at = last_message_result.scalar()

            last_join_result = await session.execute(
                text("SELECT MAX(joined_at) FROM group_accounts")
            )
            last_join_at = last_join_result.scalar()

            last_scout_result = await session.execute(
                text(
                    "SELECT MAX(completed_at) FROM agent_tasks "
                    "WHERE agent_type = 'scout' AND status = 'completed'"
                )
            )
            last_scout_at = last_scout_result.scalar()

    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": str(exc), "ts": _utc_now_iso()}, status=500
        )

    # ---- build dicts -----------------------------------------------------
    funnel = {
        "groups_discovered": groups_discovered,
        "groups_writable": groups_writable,
        "groups_joined": groups_joined,
        "accounts_in_trust": accounts_in_trust,
        "accounts_in_promo": accounts_in_promo,
        "messages_today": messages_today,
        "outreach_messages_today": outreach_messages_today,
        "links_shared_today": links_shared_today,
    }
    trends = {
        "messages_1h": messages_1h,
        "messages_6h": messages_6h,
        "messages_24h": messages_24h,
        "joins_24h": joins_24h,
        "new_groups_24h": new_groups_24h,
    }

    # ---- score (5 dimensions x 20 pts each) -----------------------------
    # Coverage: 500 writable groups = 20 pts
    coverage = min(20, funnel["groups_writable"] / 25)
    # Penetration: 80% of writable groups joined = 20 pts
    pen_ratio = funnel["groups_joined"] / max(funnel["groups_writable"], 1)
    penetration = min(20, (pen_ratio / 0.8) * 20)
    # Output: messages_today >= groups_writable = 20 pts (1 msg/group/day)
    output = min(20, (funnel["messages_today"] / max(funnel["groups_writable"], 1)) * 20)
    # Promo: 20% of messages are promo = 20 pts
    outreach_ratio = funnel["outreach_messages_today"] / max(funnel["messages_today"], 1)
    promo_score = min(20, outreach_ratio * 100)
    # Growth: 100 new (groups + joins) per day = 20 pts
    growth = min(20, (trends["new_groups_24h"] + trends["joins_24h"]) / 5)
    score = round(coverage + penetration + output + promo_score + growth)

    score_dimensions = {
        "coverage": {"score": round(coverage, 1), "max": 20, "rule": "可发言群 500 个满分", "current": funnel["groups_writable"]},
        "penetration": {"score": round(penetration, 1), "max": 20, "rule": "接入率 80% 满分", "current": f"{pen_ratio:.0%}"},
        "output": {"score": round(output, 1), "max": 20, "rule": "每群每天 1 条消息满分", "current": funnel["messages_today"]},
        "promo": {"score": round(promo_score, 1), "max": 20, "rule": "触达占比 20% 满分", "current": f"{outreach_ratio:.0%}"},
        "growth": {"score": round(growth, 1), "max": 20, "rule": "日增 100 (群+加入) 满分", "current": trends["new_groups_24h"] + trends["joins_24h"]},
    }

    if score >= 80:
        score_label = "爆发"
    elif score >= 60:
        score_label = "强劲"
    elif score >= 40:
        score_label = "良好"
    elif score >= 20:
        score_label = "一般"
    else:
        score_label = "差"

    # ---- bottleneck analysis (priority order) ----------------------------
    gw = funnel["groups_writable"]
    gj = funnel["groups_joined"]
    mt = funnel["messages_today"]
    pm = funnel["outreach_messages_today"]
    ls = funnel["links_shared_today"]

    if gw < 10:
        bottleneck = {
            "id": "low_writable_groups",
            "label": "可发言群太少",
            "detail": f"仅{gw}个可发言群（megagroup），需≥10个才能有效运营",
            "suggestion": "加速scout评估，筛选更多megagroup群",
        }
    elif gj / max(gw, 1) < 0.3:
        ratio_pct = round(gj / max(gw, 1) * 100)
        bottleneck = {
            "id": "low_join_rate",
            "label": "加群速度不足",
            "detail": f"{gw}个可发言群但仅加入{gj}个，接入率{ratio_pct}%",
            "suggestion": "检查executor是否正常运行、账号加群是否被限制",
        }
    elif phase_attempts > 0 and messages_sent_24h / phase_attempts < 0.1:
        delivery_pct = f"{messages_sent_24h / phase_attempts:.0%}"
        bottleneck = {
            "id": "low_delivery_rate",
            "label": "消息投递率过低",
            "detail": f"24h内{phase_attempts}次发言尝试仅{messages_sent_24h}条成功，投递率{delivery_pct}",
            "suggestion": "检查 FloodWait 状态、群可达性、entity 解析",
        }
    elif mt / max(gw, 1) < 0.5:
        util_pct = round(mt / max(gw, 1) * 100) if gw else 0
        bottleneck = {
            "id": "low_message_output",
            "label": "消息产出过低",
            "detail": f"{gw}个可发言群但今日仅{mt}条消息，利用率{util_pct}%",
            "suggestion": "检查内容生成是否正常、账号是否在线",
        }
    elif pm / max(mt, 1) < 0.1:
        promo_pct = round(pm / max(mt, 1) * 100)
        bottleneck = {
            "id": "low_outreach_ratio",
            "label": "触达比例过低",
            "detail": f"今日{mt}条消息中仅{pm}条触达，触达比{promo_pct}%",
            "suggestion": "增加触达内容投放频率，确保soft_outreach阶段账号正常工作",
        }
    elif ls == 0:
        bottleneck = {
            "id": "no_links_shared",
            "label": "链接未分享",
            "detail": "今日尚未分享任何链接",
            "suggestion": "检查触达消息是否包含链接、链接生成逻辑是否正常",
        }
    else:
        bottleneck = {
            "id": "ok",
            "label": "运行正常",
            "detail": "各项指标处于健康水平",
            "suggestion": "继续保持，可尝试扩大覆盖范围",
        }

    # ---- pulse -----------------------------------------------------------
    # Determine system_active: last message within 2 hours
    system_active = False
    if last_message_at is not None:
        if isinstance(last_message_at, datetime):
            delta = datetime.now(timezone.utc) - (
                last_message_at.replace(tzinfo=timezone.utc)
                if last_message_at.tzinfo is None
                else last_message_at
            )
            system_active = delta < timedelta(hours=2)

    pulse = {
        "last_message_at": _serialize(last_message_at),
        "last_join_at": _serialize(last_join_at),
        "last_scout_at": _serialize(last_scout_at),
        "system_active": system_active,
    }

    message_funnel = {
        "phase_attempts": phase_attempts,
        "send_failed": send_failed_24h,
        "content_gen_failed": content_gen_failed_24h,
        "telegram_rejected": telegram_rejected_24h,
        "messages_sent": messages_sent_24h,
        "success_rate": mf_success_rate,
    }

    payload = {
        "ts": _utc_now_iso(),
        "score": score,
        "score_label": score_label,
        "score_dimensions": score_dimensions,
        "funnel": funnel,
        "message_funnel": message_funnel,
        "bottleneck": bottleneck,
        "trends": trends,
        "pulse": pulse,
    }
    return web.json_response(payload)


async def handle_account_progress(request: web.Request) -> web.Response:
    """GET /api/account_progress -- per-account daily output vs capacity."""
    TARGET_PER_ACCOUNT = 30

    sql = (
        "SELECT a.id, a.phone, a.status, "
        "  COALESCE(m.cnt, 0) AS sent_today, "
        "  COALESCE(m.promo_cnt, 0) AS promo_today "
        "FROM accounts a "
        "LEFT JOIN ( "
        "  SELECT account_id, COUNT(*) AS cnt, "
        "    COUNT(*) FILTER (WHERE is_outreach) AS promo_cnt "
        "  FROM message_logs "
        "  WHERE sent_at > NOW() - INTERVAL '24 hours' "
        "  GROUP BY account_id "
        ") m ON a.id = m.account_id "
        "ORDER BY a.id"
    )

    try:
        async with async_session_factory() as session:
            result = await session.execute(text(sql))
            rows = result.all()
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    accounts: list[dict[str, Any]] = []
    total_sent = 0

    for row in rows:
        sent_today = int(row.sent_today)
        promo_today = int(row.promo_today)
        capacity = TARGET_PER_ACCOUNT
        utilization = sent_today / capacity if capacity > 0 else 0
        total_sent += sent_today

        status_label = {
            "active": "active",
            "abandoned": "banned",
            "hibernating": "hibernating",
            "nurturing": "nurturing",
        }.get(str(row.status), str(row.status)) if row.status else "unknown"

        accounts.append({
            "id": int(row.id),
            "phone": _mask_phone(str(row.phone)) if row.phone else "",
            "sent_today": sent_today,
            "promo_today": promo_today,
            "capacity": capacity,
            "utilization": f"{utilization:.0%}",
            "status": status_label,
        })

    total_target = len(accounts) * TARGET_PER_ACCOUNT
    total_utilization = total_sent / total_target if total_target > 0 else 0

    return web.json_response({
        "target_per_account": TARGET_PER_ACCOUNT,
        "accounts": accounts,
        "total_sent": total_sent,
        "total_target": total_target,
        "total_utilization": f"{total_utilization:.0%}",
    })


async def handle_messages(request: web.Request) -> web.Response:
    """GET /api/messages -- sent messages, optionally filtered by account_id."""
    account_filter = request.query.get("account_id")
    limit = min(200, max(1, int(request.query.get("limit", "100"))))

    where = ""
    params: dict[str, Any] = {"limit": limit}
    if account_filter:
        where = "WHERE m.account_id = :account_id"
        params["account_id"] = int(account_filter)

    sql = (
        f"SELECT m.id, m.account_id, a.phone, m.group_id, "
        f"g.tg_group_id, g.title AS group_title, g.member_count, "
        f"m.content, m.is_outreach, m.message_type, m.sent_at "
        f"FROM message_logs m "
        f"LEFT JOIN groups g ON m.group_id = g.id "
        f"LEFT JOIN accounts a ON m.account_id = a.id "
        f"{where} "
        f"ORDER BY m.sent_at DESC "
        f"LIMIT :limit"
    )

    try:
        async with async_session_factory() as session:
            result = await session.execute(text(sql), params)
            rows = result.all()
    except Exception as exc:
        return web.json_response({"error": str(exc), "ts": _utc_now_iso()}, status=500)

    columns = [
        "id", "account_id", "phone", "group_id",
        "tg_group_id", "group_title", "member_count",
        "content", "is_outreach", "message_type", "sent_at",
    ]
    messages = []
    for row in rows:
        d = _row_to_dict(row, columns)
        d["phone"] = _mask_phone(str(d["phone"])) if d["phone"] else ""
        messages.append(d)

    return web.json_response({"messages": messages, "total": len(messages)})


async def handle_join_trend(request: web.Request) -> web.Response:
    """GET /api/join_trend -- hourly join counts for the last 48 hours."""
    sql = (
        "SELECT date_trunc('hour', joined_at) AS hour, COUNT(*) AS cnt "
        "FROM group_accounts "
        "WHERE joined_at > NOW() - INTERVAL '48 hours' "
        "GROUP BY 1 ORDER BY 1"
    )
    try:
        async with async_session_factory() as session:
            result = await session.execute(text(sql))
            rows = result.all()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    points = []
    for row in rows:
        hour_str = row[0].isoformat() + "Z" if hasattr(row[0], "isoformat") else str(row[0])
        points.append({"hour": hour_str, "joins": int(row[1])})

    # Also compute summary rates
    total_48h = sum(p["joins"] for p in points)
    total_24h = sum(p["joins"] for p in points[-24:]) if len(points) >= 24 else total_48h
    total_6h = sum(p["joins"] for p in points[-6:]) if len(points) >= 6 else total_48h

    return web.json_response({
        "points": points,
        "summary": {
            "last_6h": total_6h,
            "last_24h": total_24h,
            "last_48h": total_48h,
            "avg_per_hour_24h": round(total_24h / 24, 1) if total_24h else 0,
        },
    })


async def handle_join_failures(request: web.Request) -> web.Response:
    """GET /api/join_failures -- join failure stats."""
    try:
        async with async_session_factory() as session:
            # Total join failures
            total = await _scalar(session,
                "SELECT COUNT(*) FROM agent_tasks WHERE task_type = 'join_failed'")

            # 24h failures
            total_24h = await _scalar(session,
                "SELECT COUNT(*) FROM agent_tasks WHERE task_type = 'join_failed' "
                "AND completed_at > NOW() - INTERVAL '24 hours'")

            # Successful joins for comparison
            total_success = await _scalar(session,
                "SELECT COUNT(*) FROM agent_tasks WHERE task_type = 'joined_group'")

            # Groups stuck in evaluated (never joined)
            never_joined = await _scalar(session,
                "SELECT COUNT(*) FROM groups g WHERE g.status = 'evaluated' "
                "AND NOT EXISTS (SELECT 1 FROM group_accounts ga WHERE ga.group_id = g.id)")

            # Groups marked readonly due to join failures
            join_failed_readonly = await _scalar(session,
                "SELECT COUNT(*) FROM groups WHERE notes LIKE '%join-failed%'")

            # Top failing groups
            result = await session.execute(text(
                "SELECT g.tg_group_id, g.title, COUNT(*) AS fails "
                "FROM agent_tasks t JOIN groups g ON t.group_id = g.id "
                "WHERE t.task_type = 'join_failed' "
                "GROUP BY g.tg_group_id, g.title "
                "ORDER BY fails DESC LIMIT 10"
            ))
            top_fails = [{"group": r[0], "title": r[1] or r[0], "fails": int(r[2])} for r in result]

    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    success_rate = total_success / max(total_success + total, 1) * 100

    return web.json_response({
        "total_failures": total,
        "failures_24h": total_24h,
        "total_success": total_success,
        "success_rate": f"{success_rate:.0f}%",
        "never_joined_groups": never_joined,
        "auto_blocked_groups": join_failed_readonly,
        "top_failing_groups": top_fails,
    })


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    # Static / index
    app.router.add_get("/", index_handler)

    # Legacy endpoints (keep backward compatibility)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats", handle_stats)

    # /api/ prefixed aliases for existing endpoints
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/stats", handle_stats)

    # Join trend API
    app.router.add_get("/api/join_trend", handle_join_trend)
    app.router.add_get("/api/join_failures", handle_join_failures)

    # New API endpoints
    app.router.add_get("/api/kpi", handle_kpi)
    app.router.add_get("/api/groups", handle_groups)
    app.router.add_get("/api/accounts", handle_accounts)
    app.router.add_get("/api/activity", handle_activity)
    app.router.add_get("/api/messages", handle_messages)
    app.router.add_get("/api/account_progress", handle_account_progress)
    app.router.add_get("/api/promo_stats", handle_promo_stats)
    app.router.add_get("/api/phase_distribution", handle_phase_distribution)
    app.router.add_get("/api/logs", handle_logs)

    return app


def main() -> None:
    print(f"[dashboard] starting on http://{HOST}:{PORT}")
    print("[dashboard] endpoints:")
    print("  GET /              (dashboard UI)")
    print("  GET /health        (legacy)")
    print("  GET /stats         (legacy)")
    print("  GET /api/health")
    print("  GET /api/stats")
    print("  GET /api/kpi")
    print("  GET /api/groups    (?page=&grade=&status=)")
    print("  GET /api/accounts")
    print("  GET /api/activity  (?type=)")
    print("  GET /api/messages")
    print("  GET /api/account_progress")
    print("  GET /api/promo_stats")
    print("  GET /api/phase_distribution")
    print("  GET /api/logs      (?lines=100)")
    web.run_app(build_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
