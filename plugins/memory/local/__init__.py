from __future__ import annotations

from agent.local_memory.provider import LocalDeepMemoryProvider


def register(ctx):
    ctx.register_memory_provider(LocalDeepMemoryProvider())


__all__ = ["LocalDeepMemoryProvider", "register"]
