# agents/__init__.py
"""
Package exports for the agents package.
"""
from .generic_ui_agent import GenericUIAgent  # core engine
from . import ui_agent  # thin shim

__all__ = ["GenericUIAgent", "ui_agent"]
