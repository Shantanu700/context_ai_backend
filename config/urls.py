from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health),
    path("", include("core.urls")),
]

if settings.DEBUG and settings.STORAGE_BACKEND == "local":
    # so keyframe URLs are fetchable in dev; R2 serves them directly in prod
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
