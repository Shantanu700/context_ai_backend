from rest_framework.routers import SimpleRouter

from .views import AdViewSet, ToneViewSet, VideoViewSet

# trailing_slash=False so POST /videos works without an APPEND_SLASH redirect
router = SimpleRouter(trailing_slash=False)
router.register("videos", VideoViewSet, basename="video")
router.register("ads", AdViewSet, basename="ad")
router.register("tones", ToneViewSet, basename="tone")

urlpatterns = router.urls
