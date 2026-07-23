"""Register a verified Telegram account into the PostgreSQL database.

Usage:
    cd /Users/hermit/Desktop/OPS/Rwans_op/OPS_TG_OP/ops-orchestrator
    source .venv/bin/activate
    PYTHONPATH=. python scripts/register_account.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, text

from src.models.base import engine, get_session
from src.models.account import Account


async def register_account() -> None:
    """Insert the verified account into the accounts table."""

    # Account info from the import process
    account_data = {
        "phone": "10000000001",
        "phone_type": "virtual",
        "phone_provider": None,
        "username": "BoyceMorris",
        "display_name": "Boyce Morris",
        "bio": None,
        "avatar_url": None,
        "role": "executor",
        "persona_id": None,
        "language": "en",
        "status": "active",  # Already verified via opentele
        "proxy_id": 1,
        "risk_score": 0.0,
        "trust_score": 0.5,
        "nurture_start_date": datetime.utcnow(),
        "activated_date": datetime.utcnow(),  # Activated now since status=active
        "last_active": datetime.utcnow(),
        "session_string": "tdlib_sessions/account_1/10000000001.session",
        # Daily counters start at 0
        "messages_sent_today": 0,
        "outreach_messages_today": 0,
        "groups_active_today": 0,
        "new_groups_today": 0,
        "dms_initiated_today": 0,
        "links_sent_today": 0,
        # Lifetime counters start at 0
        "total_messages": 0,
        "total_outreach_messages": 0,
        "kicked_count": 0,
        # Risk flags
        "reported": False,
        "reported_at": None,
        "hibernated_until": None,
    }

    async with get_session() as session:
        # Check if account already exists
        stmt = select(Account).where(Account.phone == account_data["phone"])
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            print(f"Account +{account_data['phone']} already exists (id={existing.id})")
            print(f"  Status:       {existing.status}")
            print(f"  Role:         {existing.role}")
            print(f"  Trust Score:  {existing.trust_score}")
            print(f"  Risk Score:   {existing.risk_score}")
            print(f"  Session:      {existing.session_string}")
            print(f"  Created:      {existing.created_at}")
            print("\nSkipping insert. Delete the existing record first if you want to re-register.")
            return

        # Insert new account
        account = Account(**account_data)
        session.add(account)
        # Commit happens automatically via get_session context manager

    print(f"Account registered successfully!")

    # Verify by querying back
    async with get_session() as session:
        stmt = select(Account).where(Account.phone == account_data["phone"])
        result = await session.execute(stmt)
        acc = result.scalar_one_or_none()

        if acc is None:
            print("ERROR: Account not found after insert!")
            return

        print(f"\n{'='*60}")
        print(f"  ACCOUNT REGISTRATION CONFIRMED")
        print(f"{'='*60}")
        print(f"  ID:              {acc.id}")
        print(f"  Phone:           +{acc.phone}")
        print(f"  Phone Type:      {acc.phone_type}")
        print(f"  Username:        @{acc.username}")
        print(f"  Display Name:    {acc.display_name}")
        print(f"  Role:            {acc.role}")
        print(f"  Language:        {acc.language}")
        print(f"  Status:          {acc.status}")
        print(f"  Proxy ID:        {acc.proxy_id}")
        print(f"  Trust Score:     {acc.trust_score}")
        print(f"  Risk Score:      {acc.risk_score}")
        print(f"  Nurture Start:   {acc.nurture_start_date}")
        print(f"  Activated Date:  {acc.activated_date}")
        print(f"  Last Active:     {acc.last_active}")
        print(f"  Session File:    {acc.session_string}")
        print(f"  Created At:      {acc.created_at}")
        print(f"  Updated At:      {acc.updated_at}")
        print(f"{'='*60}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(register_account())
