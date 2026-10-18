from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "cli"


def platform_tags() -> tuple[str, str]:
    system = platform.system()
    try:
        os_tag = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[system]
    except KeyError as error:
        raise ValueError(f"Unsupported operating system: {system}") from error

    machine = platform.machine()
    normalized_machine = machine.lower()
    if normalized_machine in {"x86_64", "amd64"}:
        arch_tag = "x86_64"
    elif normalized_machine in {"arm64", "aarch64"}:
        arch_tag = "arm64"
    else:
        raise ValueError(f"Unsupported architecture: {machine}")
    return os_tag, arch_tag


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_manifest(entry: dict[str, str]) -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    manifest_path = DIST / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"binaries": []}
    binaries = [
        existing
        for existing in manifest.get("binaries", [])
        if (existing.get("os"), existing.get("arch")) != (entry["os"], entry["arch"])
    ]
    binaries.append(entry)
    manifest_path.write_text(json.dumps({"binaries": binaries}, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    os_tag, arch_tag = platform_tags()
    version = project_version()
    build_name = f"harness-{version}-{os_tag}-{arch_tag}"
    environment = os.environ.copy()
    environment["HARNESS_CLI_BUILD_NAME"] = build_name
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--distpath",
            "dist/cli",
            "--workpath",
            "build/cli",
            "--noconfirm",
            "scripts/harness_cli.spec",
        ],
        check=True,
        cwd=ROOT,
        env=environment,
    )
    filename = f"{build_name}.exe" if os_tag == "windows" else build_name
    binary_path = DIST / filename
    update_manifest({
        "os": os_tag,
        "arch": arch_tag,
        "version": version,
        "filename": filename,
        "sha256": sha256(binary_path),
        "built_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
