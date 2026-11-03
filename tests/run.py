import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    failures = []
    with tempfile.TemporaryDirectory(prefix="ownmax-tests-") as temporary:
        state = Path(temporary)
        (state / "call-secrets.json").write_text(
            json.dumps({"signing": "isolated-test-key", "turn": "isolated-turn-key"})
        )
        environment = {
            **os.environ,
            "OWNMAX_DATA_DIR": str(state),
            "db_type": "sqlite",
            "db_file": str(state / "ownmax.db"),
            "OWNMAX_CALL_PHONES": "",
            "OWNMAX_TEST_PHONES": "",
        }
        commands = [("unit", ["-m", "unittest", "discover", "-s", "tests/unit", "-v"])]
        commands.extend(
            (path.name, [str(path)]) for path in sorted((root / "tests/isolated").glob("test_*.py"))
        )
        for name, arguments in commands:
            try:
                result = subprocess.run(
                    [sys.executable, *arguments], cwd=root, env=environment, timeout=100
                )
                if result.returncode:
                    failures.append(name)
            except subprocess.TimeoutExpired:
                failures.append(name)
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("PASS unit suite and 8 isolated scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
