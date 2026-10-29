"""Read a narrowly scoped Secret Manager credential from a Compute Engine workload."""

import base64
import re
import time
import urllib.request

from kaggle_experiments.protocol import strict_json_loads


class SecretAccessError(RuntimeError):
    pass


_RESOURCE = re.compile(r"projects/[0-9]+/secrets/[A-Za-z0-9_-]{1,255}/versions/(?:latest|[1-9][0-9]*)\Z")
_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
_SECRET_URL = "https://secretmanager.googleapis.com/v1/{}:access"
_RESPONSE_LIMIT = 16384
_TIMEOUT_SECONDS = 5


def _read_response(opener, url, headers, timeout_s=_TIMEOUT_SECONDS):
    if opener is None:
        from cloud_experiments.http_transport import bounded_request
        status, _, body = bounded_request("GET", url, headers, None, timeout_s, _RESPONSE_LIMIT)
        if status != 200:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        return body
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(request, timeout=timeout_s) as response:
        if getattr(response, "status", None) != 200:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        body = response.read(_RESPONSE_LIMIT + 1)
    if not isinstance(body, bytes) or len(body) > _RESPONSE_LIMIT:
        raise SecretAccessError("SECRET_ACCESS_FAILED")
    return body


def access_secret_resource(resource, *, opener=None):
    if not isinstance(resource, str) or not _RESOURCE.fullmatch(resource):
        raise SecretAccessError("INVALID_SECRET_RESOURCE")
    try:
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        metadata = strict_json_loads(_read_response(opener, _METADATA_URL, {"Metadata-Flavor": "Google"}))
        token = metadata.get("access_token") if isinstance(metadata, dict) else None
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        response = strict_json_loads(_read_response(opener, _SECRET_URL.format(resource),
            {"Authorization": "Bearer " + token, "Accept": "application/json"}, remaining))
        payload = response.get("payload") if isinstance(response, dict) else None
        encoded = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or len(encoded) > 8192:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        secret = base64.b64decode(encoded, validate=True)
        if not 8 <= len(secret) <= 1024:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        value = secret.decode("utf-8")
        if "\r" in value or "\n" in value:
            raise SecretAccessError("SECRET_ACCESS_FAILED")
        return value
    except SecretAccessError:
        raise
    except Exception:
        raise SecretAccessError("SECRET_ACCESS_FAILED") from None
