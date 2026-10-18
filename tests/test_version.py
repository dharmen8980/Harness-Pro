import json
from pathlib import Path
import tomllib

from builder.api import _app_version


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def test_frontend_package_version_matches_project_version():
    package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    assert package["version"] == project_version()


def test_api_version_matches_project_version():
    assert _app_version() == project_version()
