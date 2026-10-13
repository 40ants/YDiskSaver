#!/usr/bin/env python3
"""Copy a private Yandex Disk folder through the web UI's models-v2 API.

The browser is not started. A previously saved local session JSON file
supplies cookies. Existing local files are skipped without requesting a download URL.
If the original cannot be downloaded, image previews are saved as preview-*.jpg
(or with another extension matching their response type). A preview does not
count as a successful copy of the original.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ydisk_auth import DEFAULT_STATE, ScriptParser, cookie_jar, find_config
from ydisk_fs import ensure_directory, prepare_root, safe_name, save_stream


API = "https://disk.yandex.ru/models-v2"
PREVIEW_TIERS = ("XXXL", "L", "S", "DEFAULT")
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}


def normalize_remote(value: str) -> str:
    """Accept a Disk path, disk: path, or the private web UI folder URL."""
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname != "disk.yandex.ru" or parsed.query or parsed.fragment:
            raise ValueError("Ожидается адрес папки disk.yandex.ru/client/disk/...")
        if not (parsed.path == "/client/disk" or parsed.path.startswith("/client/disk/")):
            raise ValueError("Ожидается адрес папки disk.yandex.ru/client/disk/...")
        value = unquote(parsed.path.removeprefix("/client"))
    elif value.startswith("disk:"):
        value = "/disk" + value.removeprefix("disk:")
    value = value.rstrip("/")
    if value != "/disk" and not value.startswith("/disk/"):
        raise ValueError("Путь должен иметь вид /disk/папка")
    if any(part in ("", ".", "..") for part in value.split("/")[2:]):
        raise ValueError("Некорректный путь на Диске")
    return value


def error_label(error: BaseException) -> str:
    # Never include exception messages: urllib may put signed URLs in them.
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, TimeoutError):
        return "таймаут"
    return type(error).__name__


def print_debug_error(error: BaseException, context: str, enabled: bool) -> None:
    if not enabled:
        return
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    # Download links are signed; keep them out of logs that may be shared.
    details = re.sub(r"(?:https?:)?//[^\s'\"<>]+", "<URL скрыт>", details)
    details = re.sub(r"(?im)\b(?:cookie|authorization)\s*[:=]\s*[^\n]+", "<данные входа скрыты>", details)
    details = re.sub(r"(?i)\b(sk|connection_id)\s*[:=]\s*[^\s,;}]+", r"\1=<скрыто>", details)
    print(f"[debug] {context}\n{details.rstrip()}", file=sys.stderr)


def normalize_download_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "downloader.disk.yandex.ru"
        or re.fullmatch(r"[0-9]+\.downloader\.disk\.yandex\.ru", host)
    ):
        raise ValueError("Неожиданный адрес скачивания")
    return url


def preview_urls(item: dict):
    meta = item.get("meta") or {}
    sizes = meta.get("sizes") if isinstance(meta, dict) else None
    if not isinstance(sizes, list):
        return
    by_name = {
        entry.get("name"): entry.get("url")
        for entry in sizes if isinstance(entry, dict)
    }
    for tier in PREVIEW_TIERS:
        url = by_name.get(tier)
        if isinstance(url, str):
            try:
                yield normalize_download_url(url)
            except ValueError:
                continue


def existing_preview(local: Path, name: str) -> Path | None:
    stem = Path(name).stem
    for extension in IMAGE_EXTENSIONS.values():
        candidate = local / f"preview-{stem}{extension}"
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def print_missing_session(path: Path) -> None:
    print(f"Не найден файл сессии Яндекс Диска: {path}", file=sys.stderr)
    print("Как его создать:", file=sys.stderr)
    print("  1. Войдите в Яндекс Диск в Chrome и откройте DevTools → Network.", file=sys.stderr)
    print("  2. Перейдите в папку Диска и выберите «Copy all listed as HAR (with sensitive data)».", file=sys.stderr)
    print("  3. Сохраните скопированный HAR в файл, затем выполните:", file=sys.stderr)
    if path == DEFAULT_STATE.expanduser():
        print('     python3 har_to_session.py "$HOME/Downloads/ydisk-session.har"', file=sys.stderr)
    else:
        print('     python3 har_to_session.py "$HOME/Downloads/ydisk-session.har" "' + str(path) + '"', file=sys.stderr)
    print("Подробности: README.md, раздел «Как получить данные для входа».", file=sys.stderr)


class DiskWeb:
    def __init__(self, state: Path, remote: str, timeout: float, debug: bool = False):
        self.remote = remote
        self.timeout = timeout
        self.debug = debug
        self.opener = build_opener(HTTPCookieProcessor(cookie_jar(state)))
        self.page = "https://disk.yandex.ru/client" + quote(remote, safe="/")
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": self.page,
            "Origin": "https://disk.yandex.ru",
        }
        self.sk = None
        self.connection_id = None
        self.refresh_session()

    def refresh_session(self) -> None:
        with self.opener.open(Request(self.page, headers=self.headers), timeout=self.timeout) as response:
            if urlsplit(response.url).hostname != "disk.yandex.ru":
                raise RuntimeError("Сессия перенаправлена на страницу входа")
            html = response.read().decode("utf-8", "replace")
        parser = ScriptParser()
        parser.feed(html)
        if not parser.parts:
            raise RuntimeError("В странице нет preloaded-data; сессия могла истечь")
        config = find_config(json.loads("".join(parser.parts)))
        if not config:
            raise RuntimeError("Не найдены параметры веб-сессии; сохраните cookies заново")
        self.sk = config["sk"]
        self.connection_id = config["idClient"]

    def call(self, method: str, params: dict) -> dict:
        for attempt in range(2):
            body = json.dumps({
                "sk": self.sk,
                "connection_id": self.connection_id,
                "apiMethod": method,
                "requestParams": params,
            }).encode("utf-8")
            request = Request(
                f"{API}?m={method}", data=body,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    result = json.load(response)
            except HTTPError as error:
                if error.code == 403 and attempt == 0:
                    try:
                        payload = json.load(error)
                    except (ValueError, OSError):
                        raise error
                    details = payload.get("error") if isinstance(payload, dict) else None
                    new_sk = details.get("ckey") if isinstance(details, dict) else None
                    if isinstance(new_sk, str) and new_sk:
                        self.sk = new_sk
                        continue
                raise
            if not isinstance(result, dict) or "error" in result:
                raise RuntimeError("Веб-API вернул ошибку")
            return result
        raise RuntimeError("Не удалось обновить веб-сессию")

    def list_children(self, directory: str):
        offset = 0
        seen = set()
        while True:
            params = {
                "sort": "name", "order": "1", "idContext": directory,
                "amount": 40, "offset": offset,
                "withParent": "1", "with_share": "1",
            }
            for attempt in range(2):
                try:
                    result = self.call("mpfs/resources", params)
                    break
                except (HTTPError, URLError, TimeoutError) as error:
                    if attempt == 1:
                        raise
                    print_debug_error(error, f"повтор запроса списка {directory}", self.debug)
                    time.sleep(1)
            resources = result.get("resources")
            if not isinstance(resources, list):
                raise RuntimeError("Неожиданный ответ mpfs/resources")
            found = 0
            for item in resources:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                if not isinstance(path, str) or path == directory:
                    continue
                prefix = directory.rstrip("/") + "/"
                if not path.startswith(prefix) or "/" in path[len(prefix):]:
                    continue
                if path in seen:
                    continue
                seen.add(path)
                found += 1
                yield item
            if found == 0:
                break
            offset += 40

    def download_url(self, path: str) -> str:
        result = self.call("mpfs/url", {"path": path})
        url = result.get("file")
        if not isinstance(url, str):
            raise RuntimeError("Неожиданный адрес в ответе mpfs/url")
        return normalize_download_url(url)

    def open_download(self, url: str):
        return self.opener.open(
            Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": self.page}),
            timeout=self.timeout,
        )


def copy_tree(client: DiskWeb, remote: str, local: Path, root: Path,
              retries: int, file_timeout: float, preview_timeout: float,
              counts: Counter, debug: bool = False) -> None:
    ensure_directory(local, root)
    try:
        children = list(client.list_children(remote))
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        counts["errors"] += 1
        print(f"Ошибка списка {remote}: {error_label(error)}", file=sys.stderr)
        print_debug_error(error, f"список {remote}", debug)
        return

    for item in children:
        try:
            name = safe_name(item.get("name"))
            path = item["path"]
            kind = item.get("type")
            target = local / name
            if kind == "dir":
                counts["directories"] += 1
                copy_tree(client, path, target, root, retries, file_timeout,
                          preview_timeout, counts, debug)
                continue
            if kind != "file":
                raise ValueError("Неизвестный тип ресурса")
            if target.is_dir() and not target.is_symlink():
                raise ValueError("На месте файла уже находится папка")
            if target.exists() or target.is_symlink():
                counts["skipped"] += 1
                print(f"Пропуск: {target}")
                continue
            meta = item.get("meta", {})
            expected_size = meta.get("size") if isinstance(meta, dict) else None
            if not isinstance(expected_size, int) or expected_size < 0:
                expected_size = None
            original_error = None
            for attempt in range(retries):
                deadline = time.monotonic() + file_timeout
                try:
                    url = client.download_url(path)
                    with client.open_download(url) as response:
                        status = save_stream(response, target, root, expected_size, deadline)
                    counts[status] += 1
                    print(f"{status}: {target}")
                    break
                except (HTTPError, URLError, OSError, ValueError, RuntimeError, TimeoutError) as error:
                    original_error = error
                    print_debug_error(error, f"оригинал {path}, попытка {attempt + 1}/{retries}", debug)
                    if attempt + 1 < retries:
                        time.sleep(1)
            else:
                counts["errors"] += 1
                print(f"Оригинал недоступен {path}: {error_label(original_error)}", file=sys.stderr)
                preview = existing_preview(local, name)
                if preview:
                    counts["preview_skipped"] += 1
                    print(f"Превью уже есть: {preview}")
                    continue
                candidates = list(preview_urls(item))
                if not candidates and debug:
                    print(f"[debug] Для {path} в метаданных нет URL превью.", file=sys.stderr)
                for preview_number, url in enumerate(candidates, 1):
                    try:
                        deadline = time.monotonic() + preview_timeout
                        with client.open_download(url) as response:
                            mime = response.headers.get_content_type()
                            extension = IMAGE_EXTENSIONS.get(mime)
                            if not extension:
                                raise ValueError("Ответ превью не является поддерживаемым изображением")
                            preview = local / f"preview-{Path(name).stem}{extension}"
                            length = response.headers.get("Content-Length")
                            expected = int(length) if length and length.isdigit() else None
                            status = save_stream(response, preview, root, expected, deadline)
                        counts["previewed" if status == "downloaded" else "preview_skipped"] += 1
                        print(f"Превью: {preview}")
                        break
                    except (HTTPError, URLError, OSError, ValueError, RuntimeError, TimeoutError) as error:
                        print_debug_error(error, f"превью {path}, вариант {preview_number}", debug)
                        continue
                else:
                    print(f"Превью тоже недоступно: {path}", file=sys.stderr)
        except (KeyError, ValueError, OSError) as error:
            counts["errors"] += 1
            print(f"Ошибка элемента в {remote}: {error_label(error)}", file=sys.stderr)
            print_debug_error(error, f"элемент папки {remote}", debug)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Пример: python3 ydisk_saver.py /disk/crons ~/tmp/disk",
    )
    parser.add_argument("remote_path", help="Папка на Диске, например /disk/crons")
    parser.add_argument("local_dir", type=Path, help="Локальная папка для её содержимого")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--request-timeout", type=float, default=8.0)
    parser.add_argument("--file-timeout", type=float, default=30.0)
    parser.add_argument("--preview-timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--debug", action="store_true", help="Печатать traceback для каждой неудачной попытки")
    args = parser.parse_args()
    try:
        remote = normalize_remote(args.remote_path)
    except ValueError as error:
        parser.error(str(error))
    if (args.request_timeout <= 0 or args.file_timeout <= 0
            or args.preview_timeout <= 0 or args.retries < 1):
        parser.error("Таймауты и число попыток должны быть положительными")
    state_path = args.state.expanduser()
    if not state_path.is_file():
        print_missing_session(state_path)
        return 2
    try:
        client = DiskWeb(state_path, remote, args.request_timeout, args.debug)
        root = prepare_root(args.local_dir)
        counts = Counter()
        copy_tree(client, remote, root, root, args.retries, args.file_timeout,
                  args.preview_timeout, counts, args.debug)
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        print(f"Не удалось начать копирование: {error_label(error)}", file=sys.stderr)
        print_debug_error(error, "запуск копирования", args.debug)
        return 1
    print("Итог:", ", ".join(f"{key}={counts[key]}" for key in
                             ("downloaded", "skipped", "previewed",
                              "preview_skipped", "directories", "errors")))
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
