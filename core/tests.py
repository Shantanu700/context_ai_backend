import shutil
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from django.conf import settings

from . import media
from .matching import ad_text, rank_ads, scene_text
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


class MediaTests(TestCase):
    def test_keyframe_count_scales_with_scene_length(self):
        self.assertEqual(media.keyframe_times(0, 4), [2.0])  # short scene: one frame, mid-scene
        self.assertEqual(len(media.keyframe_times(0, 10)), 2)
        self.assertEqual(len(media.keyframe_times(0, 30)), 3)
        for start, end in [(0, 4), (0, 10), (12.5, 40)]:
            for t in media.keyframe_times(start, end):
                self.assertTrue(start < t < end)  # never land on a cut point

    def test_align_assigns_segments_by_midpoint(self):
        bounds = [(0.0, 5.0), (5.0, 10.0)]
        segments = [
            {"start": 0.5, "end": 2.0, "text": "first"},
            {"start": 4.5, "end": 5.4, "text": "straddles the cut"},  # midpoint 4.95 -> scene 0
            {"start": 6.0, "end": 7.0, "text": "second"},
            {"start": 10.2, "end": 11.0, "text": "overruns the video"},
        ]
        self.assertEqual(
            media.align(bounds, segments),
            ["first straddles the cut", "second overruns the video"],
        )

    def test_align_handles_a_silent_video(self):
        self.assertEqual(media.align([(0.0, 5.0)], []), [""])


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class FfmpegTests(TestCase):
    """Pins the ffprobe/ffmpeg contract: a real clip in, parsed metadata and a JPEG out."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp(prefix="ctxai-ffmpeg-tests-"))
        cls.clip = cls.tmp / "clip.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=10:d=2",
            "-pix_fmt", "yuv420p", str(cls.clip),
        ], check=True)

    def test_probe_reads_metadata_and_no_audio_stream(self):
        info = media.probe(self.clip)
        self.assertAlmostEqual(info["duration"], 2.0, places=1)
        self.assertEqual((info["width"], info["height"]), (320, 240))
        self.assertFalse(info["has_audio"])

    def test_probe_rejects_a_file_with_no_video(self):
        audio = self.tmp / "tone.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", str(audio),
        ], check=True)
        with self.assertRaisesRegex(ValueError, "no video stream"):
            media.probe(audio)

    def test_keyframe_is_never_upscaled(self):
        out = media.extract_keyframe(self.clip, 1.0, self.tmp / "f.jpg")
        self.assertEqual(media.probe(out)["width"], 320)  # source is narrower than KEYFRAME_WIDTH


class SchemaTests(TestCase):
    """The schema is the contract the Next.js client generates from — keep it honest."""

    def _schema(self):
        from drf_spectacular.generators import SchemaGenerator

        return SchemaGenerator().get_schema(request=None, public=True)

    def test_every_endpoint_is_documented(self):
        paths = self._schema()["paths"]
        self.assertEqual(
            {(p, verb) for p, ops in paths.items() for verb in ops},
            {
                ("/health", "get"),
                ("/videos", "post"),
                ("/videos/{uuid}/scenes", "get"),
                ("/videos/{uuid}/status", "get"),
                ("/ads", "get"),
                ("/ads", "post"),
            },
        )

    def test_scenes_is_documented_as_a_bare_array(self):
        op = self._schema()["paths"]["/videos/{uuid}/scenes"]["get"]
        body = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(body["type"], "array")  # the view does not paginate
        self.assertEqual([p["name"] for p in op.get("parameters", [])], ["uuid"])

    def test_upload_documents_both_request_shapes(self):
        op = self._schema()["paths"]["/videos"]["post"]
        # DRF's default parsers also accept form-urlencoded; these two are the ones that matter
        self.assertLessEqual(
            {"application/json", "multipart/form-data"}, set(op["requestBody"]["content"])
        )
        self.assertIn("202", op["responses"])

    def test_schema_endpoint_serves_yaml(self):
        resp = self.client.get("/schema")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("openapi", resp.headers["Content-Type"])


def vec(*head) -> list[float]:
    """A unit-ish EMBEDDING_DIM vector whose leading components we control."""
    return list(head) + [0.0] * (settings.EMBEDDING_DIM - len(head))


class MatchingTests(TestCase):
    def setUp(self):
        self.video = Video.objects.create()
        self.scene = Scene.objects.create(
            video=self.video, index=0, start=0, end=5,
            description="a family drives along the coast",
            tone="warm", iab_categories=["Automotive", "Travel"],
            embedding=vec(1.0, 0.0),
        )

    def test_shared_iab_category_outranks_a_closer_vector(self):
        closer = Ad.objects.create(
            brand="Closer", title="Exact vector match", description="x",
            iab_categories=["Pets"], embedding=vec(1.0, 0.0),
        )
        # cosine similarity 0.9, so it trails by 0.1 — less than one IAB_BOOST of 0.15
        boosted = Ad.objects.create(
            brand="Boosted", title="Shares two categories", description="x",
            iab_categories=["Automotive", "Travel"], embedding=vec(0.9, 0.43588989),
        )

        ranked = rank_ads(self.scene)
        self.assertEqual(ranked[0][0].pk, boosted.pk)
        self.assertEqual(ranked[1][0].pk, closer.pk)
        self.assertAlmostEqual(ranked[1][1], 1.0, places=4)  # sim 1.0, no shared category
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_iab_match_is_case_insensitive(self):
        Ad.objects.create(
            brand="A", title="lowercased categories", description="x",
            iab_categories=["automotive"], embedding=vec(1.0, 0.0),
        )
        self.assertAlmostEqual(rank_ads(self.scene)[0][1], 1.0 + settings.IAB_BOOST, places=4)

    def test_ads_without_an_embedding_are_never_matched(self):
        Ad.objects.create(brand="Unembedded", title="no vector", description="x")
        self.assertEqual(rank_ads(self.scene), [])

    def test_ranking_is_capped_at_top_k(self):
        for i in range(settings.TOP_K_ADS + 3):
            Ad.objects.create(brand=f"B{i}", title=f"T{i}", description="x", embedding=vec(1.0, i / 100))
        self.assertEqual(len(rank_ads(self.scene)), settings.TOP_K_ADS)

    def test_embedded_text_carries_visuals_tone_and_speech(self):
        self.scene.objects_seen = ["car", "coastline"]
        self.scene.transcript_text = "we should pull over here"
        text = scene_text(self.scene)
        for fragment in ["family drives", "car, coastline", "warm", "Automotive", "pull over"]:
            self.assertIn(fragment, text)

    def test_ad_text_includes_brand_tone_and_categories(self):
        ad = Ad.objects.create(
            brand="Northwind", title="Vega EV", description="An electric crossover.",
            iab_categories=["Automotive"], target_tone="aspirational",
        )
        self.assertIn("Northwind", ad_text(ad))
        self.assertIn("aspirational", ad_text(ad))
        self.assertIn("Automotive", ad_text(ad))


class AnalyzeSceneTaskTests(TestCase):
    """The per-scene task must fill the record, count progress, and never stall the chord."""

    def setUp(self):
        self.video = Video.objects.create(scenes_total=1, status=Video.Status.PROCESSING)
        self.scene = Scene.objects.create(video=self.video, index=0, start=0, end=5)
        self.ad = Ad.objects.create(
            brand="Kettle & Co", title="Coffee", description="beans",
            iab_categories=["Food & Drink"], target_tone="warm", embedding=vec(1.0),
        )

    def _run(self, analysis=None, fit=None, embed_side_effect=None):
        from types import SimpleNamespace

        from . import tasks

        analysis = analysis or SimpleNamespace(
            description="two friends share coffee", objects=["mug"], tone="warm",
            iab_categories=["Food & Drink"],
        )
        fit = fit or SimpleNamespace(rationale="the ad matches the cosy mood", brand_safety_flag=False)
        with (
            patch.object(tasks.gemini, "analyze_scene", return_value=analysis) as ana,
            patch.object(tasks.gemini, "write_rationale", return_value=fit),
            patch.object(tasks, "embed", side_effect=embed_side_effect or (lambda texts: [vec(1.0)])),
        ):
            tasks.analyze_scene(self.scene.pk)
        return ana

    def test_fills_tags_recommendation_and_progress(self):
        self._run()
        self.scene.refresh_from_db()
        self.video.refresh_from_db()

        self.assertEqual(self.scene.description, "two friends share coffee")
        self.assertEqual(self.scene.tone, "warm")
        self.assertEqual(self.scene.iab_categories, ["Food & Drink"])
        self.assertEqual(self.scene.objects_seen, ["mug"])
        self.assertEqual(self.scene.recommended_ad, self.ad)
        self.assertEqual(self.scene.rationale, "the ad matches the cosy mood")
        self.assertFalse(self.scene.brand_safety_flag)
        self.assertEqual(self.scene.top_matches, [{"ad_id": self.ad.pk, "score": 1.15}])
        self.assertEqual(self.video.scenes_done, 1)

    def test_persists_a_raised_brand_safety_flag(self):
        from types import SimpleNamespace

        self._run(fit=SimpleNamespace(rationale="upbeat ad against a grim scene", brand_safety_flag=True))
        self.scene.refresh_from_db()
        self.assertTrue(self.scene.brand_safety_flag)

    def test_a_failing_scene_still_counts_so_the_chord_completes(self):
        self._run(embed_side_effect=lambda texts: (_ for _ in ()).throw(RuntimeError("gemini exploded")))
        self.scene.refresh_from_db()
        self.video.refresh_from_db()

        self.assertEqual(self.scene.description, "")  # nothing persisted
        self.assertIsNone(self.scene.recommended_ad)
        self.assertEqual(self.video.scenes_done, 1)  # but progress advanced

    def test_quota_exhaustion_is_requeued_not_counted(self):
        rate_limited = RuntimeError("429 RESOURCE_EXHAUSTED")
        rate_limited.code = 429  # what google.genai APIError carries

        # called directly rather than through a worker, celery's retry() re-raises the
        # original exception instead of Retry — either way it leaves analyze_scene
        with self.assertRaises(RuntimeError):
            self._run(embed_side_effect=lambda texts: (_ for _ in ()).throw(rate_limited))

        self.video.refresh_from_db()
        self.assertEqual(self.video.scenes_done, 0)  # re-queued, so not counted as done

    def test_a_non_transient_error_is_not_requeued(self):
        broken = ValueError("malformed response")  # no .code, so nothing to retry for
        self._run(embed_side_effect=lambda texts: (_ for _ in ()).throw(broken))
        self.video.refresh_from_db()
        self.assertEqual(self.video.scenes_done, 1)

    def test_status_reports_the_silent_failure(self):
        self._run(embed_side_effect=lambda texts: (_ for _ in ()).throw(RuntimeError("boom")))
        body = self.client.get(f"/videos/{self.video.uuid}/status").json()
        self.assertEqual(body["scenes_done"], 1)
        self.assertEqual(body["scenes_failed"], 1)


class AdEmbeddingTests(TestCase):
    def test_creating_an_ad_over_the_api_queues_its_embedding(self):
        with patch("core.views.embed_ad.delay") as delay:
            resp = self.client.post(
                "/ads",
                {"brand": "A", "title": "T", "description": "d", "iab_categories": ["Pets"]},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201)
        delay.assert_called_once_with(Ad.objects.get().pk)


class PreviewFramesTests(TestCase):
    def test_plain_numbers_are_seconds_not_frame_numbers(self):
        from core.management.commands.preview_frames import _timecode

        # PySceneDetect reads a bare int as a frame index: "90" meant 1.5s at 59.94fps
        self.assertEqual(_timecode("90"), 90.0)
        self.assertIsInstance(_timecode("90"), float)
        self.assertEqual(_timecode("00:01:30"), "00:01:30")
        self.assertIsNone(_timecode(None))
