#!/usr/bin/env python3
"""
从 tgstat CSV 文件导入群组数据到 promo-bot 数据库
用法: PYTHONPATH=. .venv/bin/python scripts/import_tgstat_csv.py <csv_path>
"""
import asyncio
import csv
import sys
import os
import re
from datetime import datetime

import json
import asyncpg


DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot")
# asyncpg doesn't understand the +asyncpg part
DB_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")


def parse_int(val: str) -> int:
    """Parse numbers like '22 928' or '1.2k'"""
    if not val or not val.strip():
        return 0
    val = val.strip().replace(" ", "").replace("\xa0", "")
    if val.endswith("k"):
        return int(float(val[:-1]) * 1000)
    if val.endswith("M"):
        return int(float(val[:-1]) * 1000000)
    try:
        return int(val)
    except ValueError:
        return 0


def detect_language(title: str) -> str:
    """Simple language detection from title"""
    if not title:
        return "unknown"
    # CJK - Chinese/Japanese/Korean
    if re.search(r"[\u4e00-\u9fff]", title):
        return "zh"
    # Cyrillic - Russian/Ukrainian
    if re.search(r"[\u0400-\u04ff]", title):
        return "ru"
    # Arabic
    if re.search(r"[\u0600-\u06ff]", title):
        return "ar"
    # Default to English
    return "en"


def compute_grade(member_count: int, mau: int, messages_7d: int) -> tuple:
    """Compute grade and score from stats"""
    score = 0.0
    # Member count weight (0-40 points)
    if member_count >= 50000:
        score += 40
    elif member_count >= 10000:
        score += 30
    elif member_count >= 1000:
        score += 20
    elif member_count >= 100:
        score += 10

    # Activity weight (0-30 points)
    if messages_7d >= 10000:
        score += 30
    elif messages_7d >= 1000:
        score += 20
    elif messages_7d >= 100:
        score += 10

    # MAU weight (0-30 points)
    if mau >= 10000:
        score += 30
    elif mau >= 1000:
        score += 20
    elif mau >= 100:
        score += 10

    # Grade
    if score >= 70:
        grade = "S"
    elif score >= 50:
        grade = "A"
    elif score >= 30:
        grade = "B"
    else:
        grade = "C"

    return grade, score


async def import_csv(csv_path: str):
    print(f"Reading CSV: {csv_path}")
    conn = await asyncpg.connect(DB_URL)

    # Get existing tg_group_ids for dedup
    existing = set()
    rows = await conn.fetch("SELECT tg_group_id FROM groups")
    for r in rows:
        existing.add(r["tg_group_id"])
    print(f"Existing groups in DB: {len(existing)}")

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)

    imported = 0
    skipped = 0
    no_link = 0

    now = datetime.utcnow()

    for row in csv_rows:
        tme_link = row.get("tme_link", "").strip()
        if not tme_link:
            no_link += 1
            continue

        # Normalize tg_group_id - use username or link
        tg_group_id = tme_link
        username = row.get("username", "").strip()

        # Dedup
        if tg_group_id in existing or (username and f"@{username}" in existing):
            skipped += 1
            continue

        title = row.get("title", "").strip()
        member_count = parse_int(row.get("participants", "0"))
        messages_7d = parse_int(row.get("messages_7d", "0"))
        mau = parse_int(row.get("mau", "0"))
        category = row.get("category", "").strip()
        language = detect_language(title)

        grade, score = compute_grade(member_count, mau, messages_7d)

        try:
            await conn.execute(
                """INSERT INTO groups
                   (tg_group_id, title, username, member_count, daily_active,
                    language, topics, grade, score, admin_strictness, link_tolerance,
                    best_posting_hours, competitor_presence, active_kols,
                    recommended_approach, recommended_persona,
                    status, cooldown_until, last_activity, notes,
                    created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                           $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)
                """,
                tg_group_id,
                title,
                username if username else None,
                member_count if member_count > 0 else None,
                messages_7d,
                language,
                json.dumps([category] if category else []),
                grade,
                score,
                "medium",
                "medium",
                json.dumps([9, 12, 18, 21]),  # best_posting_hours default
                json.dumps([]),  # competitor_presence
                json.dumps([]),  # active_kols
                None,  # recommended_approach
                None,  # recommended_persona
                "discovered",
                None,  # cooldown_until
                None,  # last_activity
                None,  # notes
                now,
                now,
            )
            existing.add(tg_group_id)
            imported += 1
        except Exception as e:
            print(f"  Error inserting {tme_link}: {e}")
            skipped += 1

    await conn.close()

    print(f"\nImport complete:")
    print(f"  Total in CSV: {len(csv_rows)}")
    print(f"  Imported: {imported}")
    print(f"  Skipped (dup/error): {skipped}")
    print(f"  No link: {no_link}")


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "../../tgstat_groups_v2.csv"
    asyncio.run(import_csv(csv_path))
