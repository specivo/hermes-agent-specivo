"""Specivo plugin for Hermes Agent: the entry point Hermes loads (``plugin.yaml`` sits next to it)."""

if __package__:
    # Hermes imports the plugin directory as a package.
    from .hermes_specivo.plugin import register
else:  # pragma: no cover - pytest imports the repository root as a plain module while collecting tests
    from hermes_specivo.plugin import register

__all__ = ["register"]
