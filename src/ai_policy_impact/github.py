"""Read-only GitHub client with bounded retries and an immutable request journal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_VERSION = "2026-03-10"
# Our own backoff stays short; a wait GitHub asks for (secondary rate limits send
# retry-after: 60) is honoured up to this long.
MAX_BACKOFF_SECONDS = 30
MAX_RETRY_AFTER_SECONDS = 300
# The hourly REST budget resets within the hour; wait for it rather than fail.
MAX_RATE_LIMIT_WAIT_SECONDS = 3700


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def github_token() -> str | None:
    """Read existing credentials in memory; never print or persist them."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class APIError(RuntimeError):
    def __init__(self, status: int, message: str, raw_file: str | None = None):
        super().__init__(message)
        self.status = status
        self.raw_file = raw_file


@dataclass
class Response:
    data: object
    retrieved_at: str
    raw_file: str


class GitHubClient:
    def __init__(self, raw_dir: Path, token: str | None):
        self.raw_dir = raw_dir
        if self.raw_dir.exists() and any(self.raw_dir.iterdir()):
            raise ValueError(f"Request directory must be new or empty: {self.raw_dir}")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.sequence = 0

    def request(
        self, path: str, *, params: dict | None = None, payload: dict | None = None
    ) -> Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API paths must be relative to api.github.com")
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "ai-policy-impact/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            stamp = utc_now()
            response_headers = {}
            try:
                with urlopen(
                    Request(url, data=body, headers=headers), timeout=45
                ) as response:
                    status = response.status
                    response_headers = dict(response.headers.items())
                    content = response.read()
            except HTTPError as error:
                status = error.code
                response_headers = dict(error.headers.items())
                content = error.read()
            # HTTPException covers a connection dropped mid-body (IncompleteRead).
            except (URLError, TimeoutError, OSError, HTTPException):
                status = 0
                content = b'{"message":"Network request failed"}'
            self.sequence += 1
            name = f"{self.sequence:06d}.json"
            (self.raw_dir / name).write_bytes(content)
            kept_headers = {
                k.lower(): v
                for k, v in response_headers.items()
                if k.lower()
                in {
                    "etag",
                    "link",
                    "x-github-request-id",
                    "retry-after",
                    "x-ratelimit-remaining",
                    "x-ratelimit-reset",
                }
            }
            journal = {
                "retrieved_at": stamp,
                "method": "GET" if body is None else "POST",
                "url": url,
                "payload": payload,
                "api_version": API_VERSION,
                "status": status,
                "headers": kept_headers,
                "file": name,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            with (self.raw_dir / "requests.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(journal, ensure_ascii=False) + "\n")
            try:
                data = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                if 200 <= status < 300:
                    raise APIError(
                        status, "Non-JSON API response", str(self.raw_dir / name)
                    ) from None
                data = {"message": "Non-JSON API response"}
            raw_file = str(self.raw_dir / name)
            if 200 <= status < 300:
                return Response(data, stamp, raw_file)
            retry = status in {0, 429, 500, 502, 503, 504} or (
                status == 403 and "retry-after" in kept_headers
            )
            delay = 2**attempt
            cap = MAX_BACKOFF_SECONDS
            try:
                requested = int(kept_headers.get("retry-after", "0"))
            except ValueError:
                retry = False
                requested = 0
            if requested:
                delay, cap = max(delay, requested), MAX_RETRY_AFTER_SECONDS
            if (
                status in {403, 429}
                and kept_headers.get("x-ratelimit-remaining") == "0"
            ):
                try:
                    reset = int(kept_headers["x-ratelimit-reset"])
                except (KeyError, ValueError):
                    reset = None
                if reset is not None:
                    retry = True
                    delay = max(delay, reset - time.time() + 5)
                    cap = MAX_RATE_LIMIT_WAIT_SECONDS
            if retry and attempt < 2 and delay <= cap:
                time.sleep(delay)
                continue
            message = (
                data.get("message", "GitHub request failed")
                if isinstance(data, dict)
                else "GitHub request failed"
            )
            raise APIError(status, f"HTTP {status}: {message}", raw_file)
        raise AssertionError("unreachable")

    def get(self, path: str, /, **params: object) -> Response:
        return self.request(path, params=params or None)

    def graphql(self, query: str, variables: dict) -> dict:
        response = self.request(
            "/graphql", payload={"query": query, "variables": variables}
        )
        if response.data.get("errors"):
            # HTTP 200 is not proof of a successful GraphQL query.
            errors = response.data["errors"]
            raise APIError(
                200, "; ".join(e["message"] for e in errors), response.raw_file
            )
        return response.data["data"]
