from typing import TypedDict, cast

from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import status
from rest_framework.authentication import SessionAuthentication as BaseSessionAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.request import Request


class AuthenticationMethodTypes(TypedDict, total=False):
    get: bool
    post: bool
    put: bool
    patch: bool
    delete: bool


class SessionAuthentication(BaseSessionAuthentication):
    """Advertise a challenge so DRF answers 401 instead of downgrading to 403.

    Without an `authenticate_header`, DRF turns every NotAuthenticated into a 403, which
    a client cannot tell apart from "logged in but not allowed".
    """

    def authenticate_header(self, request):
        return "Session"


class SessionScheme(OpenApiAuthenticationExtension):
    """Teach drf-spectacular about the subclass above; it only knows DRF's own classes."""

    target_class = "core.permissions.SessionAuthentication"
    name = "sessionAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "cookie", "name": settings.SESSION_COOKIE_NAME}


class APIAuthenticationPermission(BasePermission):
    """Authenticated by default; a view opts out with `authentication`.

        authentication = False              # whole view is public
        authentication = {"post": False}    # only POST is public

    Applied globally through DEFAULT_PERMISSION_CLASSES, so a new endpoint is private
    unless it says otherwise.
    """

    message = "You are unauthenticated! Please login again!"
    code = status.HTTP_401_UNAUTHORIZED

    def has_permission(self, request: Request, view) -> bool:
        authentication = getattr(view, "authentication", True)
        method = cast(str, request.method).lower()

        if not (
            authentication
            if isinstance(authentication, bool)
            else authentication.get(method, True)
        ):
            return True

        return bool(request.user and request.user.is_authenticated)
