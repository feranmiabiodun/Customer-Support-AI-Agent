# test_smoke.py
import importlib
import traceback

mods = [
    "agents",
    "agents.ui_agent",
    "agents.compat_ui_shim",
    "adapters",
    "parser",
]

for m in mods:
    try:
        mod = importlib.import_module(m)
        public = [name for name in dir(mod) if not name.startswith("_")]
        print(m, "OK — public attrs (sample):", public[:10])
    except Exception:
        print(m, "ERROR — traceback below:")
        traceback.print_exc()
        print("-" * 60)
