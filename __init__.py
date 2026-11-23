"""Specivo plugin for Hermes Agent: the entry point Hermes loads (``plugin.yaml`` sits next to it).

``register`` also offers an optional memory provider through ``ctx.register_memory_provider``
(``hermes_specivo.memory.SpecivoMemoryProvider``, a Hermes ``MemoryProvider``), active only when
``memory.provider: specivo`` selects it. This docstring is also how Hermes's memory discovery
recognises the directory; ``kind: standalone`` in ``plugin.yaml`` keeps the plugin manager loading
the hooks and commands as before.
"""

if __package__:
    # Hermes imports the plugin directory as a package.
    from .hermes_specivo.plugin import register
else:  # pragma: no cover - pytest imports the repository root as a plain module while collecting tests
    from hermes_specivo.plugin import register

__all__ = ["register"]
