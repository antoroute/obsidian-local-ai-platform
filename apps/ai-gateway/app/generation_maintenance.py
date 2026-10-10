"""Reconcile expired relay leases and purge expired user data.

Run once by default, or --loop in an internal container. The request handlers
also reconcile leases, so this service is not required to execute a step.
"""
import argparse
import logging
import signal
import threading

from sqlalchemy import select, text

from app.config import get_settings
from app.database import get_session_factory
from app.generation_store import lock_user, purge_user, reconcile_user
from app.models import GenerationUserState

logger = logging.getLogger(__name__)


def maintain() -> None:
    settings = get_settings()
    with get_session_factory()() as session:
        user_ids = list(session.scalars(select(GenerationUserState.user_id)))
    for user_id in user_ids:
        with get_session_factory()() as session:
            lock_user(session, user_id)
            reconcile_user(session, user_id, settings)
            purge_user(session, user_id, settings)
            session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        with get_session_factory()() as session:
            session.execute(text("SELECT 1 FROM generation_user_states LIMIT 1"))
        return
    logging.basicConfig(level=logging.INFO)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            maintain()
        except Exception:
            # SQL exceptions may include bound user text; never log exceptions.
            logger.error("generation_maintenance_failed")
            if not args.loop:
                raise SystemExit(1)
        if not args.loop or args.once:
            return
        stopped.wait(60)


if __name__ == "__main__":
    main()
