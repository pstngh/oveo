from __future__ import annotations

from oveo.models import Thread, User


def may_access_thread(actor: User, thread: Thread) -> bool:
    """Keep every conversation private to the account that owns it."""

    return actor.id == thread.owner_id
