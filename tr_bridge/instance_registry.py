"""Instance registry — initialises and provides access to per-instance sessions."""

from __future__ import annotations

import asyncio
import logging

from tr_bridge.config import Config
from tr_bridge.session import InstanceSession

logger = logging.getLogger(__name__)


class InstanceNotFoundError(Exception):
    """Raised when a requested instance name is not in the registry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Instance not found: {name!r}")
        self.name = name


class InstanceRegistry:
    """Holds one :class:`~tr_bridge.session.InstanceSession` per configured instance."""

    def __init__(self, config: Config) -> None:
        self._sessions: dict[str, InstanceSession] = {
            inst.name: InstanceSession(
                config=inst,
                session_dir=config.session_dir(inst.name),
                tfa_timeout=config.tfa_timeout,
            )
            for inst in config.instances
        }

    def get(self, name: str) -> InstanceSession:
        """Return the session for *name*.

        Raises:
            InstanceNotFoundError: if *name* is not a configured instance.
        """
        try:
            return self._sessions[name]
        except KeyError:
            raise InstanceNotFoundError(name) from None

    async def resume_all(self) -> None:
        """Attempt to resume all sessions concurrently.

        Intended to be called once at startup inside the FastAPI lifespan.
        """
        await asyncio.gather(*(session.resume() for session in self._sessions.values()))
        logger.info("InstanceRegistry: resume_all complete.")
