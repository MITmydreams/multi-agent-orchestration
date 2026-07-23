"""Quick script to update an account role."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from src.models.base import engine


async def main():
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE accounts SET role='content' WHERE phone='10000000002'")
        )
    # Verify
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT display_name, role, status, proxy_id FROM accounts ORDER BY id")
        )
        print(f"\n{'Name':<20} {'Role':<15} {'Status':<12} {'Proxy'}")
        print("-" * 55)
        for r in rows:
            print(f"{r[0]:<20} {r[1]:<15} {r[2]:<12} #{r[3]}")
    await engine.dispose()

asyncio.run(main())
