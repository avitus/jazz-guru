from __future__ import annotations

import os
import socket

from redis import Redis
from rq import Queue, Worker

from jazz_guru.config import get_settings
from jazz_guru.logging import get_logger

log = get_logger(__name__)

QUEUE_NAMES = ["distillation", "eval", "default"]

# Redis-backed singleton lease for the idle-sweep chain. Multiple worker
# processes booting concurrently would otherwise each call
# schedule_idle_sweep() and seed parallel recurring chains. SETNX gates
# the initial seed; the sweep_job refreshes the lease each tick (see
# refresh_sweep_lease) so the lock survives across the chain's lifetime
# and only expires if the chain itself goes silent.
SWEEP_LEADER_KEY = "jazz_guru:idle_sweep_leader"


def _sweep_lease_ttl_sec() -> int:
    # Generous TTL so a single missed refresh doesn't release the lease;
    # capped at >=60s so a misconfigured sub-30s sweep interval still
    # produces a usable lock window.
    return max(60, get_settings().jg_distill_sweep_interval_sec * 3)


def _try_acquire_sweep_lease() -> bool:
    """Atomic SETNX. True if this process became the sweep leader."""
    return bool(
        get_redis().set(
            SWEEP_LEADER_KEY,
            f"{socket.gethostname()}:{os.getpid()}",
            nx=True,
            ex=_sweep_lease_ttl_sec(),
        )
    )


def refresh_sweep_lease() -> None:
    """Extend the sweep lease unconditionally.

    Called by ``sweep_job`` on each tick so the lease stays fresh while
    the chain is alive. If the original leader died and another worker
    is now running the chain, that worker simply takes over — only one
    chain can ever be running at a time because ``schedule_idle_sweep``
    is only called by ``sweep_job`` (chain continuation) or by ``run``
    on boot under a SETNX gate.
    """
    get_redis().set(SWEEP_LEADER_KEY, "active", ex=_sweep_lease_ttl_sec())


def get_redis() -> Redis:
    url = get_settings().redis_url
    if not url:
        raise RuntimeError(
            "REDIS_URL is not set. Either set it in .env or run distillation/eval inline "
            "via `jazz-guru distill <id> --sync` and `jazz-guru evalrun`."
        )
    return Redis.from_url(url)


def get_queues() -> list[Queue]:
    r = get_redis()
    return [Queue(name, connection=r) for name in QUEUE_NAMES]


def run() -> None:
    log.info("worker.starting", queues=QUEUE_NAMES)
    queues = get_queues()
    # Seed the auto-distillation idle sweep, but only if we win the
    # SETNX lease — multiple workers booting at once would otherwise
    # each start their own recurring chain.
    try:
        from jazz_guru.distillation.scheduler import schedule_idle_sweep

        if _try_acquire_sweep_lease():
            schedule_idle_sweep()
        else:
            log.info("worker.idle_sweep_skip_not_leader")
    except Exception as e:
        log.warning("worker.idle_sweep_seed_failed", err=str(e))
    worker = Worker(queues, connection=queues[0].connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    run()
