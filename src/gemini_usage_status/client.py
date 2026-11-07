"""Minimal client for the (undocumented) Code Assist v1internal API.

Only the daily host is used. The production host answered every quota bucket
as untouched (remainingFraction 1) while the daily host showed real use, so it
is not even a fallback: a wrong figure is worse than an error.

The server refuses quota calls without an Antigravity User-Agent (403), so the
monitor identifies itself exactly as the CLI installed on this machine does.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

HOST = "daily-cloudcode-pa.googleapis.com"
TIMEOUT_SECONDS = 20
RETRY_DELAY_SECONDS = 3
# Worth one more try: the server or the network hiccuped, the request was fine.
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
FALLBACK_VERSION = "1.5.3"

_ARCH = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


def user_agent(version, system, machine):
    arch = _ARCH.get(str(machine).lower(), str(machine).lower())
    return (f"antigravity/cli/{version or FALLBACK_VERSION} "
            f"(aidev_client; os_type={str(system).lower()}; arch={arch})")


def _server_message(error):
    try:
        body = json.loads(error.read().decode("utf-8"))
        return body["error"]["message"]
    except (ValueError, KeyError, TypeError, OSError):
        return error.reason if isinstance(error.reason, str) else "no detail"


class CodeAssistClient:
    def __init__(self, access_token, ua, opener=urllib.request.urlopen, sleep=time.sleep):
        self._token = access_token
        self._ua = ua
        self._open = opener
        self._sleep = sleep

    def post(self, method, payload):
        """POST once, and once more after a short pause if the failure was transient."""
        try:
            return self._post_once(method, payload)
        except ApiError as error:
            if error.status is not None and error.status not in TRANSIENT_STATUS:
                raise
        self._sleep(RETRY_DELAY_SECONDS)
        return self._post_once(method, payload)

    def _post_once(self, method, payload):
        request = urllib.request.Request(
            f"https://{HOST}/v1internal:{method}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": self._ua,
            },
        )
        try:
            with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, _server_message(error)) from error
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(None, f"{method}: network error ({error})") from error
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise ApiError(None, f"{method}: response is not JSON") from error

    def load_code_assist(self):
        return self.post("loadCodeAssist", {
            "metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED",
                         "pluginType": "GEMINI"},
        })
