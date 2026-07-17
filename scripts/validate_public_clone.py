#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounty_hunter.isolation import (
    clone_command,
    detect_runtime,
    resolve_image_reference,
)


def main() -> int:
    runtime = detect_runtime()
    if not runtime:
        raise RuntimeError("Docker or Podman is unavailable")
    root = Path("data/attempts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        image = resolve_image_reference(runtime, "alpine/git:2.47.2")
        command = clone_command(
            runtime,
            "https://github.com/octocat/Hello-World.git",
            temporary,
            image_reference=image,
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env={"PATH": __import__("os").environ.get("PATH", "")},
        )
        source = Path(temporary) / "source"
        outcome = {
            "runtime": runtime,
            "image": image,
            "returncode": result.returncode,
            "stderr": result.stderr,
            "repository_exists": (source / ".git").exists(),
            "readme_exists": any(source.glob("README*")),
        }
        Path("reports/clone-validation.json").write_text(
            json.dumps(outcome, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(outcome, indent=2))
        return 0 if result.returncode == 0 and outcome["repository_exists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
