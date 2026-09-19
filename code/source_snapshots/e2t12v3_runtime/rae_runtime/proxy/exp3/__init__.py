"""Experiment 3 manager-star contracts and offline routing primitives.

The package deliberately has no model, network, MCP, or GitHub imports.  The
legacy single-agent runtime does not need to import it unless an explicit
``architecture_mode`` asks for the manager-star path.
"""

from .architecture import MANAGER_STAR, SINGLE_AGENT, resolve_architecture_mode

__all__ = ["MANAGER_STAR", "SINGLE_AGENT", "resolve_architecture_mode"]
