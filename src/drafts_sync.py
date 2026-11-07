"""Keep Lola's draft list in sync with Gmail.

A draft that disappeared from Gmail was either sent (a sent message appeared in
the conversation after the draft was created) or deleted by the user. Runs at
the start of every scheduled run and whenever the user opens /drafts.
"""

import asyncio
import logging
from collections import defaultdict

from src import config
from src.gmail_client import check_drafts
from src.models import utcnow
from src.state import get_store

log = logging.getLogger(__name__)


async def sync_user_drafts(user_id: int) -> dict[str, int]:
    """Update statuses of the user's pending drafts. Returns counts per new status."""
    s = config.settings
    store = get_store()
    by_account: dict[str, list[dict]] = defaultdict(list)
    for d in await store.get_pending_drafts(user_id):
        by_account[d.get("account", "")].append(d)

    counts = {"sent": 0, "discarded": 0}
    for email, drafts in by_account.items():
        account = await store.get_account(email)
        if account is None or account.user_id != user_id or account.needs_reauth or "gmail" not in account.providers:
            continue
        try:
            states = await asyncio.to_thread(
                check_drafts, account=account, client_id=s.google_client_id, client_secret=s.google_client_secret, drafts=drafts
            )
        except Exception as exc:  # noqa: BLE001 — sync is best effort
            log.warning("Draft sync failed for %s: %s", email, exc)
            continue
        for draft_id, status in states.items():
            await store.update_draft(draft_id, {"status": status, "status_changed_at": utcnow(), "status_source": "gmail"})
            counts[status] += 1
    return counts


def sync_note(counts: dict[str, int]) -> str:
    parts = []
    if counts.get("sent"):
        parts.append(f"{counts['sent']} sent")
    if counts.get("discarded"):
        parts.append(f"{counts['discarded']} deleted")
    return f"🔄 Drafts updated from Gmail: {', '.join(parts)}." if parts else ""
