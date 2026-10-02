"""Assign every interview without an owner (created before accounts) to one user.

Run: uv run python scripts/claim_interviews.py --owner <user id> [--dry-run]
The user id is the JWT `sub` (GET /api/me shows it). Interviews that already
belong to someone are never touched, and no quota is spent.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select, update

from interview_agent.config import settings
from interview_agent.interview import db


async def main(args):
    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    try:
        async with sessionmaker() as session:
            if args.dry_run:
                count = await session.scalar(
                    select(func.count())
                    .select_from(db.Conversation)
                    .where(db.Conversation.owner_id.is_(None))
                )
            else:
                result = await session.execute(
                    update(db.Conversation)
                    .where(db.Conversation.owner_id.is_(None))
                    # An ownership change, not an interview change: updated_at stays.
                    .values(owner_id=args.owner, updated_at=db.Conversation.updated_at)
                )
                await session.commit()
                count = result.rowcount
        verb = "would be claimed" if args.dry_run else "claimed"
        print(f"{count} interviews {verb} by {args.owner}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="the user's id (JWT sub)")
    parser.add_argument("--dry-run", action="store_true", help="count without changing anything")
    args = parser.parse_args()
    # A pasted sub with stray whitespace would never match the JWT's.
    args.owner = args.owner.strip()
    if not args.owner:
        parser.error("--owner must not be empty")
    asyncio.run(main(args))
