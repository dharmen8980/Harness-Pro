"""Stateless HTTP transport for the standalone compiler used by the CLI."""
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .catalog import BASH_PROGRAMS, catalog
from .models import Manifest
from .standalone import compile_standalone


CLI_DIST = Path(__file__).resolve().parents[1] / "dist/cli"


def _app_version() -> str:
    try:
        return version("harness-pro")
    except PackageNotFoundError:
        return "0.0.0-dev"


app = FastAPI(title="Harness Pro", version=_app_version())


@app.exception_handler(RequestValidationError)
async def invalid_manifest(_request: Request, error: RequestValidationError):
    # Do not echo submitted values (especially rejected credentials).
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in error.errors()]})


@app.get("/api/capabilities")
def capabilities():
    reviewer = Manifest.model_validate({
        "name": "standalone-reviewer", "display_name": "Reviewer",
        "model": "claude-sonnet-4-5-20250929",
    })
    fixer = Manifest.model_validate({"name": "standalone-fixer", "display_name": "Fixer", "description": "Edit source and run a fixed test command.",
        "model": "claude-sonnet-4-5-20250929",
        "capabilities": ["repo_read", "file_edit", "run_tests"],
        "mounts": [{"host_path": "/absolute/path/to/project/src", "target": "repo", "read_only": False}],
        "test_command": {"argv": ["python3", "-m", "unittest", "discover"], "working_directory": "repo"}})
    for preset in (reviewer, fixer):
        compile_standalone(preset)
    return {"version": 1, "app_version": _app_version(), "bash_programs": list(BASH_PROGRAMS),
            "capabilities": catalog(),
            "default_manifest": reviewer.model_dump(mode="json"),
            "presets": [{"label": "Reviewer", "manifest": reviewer.model_dump(mode="json")},
                        {"label": "Fixer", "manifest": fixer.model_dump(mode="json")}],
            "manifest_schema": Manifest.model_json_schema()}


@app.post("/api/compile")
def compile_manifest(manifest: Manifest):
    return compile_standalone(manifest)


@app.get("/api/cli-binaries")
def cli_binaries():
    manifest_path = CLI_DIST / "manifest.json"
    if not manifest_path.is_file():
        return {"binaries": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cli_dist = CLI_DIST.resolve()
    binaries = []
    for binary in manifest.get("binaries", []):
        if not isinstance(binary, dict) or not isinstance(binary.get("filename"), str):
            continue
        binary_path = (cli_dist / binary["filename"]).resolve()
        if binary_path.is_relative_to(cli_dist) and binary_path.is_file():
            binaries.append(binary)
    return {"binaries": binaries}


@app.get("/dl/cli/{filename}")
def download_cli_binary(filename: str):
    cli_dist = CLI_DIST.resolve()
    binary_path = (cli_dist / filename).resolve()
    if not binary_path.is_relative_to(cli_dist) or not binary_path.is_file():
        raise HTTPException(status_code=404, detail="No such CLI binary")
    return FileResponse(binary_path, filename=filename, media_type="application/octet-stream")


# Optional production UI; API compilation never accesses user-supplied paths.
frontend = Path(__file__).resolve().parents[1] / "frontend/dist"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
