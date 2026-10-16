"""Run the single-agent desk.

The program lives in ``single_agent/voice_agent.py``. This file keeps the old
command working: ``python3 voice_agent.py --demo``.
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SINGLE = _ROOT / "single_agent"
sys.path.insert(0, str(_SINGLE))

_spec = importlib.util.spec_from_file_location("single_voice_agent", _SINGLE / "voice_agent.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

if __name__ == "__main__":
    _module.main()
