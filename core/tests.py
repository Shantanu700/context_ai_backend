from django.test import TestCase

from .models import Ad, Scene, Video


class SmokeTests(TestCase):
    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "db": "ok"})

    def test_models_persist(self):
        video = Video.objects.create(source_url="https://example.com/v.mp4", scenes_total=2)
        ad = Ad.objects.create(brand="Acme", title="Rockets", description="fast", iab_categories=["IAB2"])
        scene = Scene.objects.create(video=video, index=0, start=0.0, end=4.5, recommended_ad=ad)

        self.assertEqual(video.status, Video.Status.PENDING)
        self.assertEqual(video.scenes.get().pk, scene.pk)
        self.assertIsNone(ad.embedding)
        self.assertFalse(scene.brand_safety_flag)
