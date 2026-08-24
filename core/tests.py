import base64
import os
import shutil
import subprocess
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from django.conf import settings

from . import media
from .matching import ad_text, rank_ads, scene_text
from .models import Ad, AdSlot, Scene, Tone, Video
from .slots import seed_slots
from . import storage
from .storage import pull_to_tmp, purge_tmp, store, sweep_tmp

MEDIA = tempfile.mkdtemp(prefix="ctxai-tests-")


class SmokeTests(TestCase):
    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "db": "ok"})

    def test_models_persist(self):
        video = Video.objects.create(source_url="https://example.com/v.mp4", scenes_total=2)
        ad = Ad.objects.create(
            brand="Acme", title="Rockets", description="fast", ad_type=Ad.AdType.VIDEO, iab_categories=["IAB2"],
        )
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
    def setUp(self):
        # isolate the cache root: the real /tmp/ctxai holds pulls from actual pipeline
        # runs, and a sweep test would count those too
        patcher = patch.object(storage, "TMP_ROOT", Path(tempfile.mkdtemp(prefix="ctxai-tests-")))
        self.tmp_root = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp_root, True)

    def test_store_then_pull_round_trips(self):
        src = Path(MEDIA) / "src.bin"
        src.write_bytes(b"video-bytes")
        key = store(src, "videos/test/src.bin")
        self.assertEqual(pull_to_tmp(key).read_bytes(), b"video-bytes")

    def test_purge_removes_the_cached_pull_and_its_directory(self):
        src = Path(MEDIA) / "purge.bin"
        src.write_bytes(b"x" * 32)
        key = store(src, "videos/purge-me/purge.bin")
        cached = pull_to_tmp(key)
        self.assertTrue(cached.exists())

        purge_tmp(key)
        self.assertFalse(cached.exists())
        self.assertFalse(cached.parent.exists())  # per-video dir tidied
        self.assertTrue(self.tmp_root.exists())  # but never the cache root itself

    def test_purge_is_safe_to_repeat_and_ignores_an_empty_key(self):
        purge_tmp("")
        purge_tmp("videos/never-existed/x.bin")  # must not raise

    def test_sweep_removes_only_stale_entries(self):
        src = Path(MEDIA) / "sweep.bin"
        src.write_bytes(b"y" * 16)
        fresh = pull_to_tmp(store(src, "videos/fresh/sweep.bin"))
        stale = pull_to_tmp(store(src, "videos/stale/sweep.bin"))
        old = time.time() - 60 * 60 * 24
        os.utime(stale, (old, old))

        self.assertEqual(sweep_tmp(max_age_hours=6), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        purge_tmp("videos/fresh/sweep.bin")


@override_settings(MEDIA_ROOT=MEDIA)
class ApiTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

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
            {
                "brand": "Acme", "title": "Boots", "description": "fast",
                "ad_type": "video", "iab_categories": ["IAB2"],
            },
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
                ("/login", "get"),
                ("/login", "post"),
                ("/login", "delete"),
                ("/videos", "get"),
                ("/videos", "post"),
                ("/videos/{uuid}", "get"),
                ("/videos/{uuid}", "delete"),
                ("/videos/{uuid}/scenes", "get"),
                ("/videos/{uuid}/slots", "get"),
                ("/videos/{uuid}/slots", "put"),
                ("/videos/{uuid}/status", "get"),
                ("/videos/{uuid}/reprocess", "post"),
                ("/ads", "get"),
                ("/ads", "post"),
                ("/ads/{id}", "get"),
                ("/ads/{id}", "delete"),
                ("/ads/{id}/asset", "put"),
                ("/tones", "get"),
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
            tone=Tone.objects.create(name="warm"), iab_categories=["Automotive", "Travel"],
            embedding=vec(1.0, 0.0),
        )

    def test_shared_iab_category_outranks_a_closer_vector(self):
        closer = Ad.objects.create(
            brand="Closer", title="Exact vector match", description="x", ad_type=Ad.AdType.VIDEO,
            iab_categories=["Pets"], embedding=vec(1.0, 0.0),
        )
        # cosine similarity 0.9, so it trails by 0.1 — less than one IAB_BOOST of 0.15
        boosted = Ad.objects.create(
            brand="Boosted", title="Shares two categories", description="x", ad_type=Ad.AdType.VIDEO,
            iab_categories=["Automotive", "Travel"], embedding=vec(0.9, 0.43588989),
        )

        ranked = rank_ads(self.scene)
        self.assertEqual(ranked[0][0].pk, boosted.pk)
        self.assertEqual(ranked[1][0].pk, closer.pk)
        self.assertAlmostEqual(ranked[1][1], 1.0, places=4)  # sim 1.0, no shared category
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_iab_match_is_case_insensitive(self):
        Ad.objects.create(
            brand="A", title="lowercased categories", description="x", ad_type=Ad.AdType.VIDEO,
            iab_categories=["automotive"], embedding=vec(1.0, 0.0),
        )
        self.assertAlmostEqual(rank_ads(self.scene)[0][1], 1.0 + settings.IAB_BOOST, places=4)

    def test_ads_without_an_embedding_are_never_matched(self):
        Ad.objects.create(brand="Unembedded", title="no vector", description="x", ad_type=Ad.AdType.VIDEO)
        self.assertEqual(rank_ads(self.scene), [])

    def test_ranking_is_capped_at_top_k(self):
        for i in range(settings.TOP_K_ADS + 3):
            Ad.objects.create(
                brand=f"B{i}", title=f"T{i}", description="x", ad_type=Ad.AdType.VIDEO,
                embedding=vec(1.0, i / 100),
            )
        self.assertEqual(len(rank_ads(self.scene)), settings.TOP_K_ADS)

    def test_embedded_text_carries_visuals_tone_and_speech(self):
        self.scene.objects_seen = ["car", "coastline"]
        self.scene.transcript_text = "we should pull over here"
        text = scene_text(self.scene)
        for fragment in ["family drives", "car, coastline", "warm", "Automotive", "pull over"]:
            self.assertIn(fragment, text)

    def test_ad_text_includes_brand_tone_and_categories(self):
        ad = Ad.objects.create(
            brand="Northwind", title="Vega EV", description="An electric crossover.", ad_type=Ad.AdType.VIDEO,
            iab_categories=["Automotive"], target_tone=Tone.objects.create(name="aspirational"),
        )
        self.assertIn("Northwind", ad_text(ad))
        self.assertIn("aspirational", ad_text(ad))
        self.assertIn("Automotive", ad_text(ad))


class AnalyzeSceneTaskTests(TestCase):
    """The per-scene task must fill the record, count progress, and never stall the chord."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))
        self.video = Video.objects.create(scenes_total=1, status=Video.Status.PROCESSING)
        self.scene = Scene.objects.create(video=self.video, index=0, start=0, end=5)
        self.ad = Ad.objects.create(
            brand="Kettle & Co", title="Coffee", description="beans", ad_type=Ad.AdType.VIDEO,
            iab_categories=["Food & Drink"], target_tone=Tone.objects.create(name="warm"), embedding=vec(1.0),
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
        self.assertEqual(self.scene.tone.name, "warm")
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


class StageFlagTaskTests(TestCase):
    """detect_scenes/transcribe_audio/build_scenes/realign_scenes honor the per-video toggles."""

    def test_detect_scenes_disabled_yields_one_full_span_scene(self):
        from . import tasks

        video = Video.objects.create(
            file_key="videos/x/src.mp4", duration=12.0, detect_scenes_enabled=False
        )
        with (
            patch.object(tasks, "pull_to_tmp", return_value=Path("/tmp/src.mp4")),
            patch.object(tasks.media, "detect_scenes") as detect,
            patch.object(tasks.media, "keyframe_times", return_value=[6.0]),
            patch.object(tasks.media, "extract_keyframe", return_value=Path("/tmp/f.jpg")),
            patch.object(tasks, "store", return_value="videos/x/frames/0000_0.jpg"),
        ):
            scenes = tasks.detect_scenes(video.pk)

        detect.assert_not_called()
        self.assertEqual(
            scenes, [{"start": 0.0, "end": 12.0, "keyframe_keys": ["videos/x/frames/0000_0.jpg"]}]
        )

    def test_transcribe_audio_disabled_returns_empty_without_touching_media(self):
        from . import tasks

        video = Video.objects.create(file_key="videos/x/src.mp4", has_audio=True, transcribe_enabled=False)
        with patch.object(tasks, "pull_to_tmp") as pull:
            self.assertEqual(tasks.transcribe_audio(video.pk), [])
        pull.assert_not_called()

    def test_transcribe_audio_persists_raw_segments(self):
        from . import tasks

        video = Video.objects.create(file_key="videos/x/src.mp4", has_audio=True, transcribe_enabled=True)
        segments = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        with (
            patch.object(tasks, "pull_to_tmp", return_value=Path("/tmp/src.mp4")),
            patch.object(tasks.media, "extract_audio", return_value=Path("/tmp/audio.wav")),
            patch.object(tasks.media, "transcribe", return_value=segments),
        ):
            result = tasks.transcribe_audio(video.pk)

        self.assertEqual(result, segments)
        video.refresh_from_db()
        self.assertEqual(video.transcript_segments, segments)

    def test_build_scenes_skips_analysis_and_finishes_when_disabled(self):
        from . import tasks

        video = Video.objects.create(analyze_enabled=False, file_key="")
        with patch.object(tasks, "purge_tmp"):
            tasks.build_scenes(([{"start": 0.0, "end": 5.0, "keyframe_keys": []}], []), video.pk)

        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.DONE)
        self.assertEqual(video.scenes.count(), 1)

    def test_realign_scenes_updates_text_in_place_without_touching_analysis(self):
        from . import tasks

        video = Video.objects.create(analyze_enabled=False)
        ad = Ad.objects.create(brand="A", title="T", description="d", ad_type=Ad.AdType.VIDEO, iab_categories=[])
        scene = Scene.objects.create(
            video=video, index=0, start=0.0, end=5.0,
            keyframe_keys=["videos/x/frames/0000_0.jpg"],
            description="already analyzed", recommended_ad=ad,
        )

        tasks.realign_scenes([{"start": 1.0, "end": 2.0, "text": "hello"}], video.pk)

        scene.refresh_from_db()
        video.refresh_from_db()
        self.assertEqual(scene.transcript_text, "hello")
        self.assertEqual(scene.description, "already analyzed")  # untouched
        self.assertEqual(scene.recommended_ad, ad)  # untouched
        self.assertEqual(scene.keyframe_keys, ["videos/x/frames/0000_0.jpg"])  # untouched
        self.assertEqual(video.status, Video.Status.DONE)  # analyze disabled -> straight to done


class ReprocessTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_requires_at_least_one_stage(self):
        video = Video.objects.create()
        resp = self.client.post(f"/videos/{video.uuid}/reprocess", {}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_transcribe_only_before_any_scenes_exist(self):
        video = Video.objects.create()
        resp = self.client.post(
            f"/videos/{video.uuid}/reprocess", {"transcribe": True}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_while_already_processing(self):
        video = Video.objects.create(status=Video.Status.PROCESSING)
        resp = self.client.post(
            f"/videos/{video.uuid}/reprocess", {"analyze": True}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 409)

    def test_detect_scenes_requested_dispatches_full_rebuild(self):
        video = Video.objects.create(status=Video.Status.DONE)
        with patch("core.views.dispatch_detect_and_transcribe") as dispatch:
            dispatch.return_value.id = "job-1"
            resp = self.client.post(
                f"/videos/{video.uuid}/reprocess",
                {"detect_scenes": True, "transcribe": True},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        dispatch.assert_called_once_with(video.pk, True)
        video.refresh_from_db()
        self.assertTrue(video.detect_scenes_enabled)
        self.assertEqual(video.status, Video.Status.PROCESSING)

    def test_transcribe_only_reuses_existing_scenes(self):
        video = Video.objects.create(status=Video.Status.DONE)
        Scene.objects.create(video=video, index=0, start=0, end=5)
        with patch("core.views.dispatch_transcribe_only") as dispatch:
            dispatch.return_value.id = "job-2"
            resp = self.client.post(
                f"/videos/{video.uuid}/reprocess", {"transcribe": True}, content_type="application/json"
            )
        self.assertEqual(resp.status_code, 202)
        dispatch.assert_called_once_with(video.pk)

    def test_analyze_only_requeues_existing_scenes(self):
        video = Video.objects.create(status=Video.Status.DONE)
        Scene.objects.create(video=video, index=0, start=0, end=5)
        with patch("core.views.queue_analysis_or_finish") as dispatch:
            dispatch.return_value.id = "job-3"
            resp = self.client.post(
                f"/videos/{video.uuid}/reprocess", {"analyze": True}, content_type="application/json"
            )
        self.assertEqual(resp.status_code, 202)
        args, _ = dispatch.call_args
        self.assertEqual(args[0], video.pk)


class AdEmbeddingTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_creating_an_ad_over_the_api_queues_its_embedding(self):
        with patch("core.views.embed_ad.delay") as delay:
            resp = self.client.post(
                "/ads",
                {"brand": "A", "title": "T", "description": "d", "ad_type": "video", "iab_categories": ["Pets"]},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201)
        delay.assert_called_once_with(Ad.objects.get().pk)


class AdAssetTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_creating_an_ad_with_a_file_stores_it_and_exposes_asset_url(self):
        upload = SimpleUploadedFile("clip.mp4", b"\x00\x01", content_type="video/mp4")
        with patch("core.views.embed_ad.delay"):
            resp = self.client.post(
                "/ads",
                {"brand": "A", "title": "T", "description": "d", "ad_type": "video", "asset": upload},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertIsNotNone(resp.json()["asset_url"])
        self.assertTrue(Ad.objects.get().asset_key)

    def test_creating_an_ad_without_a_file_leaves_asset_url_null(self):
        with patch("core.views.embed_ad.delay"):
            resp = self.client.post(
                "/ads",
                {"brand": "A", "title": "T", "description": "d", "ad_type": "overlay"},
                content_type="application/json",
            )
        self.assertIsNone(resp.json()["asset_url"])

    def test_asset_action_replaces_the_stored_file(self):
        ad = Ad.objects.create(brand="A", title="T", description="d", ad_type=Ad.AdType.OVERLAY)
        first = SimpleUploadedFile("first.png", b"\x00", content_type="image/png")
        second = SimpleUploadedFile("second.png", b"\x01", content_type="image/png")

        self.client.put(f"/ads/{ad.pk}/asset", {"asset": first}, format="multipart")
        ad.refresh_from_db()
        first_key = ad.asset_key

        resp = self.client.put(f"/ads/{ad.pk}/asset", {"asset": second}, format="multipart")
        ad.refresh_from_db()
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(ad.asset_key, first_key)
        self.assertEqual(resp.json()["asset_url"], default_storage.url(ad.asset_key))

    def test_deleting_an_ad_nulls_the_scenes_that_recommended_it(self):
        video = Video.objects.create()
        ad = Ad.objects.create(brand="A", title="T", description="d", ad_type=Ad.AdType.VIDEO)
        scene = Scene.objects.create(video=video, index=0, start=0.0, end=1.0, recommended_ad=ad)

        resp = self.client.delete(f"/ads/{ad.pk}")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Ad.objects.filter(pk=ad.pk).exists())
        scene.refresh_from_db()
        self.assertIsNone(scene.recommended_ad)


class SeedSlotsTests(TestCase):
    """The first draft of the ad plan, derived from the analysis exactly once."""

    def setUp(self):
        self.video = Video.objects.create(duration=100.0)
        self.ad = Ad.objects.create(
            brand="Kettle & Co", title="Coffee", description="beans", ad_type=Ad.AdType.VIDEO,
        )

    def _scene(self, index, start, ad=None, score=None):
        return Scene.objects.create(
            video=self.video, index=index, start=start, end=start + 4,
            recommended_ad=ad, match_score=score,
        )

    def test_placement_follows_position_in_the_video(self):
        self._scene(0, 2.0, self.ad)    # inside the leading 5%
        self._scene(1, 50.0, self.ad)
        self._scene(2, 97.0, self.ad)   # inside the trailing 5%

        self.assertEqual(seed_slots(self.video), 3)
        self.assertEqual(
            list(self.video.slots.order_by("at_seconds").values_list("placement", flat=True)),
            [AdSlot.Placement.PRE, AdSlot.Placement.MID, AdSlot.Placement.POST],
        )

    def test_carries_the_scene_ad_and_score_across(self):
        self._scene(0, 50.0, self.ad, score=0.88)

        seed_slots(self.video)

        slot = self.video.slots.get()
        self.assertEqual(slot.ad, self.ad)
        self.assertEqual(slot.scene.index, 0)
        self.assertEqual(slot.at_seconds, 50.0)
        self.assertEqual(slot.score, 0.88)
        self.assertEqual(slot.state, AdSlot.State.SUGGESTED)
        self.assertFalse(slot.is_overlay)

    def test_an_overlay_ad_lands_in_the_overlay_lane(self):
        overlay = Ad.objects.create(
            brand="Volt", title="Bug", description="d", ad_type=Ad.AdType.OVERLAY,
        )
        self._scene(0, 50.0, overlay)

        seed_slots(self.video)

        self.assertTrue(self.video.slots.get().is_overlay)

    def test_scenes_without_a_recommendation_are_skipped(self):
        self._scene(0, 10.0, self.ad)
        self._scene(1, 20.0, None)  # never analyzed, or nothing matched

        self.assertEqual(seed_slots(self.video), 1)

    def test_seeding_twice_does_not_undo_the_operators_edits(self):
        self._scene(0, 10.0, self.ad)
        self._scene(1, 20.0, self.ad)
        seed_slots(self.video)
        self.video.slots.filter(at_seconds=20.0).delete()  # operator rejected and removed it

        self.assertEqual(seed_slots(self.video), 0)
        self.assertEqual(self.video.slots.count(), 1)

    def test_unknown_duration_falls_back_to_mid_roll(self):
        self.video.duration = None
        self.video.save(update_fields=["duration"])
        self._scene(0, 0.0, self.ad)

        seed_slots(self.video)

        self.assertEqual(self.video.slots.get().placement, AdSlot.Placement.MID)


class AdSlotApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="pw")
        self.client.force_login(self.user)
        self.video = Video.objects.create(user=self.user, duration=100.0, status=Video.Status.DONE)
        self.ad = Ad.objects.create(
            brand="Acme", title="Boots", description="d", ad_type=Ad.AdType.VIDEO,
        )
        self.scene = Scene.objects.create(
            video=self.video, index=0, start=30.0, end=40.0, recommended_ad=self.ad, match_score=0.91,
        )

    def _put(self, body):
        return self.client.put(
            f"/videos/{self.video.uuid}/slots", body, content_type="application/json"
        )

    def test_get_seeds_the_plan_and_nests_the_ad(self):
        body = self.client.get(f"/videos/{self.video.uuid}/slots").json()

        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["at_seconds"], 30.0)
        self.assertEqual(body[0]["ad_detail"]["brand"], "Acme")
        self.assertEqual(body[0]["score"], 0.91)

    def test_get_is_a_bare_array_not_a_page(self):
        self.assertIsInstance(self.client.get(f"/videos/{self.video.uuid}/slots").json(), list)

    def test_put_replaces_the_whole_plan(self):
        self.client.get(f"/videos/{self.video.uuid}/slots")  # seed first

        resp = self._put([
            {"at_seconds": 12.5, "duration": 6, "state": "accepted", "ad": self.ad.pk,
             "placement": "mid_roll", "is_overlay": False},
            {"at_seconds": 80.0, "duration": 30, "state": "held", "ad": None,
             "placement": "post_roll", "is_overlay": True},
        ])

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([s["at_seconds"] for s in resp.json()], [12.5, 80.0])
        self.assertEqual(self.video.slots.count(), 2)  # the seeded slot is gone

    def test_put_with_an_empty_list_clears_the_plan(self):
        self.client.get(f"/videos/{self.video.uuid}/slots")

        self.assertEqual(self._put([]).json(), [])
        self.assertEqual(self.video.slots.count(), 0)

    def test_a_cleared_plan_is_not_re_seeded_on_the_next_read(self):
        self.client.get(f"/videos/{self.video.uuid}/slots")
        self._put([])

        self.assertEqual(self.client.get(f"/videos/{self.video.uuid}/slots").json(), [])

    def test_put_rejects_an_invalid_slot_without_touching_the_stored_plan(self):
        self.client.get(f"/videos/{self.video.uuid}/slots")

        resp = self._put([
            {"at_seconds": 10.0, "duration": 15},
            {"at_seconds": -3.0, "duration": 15},  # before the video starts
        ])

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.video.slots.get().at_seconds, 30.0)  # the seeded plan survived

    def test_put_rejects_a_zero_duration(self):
        self.assertEqual(self._put([{"at_seconds": 1.0, "duration": 0}]).status_code, 400)

    def test_a_slot_cannot_point_at_another_videos_scene(self):
        other = Video.objects.create(user=self.user)
        stranger = Scene.objects.create(video=other, index=0, start=0.0, end=1.0)

        resp = self._put([{"at_seconds": 1.0, "duration": 15, "scene": stranger.pk}])

        self.assertEqual(resp.status_code, 400)

    def test_slots_are_scoped_to_the_owner(self):
        self.client.force_login(get_user_model().objects.create_user("intruder", password="pw"))
        self.assertEqual(self.client.get(f"/videos/{self.video.uuid}/slots").status_code, 404)

    def test_retrieve_exposes_the_file_url_the_editor_plays(self):
        self.video.file_key = "videos/x/src.mp4"
        self.video.save(update_fields=["file_key"])

        body = self.client.get(f"/videos/{self.video.uuid}").json()

        self.assertEqual(body["uuid"], str(self.video.uuid))
        self.assertIsNotNone(body["file_url"])

    def test_deleting_the_video_takes_its_slots_with_it(self):
        self.client.get(f"/videos/{self.video.uuid}/slots")

        self.client.delete(f"/videos/{self.video.uuid}")

        self.assertEqual(AdSlot.objects.count(), 0)


class ToneListTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_lists_existing_tones_alphabetically_as_a_bare_array(self):
        Tone.objects.create(name="warm")
        Tone.objects.create(name="aspirational")

        resp = self.client.get("/tones")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["name"] for t in resp.json()], ["aspirational", "warm"])


class PreviewFramesTests(TestCase):
    def test_plain_numbers_are_seconds_not_frame_numbers(self):
        from core.management.commands.preview_frames import _timecode

        # PySceneDetect reads a bare int as a frame index: "90" meant 1.5s at 59.94fps
        self.assertEqual(_timecode("90"), 90.0)
        self.assertIsInstance(_timecode("90"), float)
        self.assertEqual(_timecode("00:01:30"), "00:01:30")
        self.assertIsNone(_timecode(None))


class AuthTests(TestCase):
    """Authenticated by default; only the probe and the login POST are public."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="s3cret")
        self.video = Video.objects.create()

    def _basic(self, username="tester", password="s3cret"):
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"

    def test_health_is_public(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_protected_endpoints_reject_anonymous_callers(self):
        for method, path in [
            ("post", "/videos"),
            ("get", f"/videos/{self.video.uuid}/status"),
            ("get", f"/videos/{self.video.uuid}/scenes"),
            ("post", f"/videos/{self.video.uuid}/reprocess"),
            ("get", "/ads"),
            ("post", "/ads"),
            ("get", "/login"),
        ]:
            with self.subTest(path=path, method=method):
                resp = getattr(self.client, method)(path)
                # 401, not 403 — the client needs to know it should log in
                self.assertEqual(resp.status_code, 401)

    def test_login_returns_a_session_that_unlocks_the_api(self):
        resp = self.client.post("/login", HTTP_AUTHORIZATION=self._basic())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "tester")
        self.assertEqual(self.client.get("/ads").status_code, 200)

    def test_login_rejects_bad_credentials(self):
        resp = self.client.post("/login", HTTP_AUTHORIZATION=self._basic(password="wrong"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.client.get("/ads").status_code, 401)

    def test_login_rejects_a_missing_or_malformed_header(self):
        self.assertEqual(self.client.post("/login").status_code, 401)
        self.assertEqual(self.client.post("/login", HTTP_AUTHORIZATION="Bearer xyz").status_code, 401)

    def test_logout_drops_the_session(self):
        self.client.post("/login", HTTP_AUTHORIZATION=self._basic())
        self.assertEqual(self.client.delete("/login").status_code, 204)
        self.assertEqual(self.client.get("/ads").status_code, 401)

    def test_csrf_is_not_enforced_on_session_authenticated_writes(self):
        # DisableCSRFMiddleware: a cross-origin SPA cannot supply a CSRF token
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        resp = client.post(
            "/ads",
            {"brand": "A", "title": "T", "description": "d", "iab_categories": []},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)


class CorsTests(TestCase):
    ORIGIN = "https://client.example.com"

    def test_preflight_is_answered_for_the_client_origin(self):
        resp = self.client.options(
            "/videos",
            HTTP_ORIGIN=self.ORIGIN,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
        )
        self.assertEqual(resp.status_code, 200)
        # echoed, not "*", because credentials are allowed
        self.assertEqual(resp.headers["access-control-allow-origin"], self.ORIGIN)
        self.assertEqual(resp.headers["access-control-allow-credentials"], "true")

    def test_actual_response_carries_the_cors_headers(self):
        resp = self.client.get("/health", HTTP_ORIGIN=self.ORIGIN)
        self.assertEqual(resp.headers["access-control-allow-origin"], self.ORIGIN)
