"""FastAPI backend for the promo-bot operations dashboard.

Single-file FastAPI app exposing REST + WebSocket endpoints for monitoring
the 15-account Telegram AI agent system.

Run with:
    .venv/bin/python -m uvicorn dashboard.backend:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import ORM models from the existing project
from src.config.settings import settings
from src.models.account import Account
from src.models.group import Group, GroupAccount
from src.models.message import ContentPiece, MessageLog
from src.models.metrics import DailyMetrics
from src.models.task import AgentTask

# ---------------------------------------------------------------------------
# DB engine (independent pool for the dashboard process)
# ---------------------------------------------------------------------------
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

START_TIME = time.time()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def get_session() -> AsyncSession:
    return SessionLocal()


def derive_health(active: int, hibernating: int, abandoned: int, avg_risk: float) -> str:
    if abandoned > 2 or avg_risk > 0.65:
        return "red"
    if hibernating > 2 or avg_risk > 0.4:
        return "yellow"
    if active == 0:
        return "yellow"
    return "green"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 60)
    print("  Promo-Bot Operations Dashboard")
    print("  Listening on: http://0.0.0.0:8765")
    print("  Frontend:     http://localhost:8765/")
    print("  API root:     http://localhost:8765/api/overview")
    print("  WebSocket:    ws://localhost:8765/ws/live")
    print("=" * 60 + "\n")
    yield
    await engine.dispose()


app = FastAPI(title="Promo-Bot Dashboard API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST: /api/overview
# ---------------------------------------------------------------------------
@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    async with SessionLocal() as s:
        # Count accounts by status in one query
        rows = (
            await s.execute(select(Account.status, func.count(Account.id)).group_by(Account.status))
        ).all()
        status_counts = {r[0]: r[1] for r in rows}
        total_accounts = sum(status_counts.values())
        active = status_counts.get("active", 0)
        nurturing = status_counts.get("nurturing", 0)
        hibernating = status_counts.get("hibernating", 0)
        abandoned = status_counts.get("abandoned", 0)

        groups_count = (await s.execute(select(func.count(Group.id)))).scalar_one()
        content_count = (await s.execute(select(func.count(ContentPiece.id)))).scalar_one()

        today_start = datetime.combine(date.today(), datetime.min.time())
        msgs_today = (
            await s.execute(
                select(func.count(MessageLog.id)).where(MessageLog.sent_at >= today_start)
            )
        ).scalar_one()

        avg_risk = (await s.execute(select(func.avg(Account.risk_score)))).scalar() or 0.0

    return {
        "system_health": derive_health(active, hibernating, abandoned, float(avg_risk)),
        "ai_mode": "api" if settings.anthropic_api_key else "template",
        "ai_model": settings.ai_model,
        "scheduler_running": True,
        "current_cycle": int((time.time() - START_TIME) // settings.scheduler_interval_seconds),
        "uptime_seconds": int(time.time() - START_TIME),
        "totals": {
            "accounts": total_accounts,
            "active": active,
            "nurturing": nurturing,
            "hibernating": hibernating,
            "abandoned": abandoned,
            "groups": groups_count,
            "content_pieces": content_count,
            "messages_today": msgs_today,
        },
    }


# ---------------------------------------------------------------------------
# REST: /api/accounts
# ---------------------------------------------------------------------------
@app.get("/api/accounts")
async def accounts() -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(Account).order_by(Account.id))).scalars().all()

        # Get the most recent agent task per account in one query
        recent_tasks_q = text(
            """
            SELECT DISTINCT ON (account_id) account_id, task_type, status
            FROM agent_tasks
            WHERE account_id IS NOT NULL
            ORDER BY account_id, COALESCE(started_at, created_at) DESC
            """
        )
        recent_map: dict[int, str] = {}
        for r in (await s.execute(recent_tasks_q)).all():
            recent_map[r[0]] = f"{r[1]}:{r[2]}"

    out = []
    for a in rows:
        current = recent_map.get(a.id, "lurking")
        out.append({
            "id": a.id,
            "display_name": a.display_name,
            "username": a.username,
            "role": a.role,
            "status": a.status,
            "proxy_id": a.proxy_id,
            "risk_score": round(a.risk_score, 3),
            "trust_score": round(a.trust_score, 3),
            "messages_today": a.messages_sent_today,
            "promo_today": a.promo_messages_today,
            "groups_today": a.groups_active_today,
            "total_messages": a.total_messages,
            "kicked_count": a.kicked_count,
            "last_active": iso(a.last_active),
            "age_days": a.account_age_days,
            "current_action": current,
        })
    return out


# ---------------------------------------------------------------------------
# REST: /api/groups
# ---------------------------------------------------------------------------
@app.get("/api/groups")
async def groups_endpoint() -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        groups = (await s.execute(select(Group).order_by(Group.id))).scalars().all()

        # Single join query for assignments
        assign_q = (
            select(GroupAccount.group_id, Account.id, Account.display_name, GroupAccount.phase)
            .join(Account, Account.id == GroupAccount.account_id)
        )
        assign_rows = (await s.execute(assign_q)).all()

    by_group: dict[int, list[dict[str, Any]]] = {}
    for gid, aid, name, phase in assign_rows:
        by_group.setdefault(gid, []).append({"id": aid, "name": name, "phase": phase})

    out = []
    for g in groups:
        out.append({
            "id": g.id,
            "title": g.title,
            "username": g.username,
            "grade": g.grade,
            "member_count": g.member_count,
            "status": g.status,
            "language": g.language,
            "agents": by_group.get(g.id, []),
        })
    return out


# ---------------------------------------------------------------------------
# REST: /api/activity
# ---------------------------------------------------------------------------
@app.get("/api/activity")
async def activity(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        # Messages
        msg_q = (
            select(
                MessageLog.id,
                MessageLog.account_id,
                Account.display_name,
                MessageLog.group_id,
                Group.username,
                Group.title,
                MessageLog.content,
                MessageLog.sent_at,
                MessageLog.is_promo,
            )
            .join(Account, Account.id == MessageLog.account_id)
            .outerjoin(Group, Group.id == MessageLog.group_id)
            .order_by(desc(MessageLog.sent_at))
            .limit(limit)
        )
        msgs = (await s.execute(msg_q)).all()

        task_q = (
            select(
                AgentTask.id,
                AgentTask.account_id,
                Account.display_name,
                AgentTask.task_type,
                AgentTask.status,
                AgentTask.started_at,
                AgentTask.created_at,
            )
            .outerjoin(Account, Account.id == AgentTask.account_id)
            .order_by(desc(func.coalesce(AgentTask.started_at, AgentTask.created_at)))
            .limit(limit)
        )
        tasks = (await s.execute(task_q)).all()

        content_q = (
            select(
                ContentPiece.id,
                ContentPiece.content_type,
                ContentPiece.language,
                ContentPiece.content,
                ContentPiece.created_at,
            )
            .order_by(desc(ContentPiece.created_at))
            .limit(limit)
        )
        contents = (await s.execute(content_q)).all()

    feed: list[dict[str, Any]] = []

    for r in msgs:
        group_label = f"@{r.username}" if r.username else (r.title or f"group#{r.group_id}") if r.group_id else "DM"
        feed.append({
            "id": f"m{r.id}",
            "type": "message",
            "agent_id": r.account_id,
            "agent_name": r.display_name,
            "action": f"{'promoted' if r.is_promo else 'sent message'} in {group_label}",
            "details": (r.content or "")[:200],
            "timestamp": iso(r.sent_at),
        })

    for r in tasks:
        ts = r.started_at or r.created_at
        feed.append({
            "id": f"t{r.id}",
            "type": "task",
            "agent_id": r.account_id,
            "agent_name": r.display_name or "system",
            "action": f"task {r.task_type} ({r.status})",
            "details": "",
            "timestamp": iso(ts),
        })

    for r in contents:
        feed.append({
            "id": f"c{r.id}",
            "type": "content",
            "agent_id": None,
            "agent_name": "content-agent",
            "action": f"generated {r.content_type} ({r.language})",
            "details": (r.content or "")[:200],
            "timestamp": iso(r.created_at),
        })

    feed.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    return feed[:limit]


# ---------------------------------------------------------------------------
# REST: /api/content
# ---------------------------------------------------------------------------
@app.get("/api/content")
async def content_endpoint(limit: int = Query(20, ge=1, le=500)) -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(ContentPiece).order_by(desc(ContentPiece.created_at)).limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": c.id,
            "type": c.content_type,
            "language": c.language,
            "content": c.content,
            "spam_score": round(c.spam_score, 3),
            "created_at": iso(c.created_at),
        }
        for c in rows
    ]


# ---------------------------------------------------------------------------
# REST: /api/metrics
# ---------------------------------------------------------------------------
@app.get("/api/metrics")
async def metrics_endpoint() -> dict[str, Any]:
    seven_days_ago = date.today() - timedelta(days=7)
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(DailyMetrics)
                .where(DailyMetrics.date >= seven_days_ago)
                .order_by(DailyMetrics.date)
            )
        ).scalars().all()

        accounts_rows = (await s.execute(select(Account.role, Account.risk_score))).all()

    daily = [
        {
            "date": m.date.isoformat(),
            "messages_sent": m.messages_sent,
            "promo_messages": m.promo_messages,
            "content_generated": m.content_generated,
            "risk_score": round(m.avg_risk_score, 3),
        }
        for m in rows
    ]

    risk_dist = {"normal": 0, "slow_down": 0, "strict": 0, "hibernate": 0, "abandon": 0}
    role_dist: dict[str, int] = {}
    for role, risk in accounts_rows:
        role_dist[role] = role_dist.get(role, 0) + 1
        if risk >= settings.risk_threshold_abandon:
            risk_dist["abandon"] += 1
        elif risk >= settings.risk_threshold_hibernate:
            risk_dist["hibernate"] += 1
        elif risk >= settings.risk_threshold_strict:
            risk_dist["strict"] += 1
        elif risk >= settings.risk_threshold_slow:
            risk_dist["slow_down"] += 1
        else:
            risk_dist["normal"] += 1

    return {"daily": daily, "risk_distribution": risk_dist, "role_distribution": role_dist}


# ---------------------------------------------------------------------------
# REST: /api/agents
# ---------------------------------------------------------------------------
@app.get("/api/agents")
async def agents_endpoint() -> dict[str, Any]:
    today_start = datetime.combine(date.today(), datetime.min.time())
    async with SessionLocal() as s:
        role_rows = (
            await s.execute(select(Account.role, func.count(Account.id)).group_by(Account.role))
        ).all()
        role_counts = {r[0]: r[1] for r in role_rows}

        groups_evaluated = (
            await s.execute(select(func.count(Group.id)).where(Group.status != "discovered"))
        ).scalar_one()
        groups_discovered_today = (
            await s.execute(
                select(func.count(Group.id)).where(Group.created_at >= today_start)
            )
        ).scalar_one()
        groups_active = (
            await s.execute(select(func.count(Group.id)).where(Group.status == "active"))
        ).scalar_one()

        msgs_today = (
            await s.execute(
                select(func.count(MessageLog.id)).where(MessageLog.sent_at >= today_start)
            )
        ).scalar_one()

        content_today = (
            await s.execute(
                select(func.count(ContentPiece.id)).where(ContentPiece.created_at >= today_start)
            )
        ).scalar_one()

        last_content = (
            await s.execute(select(func.max(ContentPiece.created_at)))
        ).scalar()

        last_scout_run = (
            await s.execute(
                select(func.max(AgentTask.started_at)).where(AgentTask.agent_type == "scout")
            )
        ).scalar()

        last_viral = (
            await s.execute(
                select(func.max(AgentTask.started_at)).where(AgentTask.agent_type == "viral")
            )
        ).scalar()
        viral_today = (
            await s.execute(
                select(func.count(AgentTask.id)).where(
                    AgentTask.agent_type == "viral",
                    AgentTask.created_at >= today_start,
                )
            )
        ).scalar_one()

    return {
        "scout": {
            "accounts_count": role_counts.get("scout", 0),
            "groups_discovered_today": groups_discovered_today,
            "groups_evaluated": groups_evaluated,
            "last_run": iso(last_scout_run),
        },
        "infiltrator": {
            "accounts_count": role_counts.get("infiltrator", 0),
            "groups_active": groups_active,
            "messages_sent_today": msgs_today,
        },
        "content": {
            "accounts_count": role_counts.get("content", 0),
            "pieces_generated_today": content_today,
            "last_piece": iso(last_content),
        },
        "viral": {
            "events_handled_today": viral_today,
            "last_event": iso(last_viral),
        },
        "bot": {
            "status": "running",
            "commands_handled": 0,
        },
    }


# ---------------------------------------------------------------------------
# WebSocket: /ws/live
# ---------------------------------------------------------------------------
@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    last_msg_id = 0
    last_content_id = 0
    last_task_id = 0

    # Initialize cursor at current max so we only push new things
    async with SessionLocal() as s:
        last_msg_id = (await s.execute(select(func.coalesce(func.max(MessageLog.id), 0)))).scalar_one()
        last_content_id = (await s.execute(select(func.coalesce(func.max(ContentPiece.id), 0)))).scalar_one()
        last_task_id = (await s.execute(select(func.coalesce(func.max(AgentTask.id), 0)))).scalar_one()

    try:
        while True:
            events: list[dict[str, Any]] = []
            async with SessionLocal() as s:
                new_msgs = (
                    await s.execute(
                        select(MessageLog.id, MessageLog.account_id, MessageLog.content, MessageLog.sent_at)
                        .where(MessageLog.id > last_msg_id)
                        .order_by(MessageLog.id)
                        .limit(50)
                    )
                ).all()
                for m in new_msgs:
                    last_msg_id = max(last_msg_id, m.id)
                    events.append({
                        "type": "message",
                        "id": m.id,
                        "account_id": m.account_id,
                        "content": (m.content or "")[:200],
                        "timestamp": iso(m.sent_at),
                    })

                new_content = (
                    await s.execute(
                        select(ContentPiece.id, ContentPiece.content_type, ContentPiece.content, ContentPiece.created_at)
                        .where(ContentPiece.id > last_content_id)
                        .order_by(ContentPiece.id)
                        .limit(50)
                    )
                ).all()
                for c in new_content:
                    last_content_id = max(last_content_id, c.id)
                    events.append({
                        "type": "content",
                        "id": c.id,
                        "content_type": c.content_type,
                        "content": (c.content or "")[:200],
                        "timestamp": iso(c.created_at),
                    })

                new_tasks = (
                    await s.execute(
                        select(AgentTask.id, AgentTask.task_type, AgentTask.status, AgentTask.account_id, AgentTask.created_at)
                        .where(AgentTask.id > last_task_id)
                        .order_by(AgentTask.id)
                        .limit(50)
                    )
                ).all()
                for t in new_tasks:
                    last_task_id = max(last_task_id, t.id)
                    events.append({
                        "type": "task",
                        "id": t.id,
                        "task_type": t.task_type,
                        "status": t.status,
                        "account_id": t.account_id,
                        "timestamp": iso(t.created_at),
                    })

            payload = {"ts": datetime.utcnow().isoformat(), "events": events}
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"error": str(e)}))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
