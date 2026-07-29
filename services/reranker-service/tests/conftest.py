import sys
from pathlib import Path

# No package install step exists yet for this service -- add the service root
# so `import app...` resolves the same way it does when the app itself runs
# (same pattern as the other services' tests/conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
