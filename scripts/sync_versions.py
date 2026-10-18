"""Synchronize package versions from the Python project metadata."""
import json
from pathlib import Path
import tomllib


root = Path(__file__).resolve().parents[1]
with (root / "pyproject.toml").open("rb") as source:
    version = tomllib.load(source)["project"]["version"]

target = root / "frontend/package.json"
package = json.loads(target.read_text(encoding="utf-8"))
package["version"] = version
target.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
print(target)
