from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from core.views import HealthAV, LoginAV

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", HealthAV.as_view()),
    path("login", LoginAV.as_view()),
    path("schema", SpectacularAPIView.as_view(), name="schema"),
    path("", include("core.urls")),
]

# Swagger/Redoc are HTML, so they are dev-only inspection tools like the admin.
# The machine-readable schema at /schema is always available for client generation.
if settings.DEBUG:
    urlpatterns += [
        path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
        path("redoc", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]

if settings.DEBUG and settings.STORAGE_BACKEND == "local":
    # so keyframe URLs are fetchable in dev; R2 serves them directly in prod
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
