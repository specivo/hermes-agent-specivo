"""How the memory provider is offered: opt-in through ``memory.provider``, with both Hermes loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from conftest import BASE_URL, FakeCtx

from hermes_specivo.memory import SpecivoMemoryProvider
from hermes_specivo.plugin import HOOKS, is_memory_loader, register

ROOT = Path(__file__).resolve().parent.parent


class MemoryCtx(FakeCtx):
    """The plugin manager's context in a Hermes that knows memory providers."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        super().__init__(settings)
        self.providers: list[Any] = []

    def register_memory_provider(self, provider: Any) -> None:
        self.providers.append(provider)


class MemoryLoaderCtx:
    """Hermes's memory loader context: it forwards ``register_*`` calls only (no ``get_config``)."""

    def __init__(self) -> None:
        self.providers: list[Any] = []
        self.other: list[str] = []

    def register_memory_provider(self, provider: Any) -> None:
        self.providers.append(provider)

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("register_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.other.append(name)


def test_plugin_manager_load_registers_everything_and_offers_a_provider() -> None:
    ctx = MemoryCtx({"url": BASE_URL})
    register(ctx)
    assert sorted(ctx.hooks) == sorted(HOOKS) and set(ctx.commands) == {"specivo"}
    (provider,) = ctx.providers
    assert isinstance(provider, SpecivoMemoryProvider) and provider.name == "specivo"


def test_without_memory_support_nothing_changes() -> None:
    """A Hermes without memory providers (or a context without the call) loads the plugin as before."""
    ctx = FakeCtx({"url": BASE_URL})
    register(ctx)
    assert sorted(ctx.hooks) == sorted(HOOKS) and not is_memory_loader(ctx)


def test_memory_loader_gets_only_the_provider_and_a_new_instance_each_time() -> None:
    first, second = MemoryLoaderCtx(), MemoryLoaderCtx()
    assert is_memory_loader(first)
    register(first)
    register(second)
    assert first.other == [] and len(first.providers) == 1
    assert first.providers[0] is not second.providers[0]


def test_an_offered_provider_does_no_work_until_selected() -> None:
    """Registering only offers the provider; Hermes calls it only when ``memory.provider: specivo``."""
    ctx = MemoryCtx({"url": BASE_URL})
    register(ctx)
    assert ctx.mcp.calls == [] and ctx.state.writes == 0


def test_hermes_discovers_the_directory_as_both_kinds() -> None:
    """``kind: standalone`` keeps the plugin manager loading it; the entry module names the memory
    provider contract, which is what Hermes's memory discovery looks for."""
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["kind"] == "standalone"
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")[:8192]
    assert "register_memory_provider" in source and "MemoryProvider" in source
