from __future__ import annotations

import json
import os
import urllib.request


def main() -> None:
    port = int(os.environ.get("AXMS_MCP_PORT", "8091"))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health/ready",
        headers={"Host": f"localhost:{port}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.load(response)
    if response.status != 200 or payload.get("status") != "READY":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
