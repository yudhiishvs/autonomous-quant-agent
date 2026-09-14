"""Wait for fixed loopback development endpoints without logging response bodies."""

from __future__ import annotations

import sys
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ENDPOINTS = {
    "identity": "http://127.0.0.1:8188/realms/paper/.well-known/openid-configuration",
    "api": "http://127.0.0.1:8018/health/ready",
    "ui": "http://127.0.0.1:5178/",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ENDPOINTS:
        raise SystemExit("Usage: python apps/public/wait_local.py identity|api|ui")
    url = ENDPOINTS[sys.argv[1]]
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200 and response.url == url:
                    print(sys.argv[1] + " loopback endpoint ready.")
                    return 0
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(1)
    print(sys.argv[1] + " loopback endpoint did not become ready.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
