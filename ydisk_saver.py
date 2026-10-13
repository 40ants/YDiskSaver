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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from threading import Lock, local
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ydisk_auth import DEFAULT_STATE, ScriptParser, cookie_jar, find_config
from ydisk_fs import IncompleteFileError, ensure_directory, prepare_root, safe_name, save_stream


API = "https://disk.yandex.ru/models-v2"
PREVIEW_TIERS = ("XXXL", "L", "S", "DEFAULT")
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}
LOG_LOCK = Lock()


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


def log_message(message: str, *, debug: bool = False, error: bool = False) -> None:
    """Timestamp every log line; keep debug output on one stream."""
    stream = sys.stderr if debug or error else sys.stdout
    with LOG_LOCK:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n".join(f"[{timestamp}] {line}" for line in message.splitlines()),
              file=stream, flush=True)


def print_debug_error(error: BaseException, context: str, enabled: bool) -> None:
    if not enabled:
        return
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    # Download links are signed; keep them out of logs that may be shared.
    details = re.sub(r"(?:https?:)?//[^\s'\"<>]+", "<URL скрыт>", details)
    details = re.sub(r"(?im)\b(?:cookie|authorization)\s*[:=]\s*[^\n]+", "<данные входа скрыты>", details)
    details = re.sub(r"(?i)\b(sk|connection_id)\s*[:=]\s*[^\s,;}]+", r"\1=<скрыто>", details)
    log_message(f"[debug] {context}\n{details.rstrip()}", error=True)


def print_progress(message: str, debug: bool = False) -> None:
    """Keep progress visible and ordered alongside tracebacks in debug mode."""
    log_message(message, debug=debug)


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


def existing_preview(local: Path, name: str, *, regular_only: bool = False) -> Path | None:
    stem = Path(name).stem
    for extension in IMAGE_EXTENSIONS.values():
        candidate = local / f"preview-{stem}{extension}"
        if regular_only:
            if not candidate.is_symlink() and candidate.is_file():
                return candidate
        elif candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def skip_existing(local: Path, name: str, debug: bool, skip_previews: bool) -> bool:
    target = local / name
    if target.is_dir() and not target.is_symlink():
        raise ValueError("На месте файла уже находится папка")
    if target.exists() or target.is_symlink():
        print_progress(f"Пропуск: {target}", debug)
        return True
    if skip_previews:
        preview = existing_preview(local, name, regular_only=True)
        if preview is not None:
            print_progress(f"Пропуск оригинала (есть превью {preview}): {target}", debug)
            return True
    return False


def print_missing_session(path: Path) -> None:
    instructions = [
        f"Не найден файл сессии Яндекс Диска: {path}",
        "Как его создать:",
        "  1. Войдите в Яндекс Диск в Chrome и откройте DevTools → Network.",
        "  2. Перейдите в папку Диска и выберите «Copy all listed as HAR (with sensitive data)».",
        "  3. Сохраните скопированный HAR в файл, затем выполните:",
    ]
    if path == DEFAULT_STATE.expanduser():
        instructions.append('     python3 har_to_session.py "$HOME/Downloads/ydisk-session.har"')
    else:
        instructions.append('     python3 har_to_session.py "$HOME/Downloads/ydisk-session.har" "' + str(path) + '"')
    instructions.append("Подробности: README.md, раздел «Как получить данные для входа».")
    log_message("\n".join(instructions), error=True)


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


def copy_file(client: DiskWeb, item: dict, remote: str, local: Path, root: Path,
              retries: int, file_timeout: float, preview_timeout: float,
              debug: bool, skip_previews: bool = False) -> Counter:
    """Copy one file; return counters so workers never mutate shared totals."""
    counts = Counter()
    try:
        name = safe_name(item.get("name"))
        path = item["path"]
        target = local / name
        if skip_existing(local, name, debug, skip_previews):
            counts["skipped"] += 1
            return counts
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
                    status = save_stream(response, target, root, expected_size, deadline,
                                         save_incomplete=True)
                counts[status] += 1
                print_progress(f"{status}: {target}", debug)
                break
            except (HTTPError, URLError, OSError, ValueError, RuntimeError, TimeoutError) as error:
                original_error = error
                if isinstance(error, IncompleteFileError) and error.saved_path:
                    print_progress(f"Неполный файл сохранён: {error.saved_path}", debug)
                print_debug_error(error, f"оригинал {path}, попытка {attempt + 1}/{retries}", debug)
                if attempt + 1 < retries:
                    time.sleep(1)
        else:
            counts["errors"] += 1
            log_message(f"Оригинал недоступен {path}: {error_label(original_error)}", error=True)
            preview = existing_preview(local, name)
            if preview:
                counts["preview_skipped"] += 1
                print_progress(f"Превью уже есть: {preview}", debug)
                return counts
            candidates = list(preview_urls(item))
            if not candidates and debug:
                log_message(f"[debug] Для {path} в метаданных нет URL превью.", error=True)
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
                    print_progress(f"Превью: {preview}", debug)
                    break
                except (HTTPError, URLError, OSError, ValueError, RuntimeError, TimeoutError) as error:
                    print_debug_error(error, f"превью {path}, вариант {preview_number}", debug)
            else:
                log_message(f"Превью тоже недоступно: {path}", error=True)
    except (KeyError, ValueError, OSError) as error:
        counts["errors"] += 1
        log_message(f"Ошибка элемента в {remote}: {error_label(error)}", error=True)
        print_debug_error(error, f"элемент папки {remote}", debug)
    return counts


class FileQueue:
    """Bounded download queue with an independent web session per worker."""

    def __init__(self, listing_client: DiskWeb, state: Path, remote: str,
                 request_timeout: float, retries: int, file_timeout: float,
                 preview_timeout: float, workers: int, counts: Counter, debug: bool,
                 skip_previews: bool = False):
        self.listing_client = listing_client
        self.state = state
        self.remote = remote
        self.request_timeout = request_timeout
        self.retries = retries
        self.file_timeout = file_timeout
        self.preview_timeout = preview_timeout
        self.workers = workers
        self.counts = counts
        self.debug = debug
        self.skip_previews = skip_previews
        self.thread_state = local()
        self.executor = (ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ydisk")
                         if workers > 1 else None)
        self.pending = {}

    def _worker(self, item: dict, directory: str, local_dir: Path, root: Path) -> Counter:
        client = getattr(self.thread_state, "client", None)
        if client is None:
            client = DiskWeb(self.state, self.remote, self.request_timeout, self.debug)
            self.thread_state.client = client
        return copy_file(client, item, directory, local_dir, root, self.retries,
                         self.file_timeout, self.preview_timeout, self.debug,
                         self.skip_previews)

    def submit(self, item: dict, directory: str, local_dir: Path, root: Path) -> None:
        if skip_existing(local_dir, item["name"], self.debug, self.skip_previews):
            self.counts["skipped"] += 1
            return
        if self.executor is None:
            self.counts.update(copy_file(self.listing_client, item, directory, local_dir,
                                         root, self.retries, self.file_timeout,
                                         self.preview_timeout, self.debug,
                                         self.skip_previews))
            return
        future = self.executor.submit(self._worker, item, directory, local_dir, root)
        self.pending[future] = item.get("path", directory)
        if len(self.pending) >= 2 * self.workers:
            self.collect_one()

    def collect_one(self) -> None:
        done, _ = wait(tuple(self.pending), return_when=FIRST_COMPLETED)
        for future in done:
            path = self.pending.pop(future)
            try:
                self.counts.update(future.result())
            except Exception as error:
                self.counts["errors"] += 1
                log_message(f"Ошибка загрузки {path}: {error_label(error)}", error=True)
                print_debug_error(error, f"загрузка {path}", self.debug)

    def finish(self) -> None:
        while self.pending:
            self.collect_one()

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True)


def copy_tree(client: DiskWeb, remote: str, local: Path, root: Path,
              queue: FileQueue) -> None:
    ensure_directory(local, root)
    pending_directories = [(remote, local)]
    while pending_directories:
        directory, destination = pending_directories.pop()
        try:
            children = list(client.list_children(directory))
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
            queue.counts["errors"] += 1
            log_message(f"Ошибка списка {directory}: {error_label(error)}", error=True)
            print_debug_error(error, f"список {directory}", queue.debug)
            continue

        # Create direct child directories before queuing files, so colliding
        # file/directory names cannot race on case-insensitive filesystems.
        nested = []
        for item in children:
            if item.get("type") != "dir":
                continue
            try:
                name = safe_name(item.get("name"))
                path = item["path"]
                queue.counts["directories"] += 1
                target = destination / name
                ensure_directory(target, root)
                nested.append((path, target))
            except (KeyError, ValueError, OSError, RuntimeError) as error:
                queue.counts["errors"] += 1
                log_message(f"Ошибка элемента в {directory}: {error_label(error)}", error=True)
                print_debug_error(error, f"элемент папки {directory}", queue.debug)

        # Start downloads before querying the next folder. The queue itself
        # pauses traversal only when its bounded backlog is full.
        for item in children:
            if item.get("type") == "dir":
                continue
            try:
                safe_name(item.get("name"))
                if item.get("type") != "file":
                    raise ValueError("Неизвестный тип ресурса")
                queue.submit(item, directory, destination, root)
            except (KeyError, ValueError, OSError, RuntimeError) as error:
                queue.counts["errors"] += 1
                log_message(f"Ошибка элемента в {directory}: {error_label(error)}", error=True)
                print_debug_error(error, f"элемент папки {directory}", queue.debug)

        pending_directories.extend(reversed(nested))


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
    parser.add_argument("--workers", type=int, default=1,
                        help="Число одновременных загрузок файлов (1–8, по умолчанию 1)")
    parser.add_argument("--skip-previews", action="store_true",
                        help="Не скачивать оригинал, если локальное превью уже существует")
    parser.add_argument("--debug", action="store_true", help="Печатать все сообщения и traceback в stderr")
    args = parser.parse_args()
    try:
        remote = normalize_remote(args.remote_path)
    except ValueError as error:
        parser.error(str(error))
    if (args.request_timeout <= 0 or args.file_timeout <= 0
            or args.preview_timeout <= 0 or args.retries < 1):
        parser.error("Таймауты и число попыток должны быть положительными")
    if not 1 <= args.workers <= 8:
        parser.error("Число потоков --workers должно быть от 1 до 8")
    state_path = args.state.expanduser()
    if not state_path.is_file():
        print_missing_session(state_path)
        return 2
    try:
        client = DiskWeb(state_path, remote, args.request_timeout, args.debug)
        root = prepare_root(args.local_dir)
        counts = Counter()
        queue = FileQueue(client, state_path, remote, args.request_timeout, args.retries,
                          args.file_timeout, args.preview_timeout, args.workers, counts,
                          args.debug, args.skip_previews)
        try:
            copy_tree(client, remote, root, root, queue)
            queue.finish()
        finally:
            queue.close()
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        log_message(f"Не удалось начать копирование: {error_label(error)}", error=True)
        print_debug_error(error, "запуск копирования", args.debug)
        return 1
    print_progress("Итог: " + ", ".join(f"{key}={counts[key]}" for key in
                                  ("downloaded", "skipped", "previewed",
                                   "preview_skipped", "directories", "errors")), args.debug)
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
