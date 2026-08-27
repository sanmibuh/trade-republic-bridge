"""Tests for tr_bridge.instance_registry — InstanceRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tr_bridge.config import Config, InstanceConfig
from tr_bridge.instance_registry import InstanceRegistry


def _make_config(names: list[str] | None = None, tfa_timeout: int = 120) -> Config:
    names = names or ["alice", "bob"]
    instances = [
        InstanceConfig(name=n, phone=f"+4912345{i}", pin="1234")
        for i, n in enumerate(names)
    ]
    cfg = MagicMock(spec=Config)
    cfg.instances = instances
    cfg.instance_names = names
    cfg.tfa_timeout = tfa_timeout
    cfg.session_dir.side_effect = lambda name: f"/data/tr_session_{name}"
    return cfg


class TestGetInstance:
    def test_get_returns_session_for_known_name(self) -> None:
        cfg = _make_config(["alice"])
        registry = InstanceRegistry(cfg)
        session = registry.get("alice")
        assert session is not None

    def test_get_raises_404_for_unknown_instance(self) -> None:
        from tr_bridge.instance_registry import InstanceNotFoundError

        cfg = _make_config(["alice"])
        registry = InstanceRegistry(cfg)
        with pytest.raises(InstanceNotFoundError):
            registry.get("unknown")

    def test_get_returns_same_object_on_repeated_calls(self) -> None:
        cfg = _make_config(["alice"])
        registry = InstanceRegistry(cfg)
        assert registry.get("alice") is registry.get("alice")

    def test_all_configured_instances_are_registered(self) -> None:
        cfg = _make_config(["alice", "bob", "charlie"])
        registry = InstanceRegistry(cfg)
        for name in ["alice", "bob", "charlie"]:
            assert registry.get(name) is not None


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_all_calls_resume_on_each_session(self) -> None:
        cfg = _make_config(["alice", "bob"])
        registry = InstanceRegistry(cfg)

        for name in ["alice", "bob"]:
            session = registry.get(name)
            session.resume = AsyncMock()

        await registry.resume_all()

        for name in ["alice", "bob"]:
            registry.get(name).resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_all_continues_when_one_instance_raises(self) -> None:
        """A failure on one instance must not prevent others from resuming."""
        cfg = _make_config(["alice", "bob"])
        registry = InstanceRegistry(cfg)

        registry.get("alice").resume = AsyncMock(side_effect=RuntimeError("disk error"))
        registry.get("bob").resume = AsyncMock()

        # Must not raise even though alice's resume fails.
        await registry.resume_all()

        registry.get("bob").resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_all_does_not_raise_when_all_fail(self) -> None:
        cfg = _make_config(["alice", "bob"])
        registry = InstanceRegistry(cfg)

        for name in ["alice", "bob"]:
            registry.get(name).resume = AsyncMock(side_effect=OSError("read-only fs"))

        # Service startup must survive all-instance resume failure.
        await registry.resume_all()

    @pytest.mark.asyncio
    async def test_resume_all_warning_fired_when_instance_fails(self) -> None:
        """Failed count must reach the warning branch (failed > 0)."""
        from unittest.mock import patch

        cfg = _make_config(["alice", "bob"])
        registry = InstanceRegistry(cfg)

        registry.get("alice").resume = AsyncMock(side_effect=RuntimeError("boom"))
        registry.get("bob").resume = AsyncMock()

        with patch("tr_bridge.instance_registry.logger") as mock_logger:
            await registry.resume_all()

        mock_logger.warning.assert_called_once()
        # First positional arg after the format string is the failed count.
        failed_count = mock_logger.warning.call_args[0][1]
        assert failed_count == 1
