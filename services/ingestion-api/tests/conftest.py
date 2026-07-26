import sys
from pathlib import Path

# No package install step exists yet for this service (issue #60 tracks
# proper test infra/packaging) -- add the service root so `import app...`
# resolves the same way it does when the app itself runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
