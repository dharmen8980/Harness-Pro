"""Deterministic JSON serialization shared by compiler and discovery CLIs."""
import json


def json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"
