"""T-UP-001 · 上传入口与流式落盘。

运行：python -m unittest tests.test_upload_accept -v
"""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import service.config as cfg  # noqa: E402
import service.upload as up  # noqa: E402
from tests.upload_helpers import make_video, upload, upload_result  # noqa: E402


def _expect(status, code, fn):
    try:
        fn()
    except up.UploadError as exc:
        return exc.status_code == status and exc.code == code
    return False


class TestUploadAccept(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="up_accept_")
        cls.base = pathlib.Path(cls.temp.name)
        cls.video = make_video(cls.base / "fixture4.mp4", seconds=4)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp(dir=str(self.base)))
        self.data = self.video.read_bytes()
        self.headers = {
            "content-type": "video/mp4",
            "content-length": str(len(self.data)),
            "x-upload-device-id": "ELEV-3F-001",
            "x-upload-start-ts-ms": str(up.now_ms()),
            "x-upload-location": "3F",
        }

    def _parse(self, **changes):
        headers = dict(self.headers)
        for key, value in changes.items():
            headers[key] = value
        return up.parse_upload_headers(headers, base_dir=self.work)

    def test_missing_length_411(self):
        self.assertTrue(
            _expect(411, "UP_LENGTH_REQUIRED", lambda: self._parse(**{"content-length": ""}))
        )

    def test_zero_length_400(self):
        self.assertTrue(
            _expect(400, "UP_BAD_REQUEST", lambda: self._parse(**{"content-length": "0"}))
        )

    def test_too_large_413(self):
        self.assertTrue(
            _expect(
                413,
                "UP_TOO_LARGE",
                lambda: self._parse(**{"content-length": str(up.max_bytes() + 1)}),
            )
        )

    def test_unsupported_type_415(self):
        self.assertTrue(
            _expect(
                415,
                "UP_UNSUPPORTED_TYPE",
                lambda: self._parse(**{"content-type": "text/plain"}),
            )
        )

    def test_rebase_rejected(self):
        self.assertTrue(
            _expect(400, "UP_BAD_REQUEST", lambda: self._parse(**{"x-upload-rebase-ts": "1"}))
        )

    def test_bad_device_id(self):
        self.assertTrue(
            _expect(400, "UP_BAD_DEVICE_ID", lambda: self._parse(**{"x-upload-device-id": "ELEV-DEMO-001"}))
        )

    def test_bad_timestamp(self):
        self.assertTrue(
            _expect(
                400,
                "UP_BAD_TIMESTAMP",
                lambda: self._parse(**{"x-upload-start-ts-ms": "1763300180"}),
            )
        )

    def test_accept_202_and_sha256(self):
        result = upload_result(self.work, self.video)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.record.sha256, up.UploadSink._hash_file(self.video))
        self.assertEqual(result.record.frames, 100)
        self.assertTrue(result.created)

    def test_idempotent_200_single_dir(self):
        first = upload_result(self.work, self.video)
        second = upload_result(self.work, self.video)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        dirs = [
            path
            for path in up.uploads_root(self.work).glob("*")
            if path.is_dir() and path.name != up.TMP_DIR_NAME
        ]
        self.assertEqual(len(dirs), 1)
        self.assertEqual(second.record.upload_id, first.record.upload_id)

    def test_non_ftyp_415_and_tmp_clean(self):
        intent = self._parse(**{"content-length": "12"})
        sink = up.UploadSink(intent, base_dir=self.work)
        self.assertTrue(_expect(415, "UP_UNSUPPORTED_TYPE", lambda: sink.feed(b"0123456789ab")))
        tmp_root = up.uploads_root(self.work) / up.TMP_DIR_NAME
        self.assertEqual(list(tmp_root.glob("*")) if tmp_root.is_dir() else [], [])

    def test_short_stream_400(self):
        sink = upload(self.work, self.video, declared=len(self.data) + 10)
        sink.feed(self.data)
        self.assertTrue(_expect(400, "UP_BAD_REQUEST", sink.finish))

    def test_quota_507(self):
        old = cfg.UPLOAD_QUOTA_ITEMS
        cfg.UPLOAD_QUOTA_ITEMS = 0
        try:
            self.assertTrue(
                _expect(507, "UP_QUOTA_EXCEEDED", lambda: upload_result(self.work, self.video))
            )
        finally:
            cfg.UPLOAD_QUOTA_ITEMS = old


if __name__ == "__main__":
    unittest.main()