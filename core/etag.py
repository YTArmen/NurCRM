"""13.11.2026: ETag для profile/ и company/addons/ (касса шлёт If-None-Match → 304)."""
import hashlib
import json

from rest_framework.response import Response


def etag_response(request, data, status=200):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    tag = 'W/"%s"' % hashlib.md5(raw).hexdigest()
    inm = request.META.get("HTTP_IF_NONE_MATCH", "")
    sent = {t.strip().removeprefix("W/") for t in inm.split(",") if t.strip()}
    if tag.removeprefix("W/") in sent or "*" in sent:
        resp = Response(status=304)
    else:
        resp = Response(data, status=status)
    resp["ETag"] = tag
    resp["Cache-Control"] = "private, no-cache"
    return resp
