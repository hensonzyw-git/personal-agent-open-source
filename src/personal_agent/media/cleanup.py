"""Bounded production media retention and durable deletion worker."""
from sqlalchemy import select, or_, and_

from personal_agent.media.deletion import mark_media_deleting, reap_media_object
from personal_agent.media.locking import media_locks, MediaLockError
from personal_agent.storage.models import MediaObject
from personal_agent_core.sqlite import run_write_transaction


def cleanup_media(sessions, *, store, keyring, limits, now, batch_size=100):
    eligible = or_(
        MediaObject.state.in_(("deleting", "reaping", "expired", "rejected")),
        and_(MediaObject.state == "pending", MediaObject.expires_at <= now),
        and_(MediaObject.state == "uploading", MediaObject.claim_deadline <= now),
        and_(MediaObject.state.in_(("uploaded", "ready")),
             MediaObject.updated_at <= now - limits.retention_ttl),
    )
    with sessions() as session:
        ids = session.scalars(select(MediaObject.media_id).where(eligible)
                              .order_by(MediaObject.updated_at).limit(batch_size)).all()
    results = {"processed": 0, "deferred": 0}
    for media_id in ids:
        try:
            with media_locks(store.roots.root, [media_id], blocking=False):
                with sessions() as session:
                    def decide():
                        # Recheck inside the lock and the fresh write transaction.
                        if session.scalar(select(MediaObject.media_id).where(
                            MediaObject.media_id == media_id, eligible
                        )) is not None:
                            mark_media_deleting(session, media_id=media_id,
                                                keyring=keyring, now=now)
                            return True
                        return False
                    decided = run_write_transaction(session, decide)
            if decided:
                with sessions() as session:
                    outcome = reap_media_object(session, store=store, media_id=media_id, now=now)
                results["deferred" if outcome.value == "deferred" else "processed"] += 1
        except MediaLockError:
            results["deferred"] += 1
    return results


def main():
    import argparse
    import json
    from pathlib import Path
    from personal_agent.keys import load_agent_data_keyring
    from personal_agent.media.config import media_config_from_env, ROOT_ENV
    from personal_agent.storage.engine import create_database_engine, session_factory
    from personal_agent_core.timeutil import utc_now
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get(ROOT_ENV):
        return  # Media is deliberately disabled on text-only deployments.
    config = media_config_from_env()
    if config is None:
        raise SystemExit("media configured but incomplete; cleanup refused")
    if not args.database.is_file():
        raise SystemExit("media database missing")
    keyring = load_agent_data_keyring()
    engine = create_database_engine(args.database)
    try:
        print(json.dumps(cleanup_media(session_factory(engine), store=config.store(keyring),
                                       keyring=keyring, limits=config.limits(), now=utc_now())))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
