"""Bulk-import target groups into the `groups` table.

This bypasses the scout discovery loop. Use it when you already have a
hand-curated list of target groups and want executors to start joining
them immediately.

Input file format (one entry per line, blank lines and `#` comments allowed)::

    @community_dao
    https://t.me/techsignals
    https://t.me/+abcDEF123hash
    -1001234567890
    # Title override:
    @some_group | Some Group Title | en | 5000

Each non-comment line accepts up to 4 pipe-separated fields:
    identifier | title | language | member_count

Only `identifier` is required; the rest fall back to placeholders. The
identifier is stored verbatim in `tg_group_id`; the patched
`user_client.join_group()` knows how to parse `@username`, `t.me/+hash`,
and bare numeric chat ids at join time.

Inserted rows get ``status='evaluated'`` so the scheduler picks them up
on the next cycle and assigns join_group tasks.

Usage::

    PYTHONPATH=. python scripts/import_groups.py path/to/target_groups.txt
    PYTHONPATH=. python scripts/import_groups.py path/to/target_groups.txt --grade A
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog
from sqlalchemy import select, text

from src.config.settings import settings
from src.models.base import async_session_factory, engine
from src.models.group import Group

logger = structlog.get_logger(__name__)


def parse_line(line: str) -> dict | None:
    """Parse one line into a group dict, or return None if it's blank/comment."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parts = [p.strip() for p in line.split("|")]
    identifier = parts[0]
    title = parts[1] if len(parts) > 1 and parts[1] else identifier
    language = parts[2] if len(parts) > 2 and parts[2] else "en"
    try:
        member_count = int(parts[3]) if len(parts) > 3 and parts[3] else 0
    except ValueError:
        member_count = 0

    # Normalise identifier into something stable for the unique key.
    # We keep the user-provided form so user_client.join_group() can parse it,
    # but strip surrounding noise.
    tg_group_id = identifier
    username: str | None = None

    if identifier.startswith("https://t.me/") or identifier.startswith("http://t.me/") or identifier.startswith("t.me/"):
        tail = identifier.split("t.me/", 1)[1].rstrip("/")
        if tail.startswith("+") or tail.startswith("joinchat/"):
            # Private invite link — no public username
            tg_group_id = identifier  # keep full url for join_group to parse
        else:
            username = tail.lstrip("@")
            tg_group_id = "@" + username
    elif identifier.startswith("@"):
        username = identifier[1:]
        tg_group_id = identifier
    elif identifier.lstrip("-").isdigit():
        # Numeric chat id (e.g. -1001234567890)
        tg_group_id = identifier
    else:
        # Bare username
        username = identifier
        tg_group_id = "@" + identifier

    return {
        "tg_group_id": tg_group_id,
        "title": title,
        "username": username,
        "language": language,
        "member_count": member_count,
    }


async def import_groups(file_path: Path, default_grade: str, default_status: str) -> None:
    if not file_path.exists():
        logger.error("import.file_not_found", path=str(file_path))
        sys.exit(2)

    raw_lines = file_path.read_text(encoding="utf-8").splitlines()
    parsed = [p for p in (parse_line(ln) for ln in raw_lines) if p]
    if not parsed:
        logger.warning("import.no_entries", path=str(file_path))
        return

    logger.info("import.start", count=len(parsed), file=str(file_path))

    inserted = 0
    skipped = 0
    async with async_session_factory() as session:
        for entry in parsed:
            existing = await session.scalar(
                select(Group).where(Group.tg_group_id == entry["tg_group_id"])
            )
            if existing is not None:
                skipped += 1
                logger.debug("import.skip_existing", tg_group_id=entry["tg_group_id"])
                continue

            group = Group(
                tg_group_id=entry["tg_group_id"],
                title=entry["title"],
                username=entry["username"],
                member_count=entry["member_count"],
                language=entry["language"],
                topics=[],
                grade=default_grade,
                score=50.0,
                admin_strictness="medium",
                link_tolerance="medium",
                best_posting_hours=[],
                competitor_presence=[],
                active_kols=[],
                status=default_status,
                notes="bulk-imported via scripts/import_groups.py",
            )
            session.add(group)
            inserted += 1

        await session.commit()

    logger.info("import.done", inserted=inserted, skipped=skipped, total=len(parsed))

    # Print a friendly summary
    print()
    print(f"  📊 Import summary")
    print(f"  {'─' * 40}")
    print(f"  Parsed:    {len(parsed)}")
    print(f"  Inserted:  {inserted}")
    print(f"  Skipped:   {skipped} (already in DB)")
    print(f"  Status:    {default_status}")
    print(f"  Grade:     {default_grade}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-import target groups into the DB.")
    parser.add_argument("file", help="Path to a text file with one group identifier per line.")
    parser.add_argument("--grade", default="B", choices=["S", "A", "B", "C"],
                        help="Default grade for imported groups (default: B)")
    parser.add_argument("--status", default="evaluated",
                        choices=["evaluated", "active", "infiltrating", "discovered"],
                        help="Initial status (default: evaluated, picked up by scheduler)")
    args = parser.parse_args()

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    logger.info("import.boot", db_url=settings.database_url)

    try:
        asyncio.run(_runner(Path(args.file), args.grade, args.status))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("import.failed", error=str(exc))
        sys.exit(1)


async def _runner(file_path: Path, grade: str, status: str) -> None:
    try:
        await import_groups(file_path, grade, status)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
