# Compatibility shim: delegate to the implementation in skill/owner-inference/scripts/infer_owner.py
import importlib.util
import sys
from pathlib import Path

orig = Path(__file__).resolve().parents[3] / 'skill' / 'owner-inference' / 'scripts' / 'infer_owner.py'
if not orig.exists():
    raise ImportError(f"Original infer_owner.py not found at {orig}")

spec = importlib.util.spec_from_file_location(__name__ + "_impl", str(orig))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Copy public attributes into this module
for k, v in module.__dict__.items():
    if not k.startswith("__"):
        globals()[k] = v
