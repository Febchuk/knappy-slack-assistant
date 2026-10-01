"""Thread-scoped conversation memory."""

from __future__ import annotations


class ThreadMemory:
    def __init__(self, limit: int = 10) -> None:
        self.limit = limit
        self._threads: dict[str, list[dict[str, str]]] = {}

    def append(self, thread_ts: str, role: str, text: str) -> None:
        bucket = self._threads.setdefault(thread_ts, [])
        bucket.append({"role": role, "text": text})
        if len(bucket) > self.limit:
            del bucket[:-self.limit]

    def history(self, thread_ts: str) -> list[dict[str, str]]:
        return list(self._threads.get(thread_ts, []))

    def prompt_block(self, thread_ts: str) -> str:
        lines = [f"{item['role']}: {item['text']}" for item in self.history(thread_ts)]
        return "\n".join(lines)
