import os
import sys
from pathlib import Path

# Keep every test's data (config, keystore, state) in a throwaway directory
os.environ["LP_HEDGER_DATA"] = str(Path(__file__).parent / ".tmpdata")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shutil  # noqa: E402

shutil.rmtree(os.environ["LP_HEDGER_DATA"], ignore_errors=True)
