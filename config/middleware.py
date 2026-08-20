from django.utils.deprecation import MiddlewareMixin


class DisableCSRFMiddleware(MiddlewareMixin):
    """SessionAuthentication enforces CSRF, which a cross-origin SPA client cannot satisfy.

    Same approach as certifier_reloaded_backend. Safe only because the session cookie is
    SameSite=None; Secure and the API is JSON-only — no browser form posts to forge.
    """

    def process_request(self, request):
        setattr(request, "_dont_enforce_csrf_checks", True)
