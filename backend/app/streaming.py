import asyncio
import secrets
from typing import Any


class Hub:
    def __init__(self) -> None:
        self.boot = secrets.token_hex(8)
        self.version = 0
        self.current: dict[str, Any] = {}
        self.clients: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()

    def publish(self, snapshot: dict[str, Any]) -> None:
        self.current = snapshot
        self.version += 1
        for queue in self.clients:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait((f"{self.boot}:{self.version}", snapshot))

    def subscribe(self) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        if len(self.clients) >= 4:
            raise OverflowError("At most four live streams are supported")
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=1)
        self.clients.add(queue)
        queue.put_nowait((f"{self.boot}:{self.version}", self.current))
        return queue
