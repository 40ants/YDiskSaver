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
import os
from pathlib import Path
import re
import signal
import sys
from threading import Event, Lock, local
import time
import traceback
import unicodedata
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
DEFAULT_REMOTE_PATH = "https://disk.yandex.ru/client/disk/"
PHOTO_CLUSTER_PAGE = 200
PHOTO_CLUSTER_BATCH = 10


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

    def call(self, method: str, params: dict | None = None) -> dict:
        for attempt in range(2):
            payload = {
                "sk": self.sk,
                "connection_id": self.connection_id,
                "apiMethod": method,
            }
            if params is not None:
                payload["requestParams"] = params
            body = json.dumps(payload).encode("utf-8")
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

    def photo_snapshot(self) -> str:
        result = self.call("intapi/photo-init-snapshot")
        snapshot = result.get("photoslice_id")
        if not isinstance(snapshot, str) or not snapshot:
            raise RuntimeError("В ответе photo-init-snapshot нет photoslice_id")
        return snapshot

    def photo_clusters(self, snapshot: str):
        offset = 0
        seen = set()
        while True:
            result = self.call("intapi/photo-get-snapshot", {
                "id": snapshot, "locale": "ru",
                "amount": PHOTO_CLUSTER_PAGE, "offset": offset,
            })
            items = result.get("items")
            if not isinstance(items, list):
                raise RuntimeError("Неожиданный список кластеров в photo-get-snapshot")
            new_clusters = 0
            for cluster in items:
                if not isinstance(cluster, dict):
                    raise RuntimeError("Неожиданный кластер в photo-get-snapshot")
                cluster_id = cluster.get("id", cluster.get("cluster_id"))
                size = cluster.get("size")
                if (not isinstance(cluster_id, str) or not cluster_id
                        or not isinstance(size, int) or size < 0):
                    raise RuntimeError("Некорректный ID или размер фото-кластера")
                if cluster_id not in seen:
                    seen.add(cluster_id)
                    new_clusters += 1
                    yield cluster_id, size
            offset += len(items)
            total = result.get("total")
            if not items or (isinstance(total, int) and offset >= total):
                break
            # Some versions of the web API ignore amount/offset and return the
            # entire snapshot in one response. Do not request it indefinitely.
            if not isinstance(total, int) and len(items) > PHOTO_CLUSTER_PAGE:
                break
            if not new_clusters:
                raise RuntimeError("photo-get-snapshot повторяет страницу кластеров")

    def photo_resources(self, snapshot: str, clusters: list[tuple[str, int]]) -> list[dict]:
        requested = {
            cluster_id: {"range": [0, size - 1]}
            for cluster_id, size in clusters if size > 0
        }
        if not requested:
            return []
        result = self.call("intapi/photo-get-clusters-with-resources", {
            "photosliceId": snapshot,
            "clusters": requested,
            "hideScreenshots": False,
            "filter": "photounlim",
        })
        resources = result.get("resources")
        if not isinstance(resources, dict) or not isinstance(resources.get("fetched"), list):
            raise RuntimeError("В ответе фото-кластеров нет resources.fetched")
        return resources["fetched"]

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
              debug: bool, skip_previews: bool = False,
              incomplete_name: str | None = None) -> Counter:
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
                                         save_incomplete=True,
                                         incomplete_name=incomplete_name)
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
    if not any(counts[key] for key in ("downloaded", "skipped", "previewed",
                                       "preview_skipped")):
        counts["unrecovered"] += 1
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

    def _worker(self, item: dict, directory: str, local_dir: Path, root: Path,
                incomplete_name: str | None) -> Counter:
        client = getattr(self.thread_state, "client", None)
        if client is None:
            client = DiskWeb(self.state, self.remote, self.request_timeout, self.debug)
            self.thread_state.client = client
        return copy_file(client, item, directory, local_dir, root, self.retries,
                         self.file_timeout, self.preview_timeout, self.debug,
                         self.skip_previews, incomplete_name)

    def submit(self, item: dict, directory: str, local_dir: Path, root: Path,
               incomplete_name: str | None = None) -> None:
        if skip_existing(local_dir, item["name"], self.debug, self.skip_previews):
            self.counts["skipped"] += 1
            return
        if self.executor is None:
            self.counts.update(copy_file(self.listing_client, item, directory, local_dir,
                                         root, self.retries, self.file_timeout,
                                         self.preview_timeout, self.debug,
                                         self.skip_previews, incomplete_name))
            return
        future = self.executor.submit(self._worker, item, directory, local_dir, root,
                                      incomplete_name)
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
                self.counts["unrecovered"] += 1
                log_message(f"Ошибка загрузки {path}: {error_label(error)}", error=True)
                print_debug_error(error, f"загрузка {path}", self.debug)

    def finish(self) -> None:
        while self.pending:
            self.collect_one()

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True)


class PauseController:
    """Pause at traversal boundaries after all submitted downloads finish."""

    def __init__(self, root: Path):
        self.root = root
        self.mount = next(path for path in (root, *root.parents) if path.is_mount())
        self.requested = False
        self.wakeup = Event()

    def toggle(self, _signum=None, _frame=None) -> None:
        # A Python signal handler runs on the main thread. Do no I/O here.
        self.requested = not self.requested
        self.wakeup.set()

    def checkpoint(self, queue: FileQueue) -> None:
        if not self.requested:
            return
        queue.finish()
        if not self.requested:
            return
        os.sync()
        if not self.requested:
            return
        pid = os.getpid()
        log_message(f"Пауза: загрузки завершены. Можно отключить диск. PID={pid}\n"
                    f"Для продолжения: kill -HUP {pid}", error=True)
        while self.requested:
            self.wakeup.wait()
            self.wakeup.clear()
            if not self.requested and not self.destination_ready():
                self.requested = True
                log_message("Диск назначения не подключён к прежнему пути; пауза продолжается. "
                            "Подключите диск и снова отправьте SIGHUP.", error=True)
        log_message("Продолжаю копирование.", error=True)

    def destination_ready(self) -> bool:
        try:
            return (self.mount.is_mount() and self.root.is_dir()
                    and self.root.resolve(strict=True) == self.root)
        except OSError:
            return False


def is_excluded(name: str, destination: Path, root: Path,
                excludes: tuple[str, ...]) -> bool:
    if not excludes:
        return False
    normalized_name = unicodedata.normalize("NFC", name)
    parent_parts = destination.relative_to(root).parts
    relative_path = "/" + "/".join(
        unicodedata.normalize("NFC", part) for part in (*parent_parts, name)
    )
    return any(
        relative_path.startswith(prefix) if prefix.startswith("/")
        else normalized_name.startswith(prefix)
        for prefix in excludes
    )


def reserve_incomplete_names(children: list[dict]) -> dict[int, str]:
    """Keep generated artifacts distinct from listed files, dirs, and each other."""
    reserved = set()
    for item in children:
        try:
            name = safe_name(item.get("name"))
        except ValueError:
            continue
        reserved.add(unicodedata.normalize("NFC", name).casefold())

    allocated = {}
    prefix = f"incomplete_{os.getpid()}-"
    for item in children:
        if item.get("type") != "file":
            continue
        try:
            name = safe_name(item.get("name"))
        except ValueError:
            continue
        candidate = prefix + name
        suffix = 1
        while unicodedata.normalize("NFC", candidate).casefold() in reserved:
            candidate = f"{prefix}{suffix}-{name}"
            suffix += 1
        reserved.add(unicodedata.normalize("NFC", candidate).casefold())
        allocated[id(item)] = candidate
    return allocated


def copy_tree(client: DiskWeb, remote: str, local: Path, root: Path,
              queue: FileQueue, excludes: tuple[str, ...] = (),
              pause: PauseController | None = None) -> None:
    ensure_directory(local, root)
    pending_directories = [(remote, local)]
    while pending_directories:
        if pause is not None:
            pause.checkpoint(queue)
        directory, destination = pending_directories.pop()
        try:
            children = list(client.list_children(directory))
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
            queue.counts["errors"] += 1
            log_message(f"Ошибка списка {directory}: {error_label(error)}", error=True)
            print_debug_error(error, f"список {directory}", queue.debug)
            continue
        incomplete_names = reserve_incomplete_names(children)

        # Create direct child directories before queuing files, so colliding
        # file/directory names cannot race on case-insensitive filesystems.
        nested = []
        for item in children:
            if pause is not None:
                pause.checkpoint(queue)
            if item.get("type") != "dir":
                continue
            try:
                name = safe_name(item.get("name"))
                path = item["path"]
                if is_excluded(name, destination, root, excludes):
                    queue.counts["excluded_dirs"] += 1
                    print_progress(f"Исключена папка: {path}", queue.debug)
                    continue
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
            if pause is not None:
                pause.checkpoint(queue)
            if item.get("type") == "dir":
                continue
            try:
                name = safe_name(item.get("name"))
                if item.get("type") != "file":
                    raise ValueError("Неизвестный тип ресурса")
                if is_excluded(name, destination, root, excludes):
                    queue.counts["excluded_files"] += 1
                    print_progress(f"Исключён файл: {item['path']}", queue.debug)
                    continue
                queue.submit(item, directory, destination, root,
                             incomplete_names[id(item)])
            except (KeyError, ValueError, OSError, RuntimeError) as error:
                queue.counts["errors"] += 1
                if item.get("type") == "file":
                    queue.counts["unrecovered"] += 1
                log_message(f"Ошибка элемента в {directory}: {error_label(error)}", error=True)
                print_debug_error(error, f"элемент папки {directory}", queue.debug)

        pending_directories.extend(reversed(nested))


def autoload_item(resource: dict) -> tuple[dict, tuple[str, ...]] | None:
    """Accept only an image in the photo auto-upload namespace."""
    if not isinstance(resource, dict):
        raise ValueError("Неожиданный ресурс фото-кластера")
    albums = resource.get("albums")
    if isinstance(albums, list) and "photounlim" not in albums:
        return None
    path = None
    for candidate in (resource.get("path"), resource.get("id")):
        if isinstance(candidate, str):
            if candidate.startswith("photounlim:/"):
                candidate = "/photounlim/" + candidate[len("photounlim:/"):]
            if candidate.startswith("/photounlim/"):
                path = candidate
                break
    if path is None:
        return None
    parts = tuple(safe_name(part) for part in path[len("/photounlim/"):].split("/"))
    meta = resource.get("meta")
    if not isinstance(meta, dict):
        meta = resource
    mime = meta.get("mimetype") or resource.get("mimetype")
    media_type = meta.get("mediatype") or resource.get("mediatype")
    if isinstance(mime, str) and mime and not mime.startswith("image/"):
        return None
    if isinstance(media_type, str) and media_type and media_type != "image":
        return None
    if not mime and not media_type:
        return None
    return {"name": parts[-1], "path": path, "type": "file", "meta": meta}, parts


def copy_autoload(client: DiskWeb, local: Path, root: Path, queue: FileQueue,
                  excludes: tuple[str, ...] = (),
                  pause: PauseController | None = None) -> None:
    """List the virtual photo slice without traversing ordinary Disk folders."""
    ensure_directory(local, root)
    try:
        snapshot = client.photo_snapshot()
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        queue.counts["errors"] += 1
        log_message(f"Не удалось открыть фотосрез: {error_label(error)}", error=True)
        print_debug_error(error, "фотосрез", queue.debug)
        return

    seen_paths = set()

    def process_batch(batch: list[tuple[str, int]]) -> None:
        if pause is not None:
            pause.checkpoint(queue)
        try:
            resources = client.photo_resources(snapshot, batch)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
            queue.counts["errors"] += 1
            log_message(f"Ошибка получения ресурсов фото-кластеров: {error_label(error)}",
                        error=True)
            print_debug_error(error, "ресурсы фото-кластеров", queue.debug)
            return
        queue.counts["autoload_clusters"] += len(batch)
        for resource in resources:
            if pause is not None:
                pause.checkpoint(queue)
            try:
                parsed = autoload_item(resource)
                if parsed is None:
                    queue.counts["autoload_filtered"] += 1
                    continue
                item, parts = parsed
                if item["path"] in seen_paths:
                    continue
                seen_paths.add(item["path"])
                destination = local.joinpath(*parts[:-1])
                if is_excluded(parts[-1], destination, local, excludes):
                    queue.counts["excluded_files"] += 1
                    continue
                ensure_directory(destination, root)
                queue.submit(item, "/photounlim", destination, root)
            except (KeyError, ValueError, OSError, RuntimeError) as error:
                queue.counts["errors"] += 1
                log_message(f"Ошибка ресурса автозагрузки: {error_label(error)}", error=True)
                print_debug_error(error, "ресурс автозагрузки", queue.debug)

    batch = []
    listed_clusters = 0
    try:
        for cluster in client.photo_clusters(snapshot):
            listed_clusters += 1
            batch.append(cluster)
            if len(batch) >= PHOTO_CLUSTER_BATCH:
                process_batch(batch)
                batch = []
        if batch:
            process_batch(batch)
        if not listed_clusters:
            queue.counts["errors"] += 1
            log_message("Фотосрез не вернул ни одного кластера; выгрузка не подтверждена.",
                        error=True)
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        queue.counts["errors"] += 1
        log_message(f"Ошибка списка фото-кластеров: {error_label(error)}", error=True)
        print_debug_error(error, "список фото-кластеров", queue.debug)
        if batch:
            process_batch(batch)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("Обычно: python3 ydisk_saver.py ~/tmp/disk; "
                "другая папка: python3 ydisk_saver.py ~/tmp/disk --remote-path /disk/crons; "
                "автозагрузка: python3 ydisk_saver.py --autoloads ~/tmp/photos"),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("local_dir", type=Path,
                        help="Локальная папка назначения; файлы сохраняются прямо в ней")
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH,
                        help="Начальная папка Диска в обычном режиме")
    parser.add_argument("--autoloads", action="store_true",
                        help="Скачать только фото автозагрузки в локальную папку назначения")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE,
                        help="Файл сессии Яндекс Диска")
    parser.add_argument("--request-timeout", type=float, default=30.0,
                        help="Таймаут сетевого запроса в секундах")
    parser.add_argument("--file-timeout", type=float, default=4.0,
                        help="Лимит времени скачивания оригинала в секундах")
    parser.add_argument("--preview-timeout", type=float, default=4.0,
                        help="Лимит времени скачивания превью в секундах")
    parser.add_argument("--retries", type=int, default=2,
                        help="Число попыток скачать оригинал")
    parser.add_argument("--workers", type=int, default=1,
                        help="Число одновременных загрузок файлов (от 1 до 8)")
    parser.add_argument("--skip-previews", action="store_true",
                        help="Не скачивать оригинал, если локальное превью уже существует")
    parser.add_argument("--exclude", action="append", metavar="PREFIX", default=[],
                        help="Пропустить файлы и папки: префикс имени на любой глубине или /путь от корня; можно повторить")
    parser.add_argument("--debug", action="store_true", help="Печатать все сообщения и traceback в stderr")
    args = parser.parse_args()
    if args.autoloads:
        if args.remote_path != DEFAULT_REMOTE_PATH:
            parser.error("--remote-path нельзя использовать вместе с --autoloads")
        remote = "/photo"
    else:
        try:
            remote = normalize_remote(args.remote_path)
        except ValueError as error:
            parser.error(str(error))
    if (args.request_timeout <= 0 or args.file_timeout <= 0
            or args.preview_timeout <= 0 or args.retries < 1):
        parser.error("Таймауты и число попыток должны быть положительными")
    if not 1 <= args.workers <= 8:
        parser.error("Число потоков --workers должно быть от 1 до 8")
    for prefix in args.exclude:
        if not prefix or "\\" in prefix or "\0" in prefix:
            parser.error("--exclude ожидает непустой префикс без \\ и NUL")
        if "/" in prefix and not prefix.startswith("/"):
            parser.error("--exclude с / внутри должен начинаться с /: путь отсчитывается от корня копируемой папки")
        if prefix.startswith("/") and any(
            part in ("", ".", "..") for part in prefix[1:].split("/")
        ):
            parser.error("--exclude ожидает путь вида /папка/префикс без пустых сегментов, . и ..")
    excludes = tuple(unicodedata.normalize("NFC", prefix) for prefix in args.exclude)
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
        pause = PauseController(root)
        previous_sighup = signal.signal(signal.SIGHUP, pause.toggle)
        try:
            pid = os.getpid()
            print_progress(f"PID процесса: {pid}\nПауза/продолжение: kill -HUP {pid}",
                           args.debug)
            if args.autoloads:
                copy_autoload(client, root, root, queue, excludes, pause)
            else:
                copy_tree(client, remote, root, root, queue, excludes, pause)
            queue.finish()
            pause.checkpoint(queue)
        finally:
            try:
                queue.close()
                pause.checkpoint(queue)
            finally:
                signal.signal(signal.SIGHUP, previous_sighup)
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        log_message(f"Не удалось начать копирование: {error_label(error)}", error=True)
        print_debug_error(error, "запуск копирования", args.debug)
        return 1
    print_progress("Итог: " + ", ".join(f"{key}={counts[key]}" for key in
                                  ("downloaded", "skipped", "previewed",
                                   "preview_skipped", "unrecovered", "directories", "excluded_dirs",
                                   "excluded_files", "autoload_clusters", "autoload_filtered",
                                   "errors")), args.debug)
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
