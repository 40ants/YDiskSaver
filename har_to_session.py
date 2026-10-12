#!/usr/bin/env python3
"""Convert a Chrome HAR to a local cookie session without printing secrets."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from ydisk_auth import DEFAULT_STATE


def parse_cookie_header(value: str):
    for part in value.split(";"):
        name, separator, cookie_value = part.partition("=")
        if separator and name.strip():
            yield name.strip(), cookie_value.strip()


def convert_har(document: dict) -> dict:
    cookies = {}
    for entry in document.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        parsed = urlsplit(request.get("url", ""))
        if parsed.scheme != "https" or parsed.hostname != "disk.yandex.ru":
            continue
        headers = request.get("headers", [])
        pairs = [
            pair
            for header in headers
            if header.get("name", "").lower() == "cookie"
            for pair in parse_cookie_header(header.get("value", ""))
        ]
        if not pairs:
            pairs = [
                (item["name"], item["value"])
                for item in request.get("cookies", [])
                if item.get("name") and isinstance(item.get("value"), str)
            ]
        for name, value in pairs:
            cookies[name] = {
                "name": name, "value": value,
                "domain": "disk.yandex.ru", "path": "/",
                "expires": -1, "secure": True,
            }
    return {"cookies": list(cookies.values()), "origins": []}


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    previous_umask = os.umask(0o077)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".session-", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        os.umask(previous_umask)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path, help="HAR из Chrome с конфиденциальными данными")
    parser.add_argument("session", type=Path, nargs="?", default=DEFAULT_STATE)
    args = parser.parse_args()
    source = args.har.expanduser().resolve()
    target = args.session.expanduser().resolve()
    if source == target:
        parser.error("HAR и файл сессии должны различаться")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        state = convert_har(document)
        if not state["cookies"]:
            raise ValueError("В HAR нет cookies запросов к disk.yandex.ru")
        write_private_json(target, state)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Не удалось создать сессию: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Сессия сохранена: {target}; cookies: {len(state['cookies'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
