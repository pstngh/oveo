from __future__ import annotations

from oveo.models import Thread, User


def may_access_thread(actor: User, thread: Thread) -> bool:
    return actor.id == thread.owner_id or actor.role == "owner"
