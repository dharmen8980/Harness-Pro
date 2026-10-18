"""Generate the fully expanded manifest type used by the React form."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from builder.models import Manifest


def expanded(value):
    if isinstance(value, dict):
        if "properties" in value:
            value["required"] = list(value["properties"])
        for child in value.values():
            expanded(child)
    elif isinstance(value, list):
        for child in value:
            expanded(child)


schema = Manifest.model_json_schema()
expanded(schema)
target = Path(__file__).resolve().parents[1] / "frontend/manifest.schema.json"
target.write_text(json.dumps(schema, indent=2) + "\n")
print(target)
