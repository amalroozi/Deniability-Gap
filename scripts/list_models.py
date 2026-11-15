"""List the model IDs each API key in .env can use. Prints model IDs only.

Usage: python scripts/list_models.py

Key values are sent only in request headers and are never printed; on an
error only the HTTP status (or the error type) is shown.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from dgap.models import prefer_ipv4

GROQ_URL = "https://api.groq.com/openai/v1/models"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
USER_AGENT = "dgap-list-models/0.1"
MAX_PAGES = 20


def _get(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def groq_models(key: str) -> list[str]:
    data = _get(GROQ_URL, {"Authorization": f"Bearer {key}"})
    return sorted(item["id"] for item in data.get("data", []))


def gemini_models(key: str) -> list[str]:
    ids: list[str] = []
    url = GEMINI_URL
    seen_tokens: set[str] = set()
    for _ in range(MAX_PAGES):
        data = _get(url, {"x-goog-api-key": key})
        for item in data.get("models", []):
            if "generateContent" in item.get("supportedGenerationMethods", []):
                ids.append(item["name"].removeprefix("models/"))
        token = data.get("nextPageToken")
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        url = f"{GEMINI_URL}&pageToken={urllib.parse.quote(token)}"
    return sorted(set(ids))


def main() -> int:
    load_dotenv()
    prefer_ipv4()
    status = 0
    for label, variable, lister in (
        ("groq", "GROQ_API_KEY", groq_models),
        ("gemini", "GEMINI_API_KEY", gemini_models),
    ):
        print(f"[{label}]")
        key = os.environ.get(variable)
        if not key:
            print(f"  {variable} is not set")
            status = 1
            continue
        try:
            ids = lister(key)
        except urllib.error.HTTPError as exc:
            print(f"  request failed: HTTP {exc.code}")
            status = 1
            continue
        except Exception as exc:  # the message is not printed: it could echo the request
            print(f"  request failed: {type(exc).__name__}")
            status = 1
            continue
        for model_id in ids:
            print(f"  {model_id}")
    return status


if __name__ == "__main__":
    sys.exit(main())
