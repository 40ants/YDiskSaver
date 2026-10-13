"""Synthetic concurrency checks; no Yandex session or network access."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import errno
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, get_ident
import time
import unittest
from unittest.mock import patch

from ydisk_fs import save_stream
from ydisk_saver import FileQueue, copy_tree, main


class ListingClient:
    def __init__(self, children):
        self.children = children

    def list_children(self, remote):
        return self.children.get(remote, [])


class Response(BytesIO):
    def __init__(self, data, content_type="application/octet-stream", delay=0):
        super().__init__(data)
        self.delay = delay
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(data))

    def read(self, amount=-1):
        if self.delay:
            time.sleep(self.delay)
        return super().read(amount)


class ParallelTests(unittest.TestCase):
    def test_repeated_excludes_skip_directories_but_not_files(self):
        remote = "/disk/test"
        excluded = {remote + "/cache-data", remote + "/.git-store",
                    remote + "/keep/cache-nested"}
        visited = []
        listings = {
            remote: [
                {"name": "cache-data", "path": remote + "/cache-data", "type": "dir"},
                {"name": ".git-store", "path": remote + "/.git-store", "type": "dir"},
                {"name": "keep", "path": remote + "/keep", "type": "dir"},
                {"name": "cache-file.txt", "path": remote + "/cache-file.txt",
                 "type": "file", "meta": {"size": 4}},
            ],
            remote + "/keep": [
                {"name": "cache-nested", "path": remote + "/keep/cache-nested",
                 "type": "dir"},
                {"name": "data.txt", "path": remote + "/keep/data.txt",
                 "type": "file", "meta": {"size": 4}},
            ],
        }

        class SyntheticDiskWeb:
            def __init__(self, *args):
                pass

            def list_children(self, path):
                if path in excluded:
                    raise AssertionError("excluded directory was queried")
                visited.append(path)
                return listings[path]

            def download_url(self, path):
                return path

            def open_download(self, url):
                return Response(b"data")

        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = root / "synthetic-state"
            state.touch()
            output = root / "output"
            argv = ["ydisk_saver.py", "--state", str(state), "--workers", "2",
                    "--exclude", "cache", "--exclude", ".git", remote, str(output)]
            stdout, stderr = StringIO(), StringIO()
            with patch("ydisk_saver.DiskWeb", SyntheticDiskWeb), patch.object(sys, "argv", argv), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = main()
            self.assertEqual(result, 0)
            self.assertEqual(set(visited), {remote, remote + "/keep"})
            self.assertEqual((output / "cache-file.txt").read_bytes(), b"data")
            self.assertEqual((output / "keep" / "data.txt").read_bytes(), b"data")
            self.assertFalse((output / "cache-data").exists())
            self.assertFalse((output / ".git-store").exists())
            self.assertFalse((output / "keep" / "cache-nested").exists())
            self.assertIn("excluded_dirs=3", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_skip_previews_avoids_original_request_in_both_modes(self):
        remote = "/disk/test"
        item = {"name": "photo.jpg", "path": remote + "/photo.jpg", "type": "file"}
        listing = ListingClient({remote: [item]})
        for workers in (1, 2):
            with self.subTest(workers=workers), TemporaryDirectory() as folder, \
                    patch("ydisk_saver.DiskWeb", side_effect=AssertionError("HTTP session created")):
                root = Path(folder).resolve()
                (root / "preview-photo.jpg").mkdir()
                (root / "preview-photo.png").write_bytes(b"preview")
                counts = Counter()
                queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                                  workers, counts, False, True)
                try:
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        copy_tree(listing, remote, root, root, queue)
                        queue.finish()
                finally:
                    queue.close()
                self.assertEqual(counts["skipped"], 1)
                self.assertEqual(counts["errors"], 0)
                self.assertFalse((root / "photo.jpg").exists())

    def test_preview_does_not_skip_original_without_flag(self):
        remote = "/disk/test"
        item = {"name": "photo.jpg", "path": remote + "/photo.jpg", "type": "file"}

        class FailingClient(ListingClient):
            def __init__(self):
                super().__init__({remote: [item]})
                self.requests = 0

            def download_url(self, path):
                self.requests += 1
                raise TimeoutError("synthetic")

        listing = FailingClient()
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "preview-photo.jpg").write_bytes(b"preview")
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              1, counts, False)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                copy_tree(listing, remote, root, root, queue)
                queue.finish()
            queue.close()
            self.assertEqual(listing.requests, 1)
            self.assertEqual(counts["errors"], 1)
            self.assertEqual(counts["preview_skipped"], 1)

    def test_download_starts_before_listing_nested_directory(self):
        remote = "/disk/test"
        started = Event()

        class OrderedListing:
            def list_children(self, directory):
                if directory == remote:
                    return [
                        {"name": "nested", "path": remote + "/nested", "type": "dir"},
                        {"name": "here.txt", "path": remote + "/here.txt", "type": "file"},
                    ]
                if not started.wait(timeout=2):
                    raise AssertionError("nested listing started before the download")
                return []

        class WorkerClient:
            def __init__(self, *args):
                pass

            def download_url(self, path):
                started.set()
                return path

            def open_download(self, url):
                return Response(b"data")

        listing = OrderedListing()
        with TemporaryDirectory() as folder, patch("ydisk_saver.DiskWeb", WorkerClient):
            root = Path(folder).resolve()
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              2, counts, False)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    copy_tree(listing, remote, root, root, queue)
                    queue.finish()
            finally:
                queue.close()
            self.assertEqual(counts["downloaded"], 1)
            self.assertEqual(counts["directories"], 1)
            self.assertEqual(counts["errors"], 0)

    def test_directory_is_created_before_conflicting_file(self):
        remote = "/disk/test"
        items = [
            {"name": "same", "path": remote + "/same-dir", "type": "dir"},
            {"name": "same", "path": remote + "/same-file", "type": "file"},
        ]
        listing = ListingClient({remote: items})
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              1, counts, False)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                copy_tree(listing, remote, root, root, queue)
                queue.finish()
            queue.close()
            self.assertTrue((root / "same").is_dir())
            self.assertEqual(counts["directories"], 1)
            self.assertEqual(counts["errors"], 1)

    def test_parallel_downloads_and_existing_file_skip(self):
        remote = "/disk/test"
        items = [
            {"name": "nested", "path": remote + "/nested", "type": "dir"},
            {"name": "existing.txt", "path": remote + "/existing.txt", "type": "file"},
        ] + [
            {"name": f"file-{n}.txt", "path": f"{remote}/file-{n}.txt", "type": "file",
             "meta": {"size": 4}}
            for n in range(6)
        ]
        nested = [{"name": "inside.txt", "path": remote + "/nested/inside.txt",
                   "type": "file", "meta": {"size": 4}}]
        listing = ListingClient({remote: items, remote + "/nested": nested})
        state = {"active": 0, "max_active": 0, "requests": [], "clients": []}
        guard = Lock()

        class WorkerClient:
            def __init__(self, *args):
                self.thread_id = get_ident()
                with guard:
                    state["clients"].append(self)

            def download_url(self, path):
                self.assert_own_thread()
                with guard:
                    state["requests"].append(path)
                return path

            def assert_own_thread(self):
                if get_ident() != self.thread_id:
                    raise AssertionError("worker shared its HTTP session")

            def open_download(self, url):
                self.assert_own_thread()

                class SlowResponse(Response):
                    def __enter__(self):
                        with guard:
                            state["active"] += 1
                            state["max_active"] = max(state["max_active"], state["active"])
                        return super().__enter__()

                    def __exit__(self, *args):
                        with guard:
                            state["active"] -= 1
                        return super().__exit__(*args)

                return SlowResponse(b"data", delay=0.02)

        with TemporaryDirectory() as folder, patch("ydisk_saver.DiskWeb", WorkerClient):
            root = Path(folder).resolve()
            (root / "existing.txt").write_bytes(b"old")
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              3, counts, False)
            maximum_pending = 0
            original_submit = queue.submit

            def tracked_submit(*args):
                nonlocal maximum_pending
                original_submit(*args)
                maximum_pending = max(maximum_pending, len(queue.pending))

            queue.submit = tracked_submit
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    copy_tree(listing, remote, root, root, queue)
                    queue.finish()
            finally:
                queue.close()
            self.assertEqual(counts["downloaded"], 7)
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(counts["directories"], 1)
            self.assertEqual(counts["errors"], 0)
            self.assertGreaterEqual(state["max_active"], 2)
            self.assertLessEqual(state["max_active"], 3)
            self.assertLessEqual(maximum_pending, 5)
            self.assertNotIn(remote + "/existing.txt", state["requests"])
            self.assertEqual((root / "existing.txt").read_bytes(), b"old")
            self.assertEqual((root / "nested" / "inside.txt").read_bytes(), b"data")
            self.assertLessEqual(len(state["clients"]), 3)

    def test_one_worker_uses_existing_listing_session(self):
        remote = "/disk/test"
        item = {"name": "one.txt", "path": remote + "/one.txt", "type": "file"}

        class SerialClient(ListingClient):
            def download_url(self, path):
                return path

            def open_download(self, url):
                return Response(b"serial")

        listing = SerialClient({remote: [item]})
        with TemporaryDirectory() as folder, patch("ydisk_saver.DiskWeb",
                                                    side_effect=AssertionError("extra session")):
            root = Path(folder).resolve()
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              1, counts, False)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                copy_tree(listing, remote, root, root, queue)
                queue.finish()
            queue.close()
            self.assertEqual(counts["downloaded"], 1)
            self.assertEqual((root / "one.txt").read_bytes(), b"serial")

    def test_incomplete_original_still_saves_preview(self):
        remote = "/disk/test"
        item = {"name": "photo.jpg", "path": remote + "/photo.jpg", "type": "file",
                "meta": {"size": 6, "sizes": [{"name": "L", "url":
                    "https://downloader.disk.yandex.ru/preview"}]}}
        listing = ListingClient({remote: [item]})

        class WorkerClient:
            def __init__(self, *args):
                pass

            def download_url(self, path):
                return "original"

            def open_download(self, url):
                if url == "original":
                    return Response(b"abc")
                return Response(b"preview", "image/jpeg")

        with TemporaryDirectory() as folder, patch("ydisk_saver.DiskWeb", WorkerClient):
            root = Path(folder).resolve()
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              2, counts, True)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    copy_tree(listing, remote, root, root, queue)
                    queue.finish()
            finally:
                queue.close()
            self.assertEqual(counts["errors"], 1)
            self.assertEqual(counts["previewed"], 1)
            self.assertEqual((root / "incomplete-photo.jpg").read_bytes(), b"abc")
            self.assertEqual((root / "preview-photo.jpg").read_bytes(), b"preview")
            self.assertFalse((root / "photo.jpg").exists())

    def test_worker_session_failure_is_counted_per_file(self):
        remote = "/disk/test"
        items = [{"name": f"file-{n}.txt", "path": f"{remote}/file-{n}.txt",
                  "type": "file"} for n in range(3)]
        listing = ListingClient({remote: items})
        with TemporaryDirectory() as folder, patch("ydisk_saver.DiskWeb",
                                                    side_effect=TimeoutError("synthetic")):
            root = Path(folder).resolve()
            counts = Counter()
            queue = FileQueue(listing, Path("unused-state"), remote, 1, 1, 2, 2,
                              2, counts, True)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    copy_tree(listing, remote, root, root, queue)
                    queue.finish()
            finally:
                queue.close()
            self.assertEqual(counts["errors"], 3)
            self.assertEqual(counts["downloaded"], 0)

    def test_fat_fallback_never_overwrites_parallel_target(self):
        class BarrierStream(BytesIO):
            def __init__(self, data, barrier):
                super().__init__(data)
                self.barrier = barrier
                self.first = True

            def read(self, amount=-1):
                if self.first:
                    self.first = False
                    self.barrier.wait(timeout=3)
                return super().read(amount)

        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "same.txt"
            barrier = Barrier(2)
            with patch("ydisk_fs.os.link", side_effect=OSError(errno.ENOTSUP, "FAT")):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(save_stream, BarrierStream(data, barrier),
                                           target, root, len(data))
                               for data in (b"first", b"second")]
                    statuses = [future.result() for future in futures]
            self.assertCountEqual(statuses, ["downloaded", "skipped"])
            self.assertIn(target.read_bytes(), (b"first", b"second"))
            self.assertEqual(list(root.glob(".yadisk-copy-*.part")), [])


if __name__ == "__main__":
    unittest.main()
