"""Quick status dashboard for all TG agents.

Usage:
    cd ops-orchestrator && .venv/bin/python scripts/status.py
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text, func, select
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://ops:ops@localhost:5432/ops_orchestrator"

ROLE_ICONS = {
    "scout": "🔍",
    "executor": "🕵️",
    "content": "📝",
    "backup": "💤",
}

STATUS_ICONS = {
    "active": "🟢",
    "nurturing": "🌱",
    "hibernating": "❄️",
    "abandoned": "🔴",
}


async def main():
    engine = create_async_engine(DB_URL, echo=False)

    print()
    print("=" * 64)
    print("  🤖 TG Agent 工作状态面板")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    async with engine.connect() as conn:
        # --- Accounts ---
        rows = await conn.execute(text("""
            SELECT id, display_name, username, role, status, proxy_id,
                   risk_score, trust_score,
                   messages_sent_today, outreach_messages_today,
                   groups_active_today, total_messages, total_outreach_messages,
                   kicked_count, last_active
            FROM accounts ORDER BY id
        """))
        accounts = rows.fetchall()

        print(f"\n  📱 账号概览 ({len(accounts)} 个)")
        print("  " + "-" * 60)
        for a in accounts:
            role_icon = ROLE_ICONS.get(a[3], "❓")
            status_icon = STATUS_ICONS.get(a[4], "❓")
            last_active = a[14].strftime("%m-%d %H:%M") if a[14] else "从未"
            print(f"  {status_icon} {a[1]:<16} @{a[2] or 'N/A':<16} {role_icon} {a[3]:<12} Proxy#{a[5]}")
            print(f"     风险: {a[6]:.2f}  信任: {a[7]:.2f}  "
                  f"今日消息: {a[8]}  触达: {a[9]}  活跃群: {a[10]}")
            print(f"     累计消息: {a[11]}  累计触达: {a[12]}  "
                  f"被踢次数: {a[13]}  最后活跃: {last_active}")
            print()

        # --- Groups ---
        groups_row = await conn.execute(text("""
            SELECT status, count(*) FROM groups GROUP BY status ORDER BY count(*) DESC
        """))
        groups = groups_row.fetchall()

        print("  📊 群组统计")
        print("  " + "-" * 60)
        if groups:
            for g in groups:
                print(f"     {g[0]:<16} {g[1]} 个")
        else:
            print("     暂无群组数据")

        # --- Group details ---
        gd_rows = await conn.execute(text("""
            SELECT g.title, g.grade, g.member_count, g.language, g.status,
                   count(ga.account_id) as agents_in
            FROM groups g
            LEFT JOIN group_accounts ga ON g.id = ga.group_id
            GROUP BY g.id, g.title, g.grade, g.member_count, g.language, g.status
            ORDER BY g.grade ASC, g.member_count DESC
            LIMIT 20
        """))
        group_details = gd_rows.fetchall()

        if group_details:
            print(f"\n  🏠 群组详情 (Top 20)")
            print("  " + "-" * 60)
            print(f"  {'群名':<24} {'评级':<4} {'成员':<8} {'语言':<6} {'状态':<10} {'Agent数'}")
            for gd in group_details:
                title = (gd[0] or "Unknown")[:22]
                print(f"  {title:<24} {gd[1] or '?':<4} {gd[2] or 0:<8} "
                      f"{gd[3] or '?':<6} {gd[4]:<10} {gd[5]}")

        # --- Content ---
        content_rows = await conn.execute(text("""
            SELECT content_type, count(*), max(created_at)
            FROM content_pieces
            GROUP BY content_type
            ORDER BY count(*) DESC
        """))
        content = content_rows.fetchall()

        print(f"\n  📄 内容生产统计")
        print("  " + "-" * 60)
        if content:
            for c in content:
                last = c[2].strftime("%m-%d %H:%M") if c[2] else "N/A"
                print(f"     {c[0]:<20} {c[1]:>4} 篇  最新: {last}")
        else:
            print("     暂无内容")

        # --- Daily metrics ---
        metrics_rows = await conn.execute(text("""
            SELECT date, active_accounts, messages_sent, outreach_messages,
                   new_registrations, daily_reach, avg_risk_score
            FROM daily_metrics
            ORDER BY date DESC
            LIMIT 7
        """))
        metrics = metrics_rows.fetchall()

        print(f"\n  📈 最近 7 天指标")
        print("  " + "-" * 60)
        if metrics:
            print(f"  {'日期':<12} {'活跃号':<8} {'消息':<8} {'触达':<8} {'新注册':<8} {'触达':<8} {'风险'}")
            for m in metrics:
                print(f"  {str(m[0]):<12} {m[1] or 0:<8} {m[2] or 0:<8} "
                      f"{m[3] or 0:<8} {m[4] or 0:<8} {m[5] or 0:<8} {m[6] or 0:.2f}")
        else:
            print("     暂无指标数据")

    print()
    print("=" * 64)
    # Check AI mode
    try:
        from src.config.settings import settings as app_settings
        if app_settings.anthropic_api_key:
            print(f"  🧠 AI 内容生成: API 模式 ({app_settings.ai_model})")
        else:
            print("  💡 提示: 内容生成目前使用模板模式 (未配置 API Key)")
    except Exception:
        print("  💡 内容生成模式未知")
    print("=" * 64)
    print()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
