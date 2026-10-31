"""Synthetic virtual-photo API checks; never use a saved browser session."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ydisk_saver import DiskWeb, PHOTO_CLUSTER_PAGE, autoload_item, main


class AutoloadTests(unittest.TestCase):
    def test_photoslice_resource_path_is_converted_to_mpfs_path(self):
        item, parts = autoload_item({
            "id": "photounlim:/2026/photo.jpg",
            "meta": {"size": 3, "mimetype": "image/jpeg"},
        })
        self.assertEqual(item["path"], "/photounlim/2026/photo.jpg")
        self.assertEqual(item["meta"]["size"], 3)
        self.assertEqual(parts, ("2026", "photo.jpg"))

    def test_photo_init_omits_request_params(self):
        client = DiskWeb.__new__(DiskWeb)
        client.sk = "synthetic-sk"
        client.connection_id = "synthetic-connection"
        client.headers = {}
        client.timeout = 1
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append(json.loads(request.data))
                return BytesIO(b'{"photoslice_id":"snapshot"}')

        client.opener = Opener()
        self.assertEqual(client.photo_snapshot(), "snapshot")
        self.assertNotIn("requestParams", requests[0])

    def test_autoload_accepts_video_from_same_album(self):
        item, parts = autoload_item({
            "id": "/photounlim/video.mp4", "albums": ["photounlim"],
            "meta": {"size": 100, "mimetype": "video/mp4", "mediatype": "video"},
        })
        self.assertEqual(item["path"], "/photounlim/video.mp4")
        self.assertEqual(item["meta"]["size"], 100)
        self.assertEqual(parts, ("video.mp4",))
        self.assertIsNotNone(autoload_item({
            "id": "/photounlim/photo.heic", "albums": ["photounlim"],
            "meta": {"size": 100, "mimetype": "image/heic", "mediatype": "image"},
        }))

    def test_autoload_video_metadata_variants(self):
        for meta in ({"mimetype": "video/quicktime"}, {"mediatype": "video"}):
            for nested in (False, True):
                with self.subTest(meta=meta, nested=nested):
                    resource = {"id": "photounlim:/2026/video.mov"}
                    resource.update({"meta": meta} if nested else meta)
                    item, parts = autoload_item(resource)
                    self.assertEqual(item["path"], "/photounlim/2026/video.mov")
                    self.assertEqual(parts, ("2026", "video.mov"))

    def test_autoload_rejects_non_media_and_unconfirmed_videos(self):
        for resource in (
            {"id": "/photounlim/audio.mp3", "mimetype": "audio/mpeg"},
            {"id": "/photounlim/file.pdf", "mediatype": "document"},
            {"id": "/photounlim/unknown.mp4"},
            {"id": "/disk/video.mp4", "mimetype": "video/mp4"},
            {"id": "/photounlim/video.mp4", "mimetype": "video/mp4",
             "albums": ["camera"]},
        ):
            with self.subTest(resource=resource):
                self.assertIsNone(autoload_item(resource))

    def test_photo_api_uses_snapshot_and_photounlim_filter(self):
        client = DiskWeb.__new__(DiskWeb)
        calls = []

        def call(method, params=None):
            calls.append((method, params))
            if method == "intapi/photo-init-snapshot":
                return {"photoslice_id": "snapshot", "revision": 7}
            if method == "intapi/photo-get-snapshot":
                return {"items": [{"id": "cluster", "size": 2}], "total": 1}
            return {"resources": {"fetched": [{"id": "/photounlim/photo.jpg"}],
                                  "missing": []}}

        client.call = call
        snapshot = client.photo_snapshot()
        clusters = list(client.photo_clusters(snapshot))
        resources = client.photo_resources(snapshot, clusters)
        self.assertEqual(clusters, [("cluster", 2)])
        self.assertEqual(resources[0]["id"], "/photounlim/photo.jpg")
        self.assertEqual(calls[0], ("intapi/photo-init-snapshot", None))
        self.assertEqual(calls[1], ("intapi/photo-get-snapshot", {
            "id": "snapshot", "locale": "ru", "amount": 200, "offset": 0,
        }))
        self.assertEqual(calls[2], ("intapi/photo-get-clusters-with-resources", {
            "photosliceId": "snapshot", "clusters": {"cluster": {"range": [0, 1]}},
            "hideScreenshots": False, "filter": "photounlim",
        }))

    def test_snapshot_continues_after_short_page_without_total(self):
        client = DiskWeb.__new__(DiskWeb)
        offsets = []

        def call(_method, params):
            offsets.append(params["offset"])
            pages = {
                0: [{"id": "first", "size": 1}],
                1: [{"id": "second", "size": 2}],
                2: [],
            }
            return {"items": pages[params["offset"]]}

        client.call = call
        self.assertEqual(list(client.photo_clusters("snapshot")),
                         [("first", 1), ("second", 2)])
        self.assertEqual(offsets, [0, 1, 2])

    def test_snapshot_ignoring_page_size_is_read_once(self):
        client = DiskWeb.__new__(DiskWeb)
        calls = []

        def call(_method, params):
            calls.append(params["offset"])
            return {"items": [
                {"id": str(index), "size": 1}
                for index in range(PHOTO_CLUSTER_PAGE + 1)
            ]}

        client.call = call
        self.assertEqual(len(list(client.photo_clusters("snapshot"))),
                         PHOTO_CLUSTER_PAGE + 1)
        self.assertEqual(calls, [0])

    def test_autoload_only_downloads_confirmed_photounlim_files(self):
        class SyntheticDiskWeb:
            def __init__(self, state, remote, timeout, debug=False):
                assert remote == "/photo"

            def list_children(self, remote):
                raise AssertionError("ordinary files must not be listed")

            def photo_snapshot(self):
                return "snapshot"

            def photo_clusters(self, snapshot):
                return iter([("cluster", 4)])

            def photo_resources(self, snapshot, clusters):
                return [
                    {"clusterId": "cluster", "id": "/photounlim/2026/photo.heic",
                     "path": "/photounlim/2026/photo.heic", "type": "file",
                     "name": "photo.heic", "meta": {"size": 4, "sizes": [],
                                                   "mimetype": "image/heic"}},
                    {"id": "/disk/ordinary.jpg", "size": 4},
                    {"id": "/photounlim/2026/not-autoload.jpg", "size": 4,
                     "albums": ["camera"]},
                    {"id": "photounlim:/2026/video.mp4", "albums": ["photounlim"],
                     "meta": {"size": 5, "mimetype": "video/mp4", "mediatype": "video"}},
                ]

            def download_url(self, path):
                assert path in ("/photounlim/2026/photo.heic", "/photounlim/2026/video.mp4")
                return path

            def open_download(self, url):
                return BytesIO(b"video" if url.endswith(".mp4") else b"data")

        for workers in (1, 2):
            with self.subTest(workers=workers), TemporaryDirectory() as folder:
                root = Path(folder)
                state = root / "synthetic-state"
                state.touch()
                output = root / "output"
                argv = ["ydisk_saver.py", "--state", str(state), "--autoloads",
                        "--workers", str(workers), str(output)]
                stdout, stderr = StringIO(), StringIO()
                with patch("ydisk_saver.DiskWeb", SyntheticDiskWeb), \
                        patch.object(sys, "argv", argv), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main()
                self.assertEqual(result, 0)
                self.assertEqual((output / "2026/photo.heic").read_bytes(), b"data")
                self.assertEqual((output / "2026/video.mp4").read_bytes(), b"video")
                self.assertFalse((output / "ordinary.jpg").exists())
                self.assertFalse((output / "2026/not-autoload.jpg").exists())
                self.assertFalse((output / "АвтозагрузкаФото").exists())
                self.assertIn("autoload_clusters=1", stdout.getvalue())
                self.assertIn("autoload_filtered=2", stdout.getvalue())
                self.assertIn("downloaded=2", stdout.getvalue())

    def test_autoload_requires_one_local_path(self):
        argv = ["ydisk_saver.py", "--autoloads", "/disk/Фото", "/tmp/output"]
        with patch.object(sys, "argv", argv), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)

    def test_old_autoload_flag_is_rejected(self):
        argv = ["ydisk_saver.py", "--autoload", "/tmp/output"]
        with patch.object(sys, "argv", argv), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)

    def test_empty_photo_snapshot_does_not_report_success(self):
        class EmptyDiskWeb:
            def __init__(self, *args):
                pass

            def photo_snapshot(self):
                return "snapshot"

            def photo_clusters(self, snapshot):
                return iter(())

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "synthetic-state"
            state.touch()
            argv = ["ydisk_saver.py", "--state", str(state), "--autoloads",
                    str(root / "output")]
            stderr = StringIO()
            with patch("ydisk_saver.DiskWeb", EmptyDiskWeb), \
                    patch.object(sys, "argv", argv), \
                    redirect_stdout(StringIO()), redirect_stderr(stderr):
                result = main()
            self.assertEqual(result, 1)
            self.assertIn("не вернул ни одного кластера", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
