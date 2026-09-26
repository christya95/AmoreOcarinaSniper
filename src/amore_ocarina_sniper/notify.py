"""Operator push notifications via ntfy (https://ntfy.sh) — optional, off the critical path.

Why: the operator may also be trying to buy by hand (phone stock apps). The one thing that
prevents two consoles is knowing within seconds what the bot did. Messages carry state and
policy reasons only — never card numbers, addresses, cookies or tokens.

Every send is fire-and-forget with a short timeout and swallows all errors; a notification
failure must never influence an attempt or its outcome.
"""

from __future__ import annotations

import asyncio
import logging

from .config import NotifyConfig

log = logging.getLogger("ocarina.notify")

PRIORITY_URGENT = "5"
PRIORITY_HIGH = "4"
PRIORITY_DEFAULT = "3"


class Notifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config
        self._tasks: set[asyncio.Task] = set()
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.ntfy_topic)

    def fire(self, title: str, message: str, *, priority: str = PRIORITY_DEFAULT, tags: str = "") -> None:
        """Schedule a notification without awaiting it. Safe to call from anywhere."""
        if not self.enabled:
            return
        task = asyncio.create_task(self.send(title, message, priority=priority, tags=tags))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(
        self, title: str, message: str, *, priority: str = PRIORITY_DEFAULT, tags: str = ""
    ) -> bool:
        if not self.enabled:
            return False
        import aiohttp

        url = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        headers = {"Title": title[:200], "Priority": priority}
        if tags:
            headers["Tags"] = tags
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=message[:4000].encode("utf-8"), headers=headers) as resp:
                    ok = 200 <= resp.status < 300
                    if not ok:
                        log.warning("ntfy responded %s", resp.status)
                    else:
                        self.sent += 1
                    return ok
        except Exception as exc:  # noqa: BLE001 - notifications never raise
            log.warning("ntfy send failed: %r", exc)
            return False

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
