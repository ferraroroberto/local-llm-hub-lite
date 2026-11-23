"""Named thread pools, one per class of blocking work.

The event loop's default pool is one shared queue: a slow ``nvidia-smi`` call or
a model load waiting for readiness sits in front of every other off-loop call.
Each class of work here has its own :class:`TimedExecutor`, so a slow class
queues behind itself only, and the request-timing middleware can report which
pool a slow request waited in (rules: project-scaffolding docs/polling-budget.md).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from typing import Any, Callable, TypeVar

from .request_timing import TimedExecutor

T = TypeVar("T")

# Admin polls and the per-model readiness probes the Models tab fans out in one
# request: short calls that must never wait behind a heavy one.
HOT_POLL = TimedExecutor("hot-poll", 8)
# Install-check batteries and log reads: shell out for seconds, rare.
HEAVY_READ = TimedExecutor("heavy-read", 2)
# A user's click that runs a fix: one at a time per click, a few clicks at once.
USER_ACTION = TimedExecutor("user-action", 4)
# Backend start/stop and the idle-unload sweep.
BACKEND_LIFECYCLE = TimedExecutor("backend-lifecycle", 4)
# A request parked until a cold on-demand backend answers; this holds a thread for
# the whole model load, so it never shares a pool with the short lifecycle calls.
BACKEND_READY = TimedExecutor("backend-ready", 8)
# The 2 s resource sampler runs with nobody watching and has no request to charge.
SAMPLER = TimedExecutor("sampler", 1)


async def run_in_pool(pool: TimedExecutor, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` on ``pool`` off the event loop, in a copy of the caller's context.

    Same contract as ``asyncio.to_thread`` (context variables reach the worker),
    on a named pool instead of the shared default one.
    """
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(pool, call)
