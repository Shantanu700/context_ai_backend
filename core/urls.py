from rest_framework.routers import SimpleRouter

from .views import AdViewSet, VideoViewSet

# trailing_slash=False so POST /videos works without an APPEND_SLASH redirect
router = SimpleRouter(trailing_slash=False)
router.register("videos", VideoViewSet, basename="video")
router.register("ads", AdViewSet, basename="ad")

urlpatterns = router.urls
