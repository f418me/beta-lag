"""Bounded asynchronous input queue with deterministic, single-consumer decisions."""

import asyncio
from collections.abc import Iterable, Iterator
from pathlib import Path

from beta_lag.config import Settings
from beta_lag.engine import Engine
from beta_lag.models import EVENT_ADAPTER, Event
from beta_lag.paper import PaperBroker
from beta_lag.runtime import process_event
from beta_lag.storage import Store


async def write_call(function, *args):
    """Wait for an in-flight write before allowing cancellation and rollback."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def read_events(path: Path) -> Iterator[Event]:
    with path.open() as file:
        for line_number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                yield EVENT_ADAPTER.validate_json(line)
            except ValueError as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error


async def replay(events: Iterable[Event], config: Settings, store: Store) -> dict:
    queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=256)
    engine = Engine(config)
    broker = PaperBroker(config)
    count = 0

    async def produce() -> None:
        for event in events:
            await queue.put(event)
        await queue.put(None)

    async def consume() -> None:
        nonlocal count
        while (event := await queue.get()) is not None:
            process_event(engine, broker, event)
            # The writer is serialized; file I/O does not block the event loop.
            await write_call(store.append, count, event)
            count += 1

    await write_call(store.begin, config)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            group.create_task(consume())
        return await write_call(store.finish, engine, broker, count)
    except BaseException:
        await write_call(store.rollback)
        raise
