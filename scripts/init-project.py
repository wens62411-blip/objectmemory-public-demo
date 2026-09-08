import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api.app.main import create_app
app=create_app(runtime_mode="REAL")
runtime=app.state.runtime
try:
    print('SQLite initialized:',runtime.db.path)
    print('REAL mode initialized. No seed items, demo cameras, virtual devices, or events were created.')
    print('Use Start-No-Hardware-Demo.bat only for an explicitly labelled DEMO run.')
finally:
    # This script initializes storage but never enters the FastAPI lifespan.
    runtime.mode_lease.close()
