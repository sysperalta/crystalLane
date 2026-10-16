"""Container HEALTHCHECK: exit 0 when the local /healthz answers 200."""

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "8080")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
