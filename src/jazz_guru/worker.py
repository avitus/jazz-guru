from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from jazz_guru.config import get_settings
from jazz_guru.logging import get_logger

log = get_logger(__name__)

QUEUE_NAMES = ["distillation", "eval", "default"]


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
    # Seed the auto-distillation idle sweep. The sweep_job re-enqueues
    # itself so this only fires once per worker boot. Wrapped because a
    # Redis hiccup at startup shouldn't prevent the worker from running
    # other jobs.
    try:
        from jazz_guru.distillation.scheduler import schedule_idle_sweep

        schedule_idle_sweep()
    except Exception as e:
        log.warning("worker.idle_sweep_seed_failed", err=str(e))
    worker = Worker(queues, connection=queues[0].connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    run()
