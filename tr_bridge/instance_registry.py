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

        Each instance is resumed independently: a failure on one instance is
        logged and does not prevent the others from resuming or the service
        from starting.

        Intended to be called once at startup inside the FastAPI lifespan.
        """
        results = await asyncio.gather(
            *(
                self._resume_safe(name, session)
                for name, session in self._sessions.items()
            ),
            return_exceptions=True,
        )
        failed = sum(1 for r in results if isinstance(r, BaseException))
        if failed:
            logger.warning(
                "InstanceRegistry: resume_all complete — %d/%d instance(s) failed.",
                failed,
                len(self._sessions),
            )
        else:
            logger.info("InstanceRegistry: resume_all complete.")

    async def _resume_safe(self, name: str, session: InstanceSession) -> None:
        """Resume *session*, catching and logging any unexpected exception.

        Re-raises after logging so that ``resume_all`` can count failures via
        ``asyncio.gather(return_exceptions=True)``.
        """
        try:
            await session.resume()
        except Exception:
            logger.exception("Instance %r: unexpected error during resume.", name)
            raise
