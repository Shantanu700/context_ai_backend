import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import Ad, Scene, Video
from .storage import pull_to_tmp, store

MEDIA = tempfile.mkdtemp(prefix="ctxai-tests-")


class SmokeTests(TestCase):
    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "db": "ok"})

    def test_models_persist(self):
        video = Video.objects.create(source_url="https://example.com/v.mp4", scenes_total=2)
        ad = Ad.objects.create(brand="Acme", title="Rockets", description="fast", iab_categories=["IAB2"])
        scene = Scene.objects.create(video=video, index=0, start=0.0, end=4.5, recommended_ad=ad)

        self.assertEqual(video.status, Video.Status.PENDING)
        self.assertIsInstance(video.id, int)  # bigint pk, uuid is the public id
        self.assertEqual(video.uuid.version, 7)
        self.assertEqual(video.scenes.get().pk, scene.pk)
        self.assertIsNone(ad.embedding)
        self.assertFalse(scene.brand_safety_flag)

    def test_video_uuids_are_unique_and_time_sortable(self):
        a = Video.objects.create()
        b = Video.objects.create()
        self.assertNotEqual(a.uuid, b.uuid)
        self.assertLess(a.uuid, b.uuid)  # uuid7 is time-ordered: sorts by creation


@override_settings(MEDIA_ROOT=MEDIA)
class StorageTests(TestCase):
    def test_store_then_pull_round_trips(self):
        src = Path(MEDIA) / "src.bin"
        src.write_bytes(b"video-bytes")
        key = store(src, "videos/test/src.bin")
        self.assertEqual(pull_to_tmp(key).read_bytes(), b"video-bytes")


@override_settings(MEDIA_ROOT=MEDIA)
class ApiTests(TestCase):
    def test_upload_dispatches_pipeline(self):
        upload = SimpleUploadedFile("clip.mp4", b"\x00\x01", content_type="video/mp4")
        with patch("core.views.process_video.delay") as delay:
            delay.return_value.id = "job-1"  # a bare Mock id makes DRF's encoder loop forever
            resp = self.client.post("/videos", {"file": upload})

        self.assertEqual(resp.status_code, 202)
        video = Video.objects.get(uuid=resp.json()["uuid"])
        self.assertTrue(video.file_key.startswith(f"videos/{video.uuid}/"))
        delay.assert_called_once_with(video.pk)

    def test_source_url_dispatches_without_a_file(self):
        with patch("core.views.process_video.delay") as delay:
            delay.return_value.id = "job-1"
            resp = self.client.post(
                "/videos", {"source_url": "https://example.com/a.mp4"}, content_type="application/json"
            )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(Video.objects.get(uuid=resp.json()["uuid"]).file_key, "")
        delay.assert_called_once()

    def test_upload_requires_exactly_one_source(self):
        with patch("core.views.process_video.delay") as delay:
            resp = self.client.post("/videos", {}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        delay.assert_not_called()

    def test_status_reports_progress(self):
        video = Video.objects.create(status=Video.Status.PROCESSING, scenes_total=54, scenes_done=32)
        body = self.client.get(f"/videos/{video.uuid}/status").json()
        self.assertEqual(body["progress"], "32/54 scenes analyzed")
        self.assertEqual(body["status"], "processing")

    def test_ads_list_and_create(self):
        resp = self.client.post(
            "/ads",
            {"brand": "Acme", "title": "Boots", "description": "fast", "iab_categories": ["IAB2"]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("embedding", resp.json())
        self.assertEqual(self.client.get("/ads").json()["count"], 1)
