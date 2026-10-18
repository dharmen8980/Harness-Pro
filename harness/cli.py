"""Console entrypoint for standalone compile and run commands."""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

from pydantic import ValidationError

from builder.models import Manifest
from builder.serialization import json_text
from builder.standalone import compile_standalone

from .errors import PolicyViolation, ProviderError, ToolExecutionError, TurnLimitExceeded, WallClockExceeded
from .loop import run_agent
from .trace import TraceEmitter


EXIT_MALFORMED = 2
EXIT_TURN_LIMIT = 3
EXIT_POLICY = 4
EXIT_PROVIDER = 5
EXIT_TIMEOUT = 6
EXIT_RUNTIME = 7


def _app_version() -> str:
    try:
        return version("harness-pro")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _load(path: Path) -> Manifest:
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def _report_validation(error: ValidationError) -> None:
    for problem in error.errors(include_input=False, include_url=False):
        location = ".".join(map(str, problem["loc"])) or "manifest"
        print(f"{location}: {problem['msg']}", file=sys.stderr)


def compile_command(args: argparse.Namespace) -> int:
    try:
        text = json_text(compile_standalone(_load(args.manifest)))
        if args.output:
            args.output.write_text(text, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(text)
        return 0
    except ValidationError as error:
        _report_validation(error)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
    return EXIT_MALFORMED


def run_command(args: argparse.Namespace) -> int:
    trace: TraceEmitter | None = None
    try:
        manifest = _load(args.manifest)
        config = compile_standalone(manifest)
        trace = TraceEmitter.to_path(args.trace_file) if args.trace_file else TraceEmitter(sys.stdout)
        result = asyncio.run(run_agent(config, args.prompt, trace))
        if args.trace_file:
            print(result.text)
        return 0
    except ValidationError as error:
        _report_validation(error)
        return EXIT_MALFORMED
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_MALFORMED
    except TurnLimitExceeded as error:
        print(str(error), file=sys.stderr)
        return EXIT_TURN_LIMIT
    except PolicyViolation as error:
        print(str(error), file=sys.stderr)
        return EXIT_POLICY
    except ProviderError as error:
        print(str(error), file=sys.stderr)
        return EXIT_PROVIDER
    except WallClockExceeded as error:
        print(str(error), file=sys.stderr)
        return EXIT_TIMEOUT
    except ToolExecutionError as error:
        print(str(error), file=sys.stderr)
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Runtime failed ({type(error).__name__})", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        if trace:
            trace.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="harness", description="Run a manifest-scoped, provider-agnostic coding agent")
    root.add_argument("--version", action="version", version=f"harness {_app_version()}")
    commands = root.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile", help="emit a deterministic standalone runtime config")
    compile_parser.add_argument("--manifest", required=True, type=Path)
    compile_parser.add_argument("-o", "--output", type=Path)
    compile_parser.set_defaults(handler=compile_command)
    run_parser = commands.add_parser("run", help="run one headless agent conversation")
    run_parser.add_argument("--manifest", required=True, type=Path)
    run_parser.add_argument("--prompt", required=True)
    run_parser.add_argument("--trace-file", type=Path)
    run_parser.set_defaults(handler=run_command)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)
