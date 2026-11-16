"""Opt-in trusted probes through Codex's own sandbox, never project test execution.

Run directly with Agent Center's development Python. Creates only temporary sentinels.
Does not load models, credentials, user project files, or modify CLI configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Codex unavailable")
    records = []
    with tempfile.TemporaryDirectory(prefix="agent-center-native-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "sensitive-sentinel"
        outside.write_text("NONSECRET-SENTINEL")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        # Every command runs inside the CLI-owned Seatbelt profile. No custom OS sandbox.
        probes = {
            "workspace_read": "from pathlib import Path; print(Path('.').resolve())",
            "workspace_write": "from pathlib import Path; Path('probe').write_text('ok')",
            "outside_write": f"from pathlib import Path; Path({str(outside)!r}).write_text('changed')",
            "outside_read": f"from pathlib import Path; print(Path({str(outside)!r}).read_text())",
            "loopback_network": (
                "import socket; s=socket.socket(); s.settimeout(1); "
                f"s.connect(('127.0.0.1',{port})); print('connected')"
            ),
        }
        for profile in (":read-only", ":workspace", "ac-restricted"):
            for name, source in probes.items():
                args = [codex, "sandbox", "-P", profile, "-C", str(workspace)]
                if profile == "ac-restricted":
                    runtime = Path(sys.base_prefix).resolve()
                    table = (
                        '{":root"="deny", ":minimal"="read", ":workspace_roots"={"."="read"}, '
                        + json.dumps(str(runtime))
                        + '="read"}'
                    )
                    args.extend(
                        [
                            "-c",
                            "permissions.ac-restricted.filesystem=" + table,
                            "-c",
                            "permissions.ac-restricted.network.enabled=false",
                        ]
                    )
                args.extend(["--", str(Path(sys.executable).resolve()), "-B", "-c", source])
                result = subprocess.run(
                    args,
                    cwd=workspace,
                    env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                records.append(
                    {
                        "profile": profile,
                        "probe": name,
                        "returncode": result.returncode,
                        "stdout": result.stdout.strip(),
                        "stderr": result.stderr.strip()[-2000:],
                    }
                )
                if (workspace / "probe").exists():
                    (workspace / "probe").unlink()
        listener.close()
        records.append({"outside_sentinel_unchanged": outside.read_text() == "NONSECRET-SENTINEL"})
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
