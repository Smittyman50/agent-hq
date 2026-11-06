"""Worker process: pulls jobs from Valkey streams and runs agents.

Run as many as you like, on any machines that can reach Postgres and Valkey:

    python -m app.worker --pools default --concurrency 4
    python -m app.worker --pools desktop --name shockwave

Reliability:
* A job is acknowledged only after it finishes. While it runs, the worker refreshes
  the entry every AHQ_WORKER_HEARTBEAT seconds.
* If a worker dies, its entries go idle; any live worker on that pool reclaims them
  after AHQ_CLAIM_IDLE_MS and runs them again (up to AHQ_MAX_ATTEMPTS times).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from . import db
from .broker import Broker
from .config import settings
from .runtime import Runtime, open_checkpointer, slot_var
from .telemetry import setup_telemetry

log = logging.getLogger("agent_hq.worker")


class Worker:
    def __init__(self, runtime: Runtime, *, name: str, pools: list[str], concurrency: int,
                 claim_idle_ms: int | None = None, heartbeat: float | None = None) -> None:
        self.rt = runtime
        self.broker = runtime.broker
        self.name = name
        self.pools = pools
        self.concurrency = concurrency
        self.claim_idle_ms = claim_idle_ms or settings.claim_idle_ms
        self.heartbeat = heartbeat or settings.worker_heartbeat
        self.sem = asyncio.Semaphore(concurrency)
        self._loops: list[asyncio.Task] = []
        self._jobs: set[asyncio.Task] = set()
        self._running = False

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        for p in self.pools:
            await self.broker.ensure_group(p)
        self._running = True
        self._loops = [asyncio.create_task(self._read_loop()),
                       asyncio.create_task(self._reclaim_loop()),
                       asyncio.create_task(self._registry_loop())]
        log.info("worker %s started: pools=%s concurrency=%s", self.name, self.pools, self.concurrency)

    async def stop(self, *, graceful: bool = True) -> None:
        """graceful=False abandons in-flight jobs without acking (simulates a crash)."""
        self._running = False
        for t in self._loops:
            t.cancel()
        if graceful:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        else:
            for t in self._jobs:
                t.cancel()
            await asyncio.gather(*self._jobs, return_exceptions=True)
        await asyncio.gather(*self._loops, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.broker.remove_worker(self.name)

    # ------------------------------------------------------------ loops

    async def _read_loop(self) -> None:
        while self._running:
            await self.sem.acquire()
            try:
                entries = await self.broker.read(self.pools, self.name, block_ms=2000)
            except asyncio.CancelledError:
                self.sem.release()
                raise
            except Exception:
                self.sem.release()
                log.exception("stream read failed; retrying")
                await asyncio.sleep(2)
                continue
            if not entries:
                self.sem.release()
                continue
            self._spawn(*entries[0])

    async def _reclaim_loop(self) -> None:
        while self._running:
            await asyncio.sleep(max(self.claim_idle_ms / 2000, 0.1))
            for pool in self.pools:
                while self._running and not self.sem.locked():
                    await self.sem.acquire()
                    try:
                        claimed = await self.broker.claim_stale(pool, self.name, self.claim_idle_ms)
                    except Exception:
                        self.sem.release()
                        log.exception("reclaim failed")
                        break
                    if not claimed:
                        self.sem.release()
                        break
                    log.warning("reclaimed job %s from a dead worker", claimed[0][2])
                    self._spawn(*claimed[0])

    async def _registry_loop(self) -> None:
        while self._running:
            await self.broker.heartbeat_worker(self.name, {
                "name": self.name, "pools": self.pools, "concurrency": self.concurrency,
                "busy": len(self._jobs)}, ttl=int(self.heartbeat * 3) + 1)
            await asyncio.sleep(self.heartbeat)

    # ------------------------------------------------------------ jobs

    def _spawn(self, pool: str, entry_id: str, job_id: str) -> None:
        task = asyncio.create_task(self._handle(pool, entry_id, job_id))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _handle(self, pool: str, entry_id: str, job_id: str) -> None:
        slot_var.set(self.sem)
        toucher = asyncio.create_task(self._touch(pool, entry_id))
        try:
            await self.rt.execute(job_id, worker=self.name)
            await self.broker.ack(pool, entry_id)
        except asyncio.CancelledError:
            raise  # crash/shutdown: leave unacked so another worker reclaims it
        except Exception:
            # Infrastructure failure (DB/Valkey). Leave unacked; it will be retried.
            log.exception("job %s errored outside the agent; leaving for retry", job_id)
        finally:
            toucher.cancel()
            self.sem.release()

    async def _touch(self, pool: str, entry_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            with contextlib.suppress(Exception):
                await self.broker.touch(pool, self.name, entry_id)


async def run_worker(name: str, pools: list[str], concurrency: int) -> None:
    setup_telemetry(service_suffix="worker")
    db.init_db()
    broker = Broker(settings.redis_url)
    async with open_checkpointer() as saver:
        worker = Worker(Runtime(broker, saver), name=name, pools=pools, concurrency=concurrency)
        await worker.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            log.info("shutting down; finishing in-flight jobs")
            await worker.stop(graceful=True)
            await broker.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Agent HQ worker")
    p.add_argument("--name", default=settings.worker_name)
    p.add_argument("--pools", default=",".join(settings.worker_pools),
                   help="comma-separated worker pools to serve")
    p.add_argument("--concurrency", type=int, default=settings.worker_concurrency)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    pools = [x.strip() for x in a.pools.split(",") if x.strip()]
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker(a.name, pools, a.concurrency))


if __name__ == "__main__":
    main()
