"""ergon: a task tracker where humans approve and agents execute (standard library only)."""
from .cli import main
from .config import CONFIG_TEMPLATE, mini_toml

__all__ = ["main", "mini_toml", "CONFIG_TEMPLATE"]
