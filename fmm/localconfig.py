"""Preferences for this machine, kept outside the repository: ~/.config/free-my-mac/local.json.

The repository is public; the user's project, tool and agent names live in this local file.
Format (every key is optional):

    {
      "conda_projects": {"<conda env>": "<folder relative to ~/Projects>"},
      "uv_tools": {"<conda env or pipx venv>": "<source for uv tool install>"},
      "own_agent_prefixes": ["<label prefix of your launchd agents>"]
    }
"""
from __future__ import annotations

import json
from typing import Dict, Tuple

from .core import Context

LOCAL_CONFIG = '~/.config/free-my-mac/local.json'


def local_config(ctx: Context) -> dict:
    path = ctx.user(LOCAL_CONFIG)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str_map(value) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str) and v}


def conda_projects(ctx: Context) -> Dict[str, str]:
    """Conda env -> project folder (relative to ~/Projects) that gets the .venv."""
    return _str_map(local_config(ctx).get('conda_projects'))


def uv_tools(ctx: Context) -> Dict[str, str]:
    """Conda env or pipx venv -> source of the `uv tool install` that replaces it."""
    return _str_map(local_config(ctx).get('uv_tools'))


def own_agent_prefixes(ctx: Context) -> Tuple[str, ...]:
    """Label prefixes of the user's own agents, which stay out of the lists of agents to turn off."""
    value = local_config(ctx).get('own_agent_prefixes')
    return tuple(str(v) for v in value if isinstance(v, str) and v) if isinstance(value, list) else ()
