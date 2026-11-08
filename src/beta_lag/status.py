"""Atomic, credential-free live status, readable without opening the writer database."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as file:
        json.dump(value, file, indent=2, allow_nan=False, default=str)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def read_status(output: Path) -> dict:
    if (output / "status.json").exists():
        path = output / "status.json"
    else:
        candidates = list(output.glob("*/status.json"))
        if not candidates:
            raise ValueError("No live status found under the requested directory")
        path = max(candidates, key=lambda value: value.stat().st_mtime)
    result = json.loads(path.read_text())
    age = (datetime.now(UTC) - datetime.fromisoformat(result["updated_at"])).total_seconds()
    result["status_age_seconds"] = round(max(0, age), 1)
    result["heartbeat_stale"] = result["status"] == "running" and age > max(
        90, result.get("status_seconds", 30) * 2
    )
    result["status_file"] = str(path.resolve())
    return result
