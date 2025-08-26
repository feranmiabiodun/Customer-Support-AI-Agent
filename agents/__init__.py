# agents/__init__.py
"""
Package exports for the agents package.
"""
from .ui_agent import CoreAgent 
from . import compat_ui_shim  

__all__ = ["CoreAgent", "compat_ui_shim"]
