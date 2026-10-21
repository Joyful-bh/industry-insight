"""Load the bundled formal renderer without relying on a global scripts package."""
import importlib.util
import sys
from pathlib import Path

runtime = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(runtime / "src"))
spec = importlib.util.spec_from_file_location(
    "track_skill_formal_renderer", runtime / "scripts" / "build_smb_dashboard.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
dashboard_payload = module.dashboard_payload
load_evidence = module.load_evidence
render = module.render
