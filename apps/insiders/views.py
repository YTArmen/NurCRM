# API инсайдеров (support.nurcrm.kg): /api/insiders/...
import os
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .models import SPHERES, TOPIC_KINDS, TOPIC_STATUSES, Follow, MeToo, News, Post, Section, StatusEvent, Topic

SPHERE_KEYS = {k for k, _ in SPHERES}
STATUS_KEYS = {k for k, _ in TOPIC_STATUSES}
KIND_KEYS = {k for k, _ in TOPIC_KINDS}
MEDIA_PREFIX = "https://app.nurcrm.kg/media/insiders/"
MAX_IMAGE = 5 * 1024 * 1024


def is_team(u):
    return bool(getattr(u, "is_platform_admin", False) or getattr(u, "is_superuser", False))


def is_owner(u):
    return getattr(u, "role", None) == "owner" or bool(getattr(u, "owned_company_id", None))


def display_name(u):
    name = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip()
    return name or (getattr(u, "email", "") or "").split("@")[0]


def company_name(u):
    c = getattr(u, "company", None)
    return getattr(c, "name", "") or ""


class InsiderAccess(permissions.BasePermission):
    message = "Раздел для команды NurCRM и владельцев магазинов."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (is_team(u) or is_owner(u)))


class TeamOnly(permissions.BasePermission):
    message = "Только для команды NurCRM."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and is_team(u))


class WriteThrottle(UserRateThrottle):
    scope = "insiders_write"
    rate = "40/hour"


def clean_text(v, field, max_len, required=True):
    v = (v or "").strip() if isinstance(v, str) else ""
    if required and not v:
        raise ValidationError({field: "Заполните поле."})
    if len(v) > max_len:
        raise ValidationError({field: f"Слишком длинно (до {max_len} знаков)."})
    return v


def clean_images(v):
    if not v:
        return []
    if not isinstance(v, list) or len(v) > 6:
        raise ValidationError({"images": "До 6 картинок."})
    out = []
    for x in v:
        if not isinstance(x, str) or not x.startswith(MEDIA_PREFIX) or ".." in x:
            raise ValidationError({"images": "Картинку нужно загрузить заново."})
        out.append(x)
    return out


def clean_examples(v):
    if not v:
        return []
    if not isinstance(v, list) or len(v) > 8:
        raise ValidationError({"examples": "До 8 примеров."})
    out = []
    for e in v:
        if not isinstance(e, dict):
            continue
        item = {"caption": clean_text(e.get("caption"), "examples", 200, required=False)}
        for side in ("before", "after"):
            url = e.get(side) or ""
            if url:
                clean_images([url])
            item[side] = url
        if item["before"] or item["after"]:
            out.append(item)
    return out


def news_json(n, full=False):
    d = {
        "id": n.id, "title": n.title, "sphere": n.sphere, "kind": n.kind, "summary": n.summary,
        "status": n.status, "audience": n.audience,
        "published_at": n.published_at.isoformat() if n.published_at else None,
        "created_at": n.created_at.isoformat(), "source": n.source,
        "examples": n.examples[:1] if not full else n.examples,
        "examples_count": len(n.examples or []), "points": n.points or [],
    }
    if full:
        d["body"] = n.body
    return d


def topic_json(t, user=None):
    d = {
        "id": t.id, "title": t.title, "kind": t.kind, "status": t.status,
        "section": {"slug": t.section.slug, "name": t.section.name},
        "author_name": t.author_name, "company_name": t.company_name, "assignee": t.assignee,
        "posts_count": t.posts_count, "created_at": t.created_at.isoformat(),
        "last_post_at": t.last_post_at.isoformat(),
        "metoo": getattr(t, "metoo_n", None),
        "news": {"id": t.news_id, "title": t.news.title} if t.news_id else None,
        "mine": bool(user and t.author_id == user.id),
    }
    return d


def post_json(p):
    return {
        "id": p.id, "author_name": p.author_name, "company_name": p.company_name, "is_team": p.is_team,
        "body": p.body, "images": p.images or [], "created_at": p.created_at.isoformat(),
    }


class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        return Response({
            "name": display_name(u), "company": company_name(u), "team": is_team(u),
            "allowed": is_team(u) or is_owner(u),
        })


class NewsListView(APIView):
    permission_classes = [InsiderAccess]

    def get(self, request):
        team = is_team(request.user)
        qs = News.objects.all()
        if team and request.query_params.get("status") == "draft":
            qs = qs.filter(status="draft").order_by("-created_at")
        else:
            qs = qs.filter(status="published")
            if not team:
                qs = qs.filter(audience="all")
        sphere = request.query_params.get("sphere")
        if sphere in SPHERE_KEYS:
            qs = qs.filter(sphere=sphere)
        q = (request.query_params.get("q") or "").strip()[:80]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q) | Q(body__icontains=q))
        offset = max(0, int(request.query_params.get("offset") or 0))
        items = list(qs[offset:offset + 21])
        counts = dict(
            Topic.objects.filter(news_id__in=[n.id for n in items]).values_list("news_id").annotate(c=Count("id"))
        )
        out = []
        for n in items[:20]:
            d = news_json(n)
            d["topics"] = counts.get(n.id, 0)
            out.append(d)
        return Response({"results": out, "more": len(items) > 20})

    def post(self, request):
        if not is_team(request.user):
            return Response({"detail": TeamOnly.message}, status=403)
        n = News(author=request.user)
        apply_news(n, request.data, creating=True)
        n.save()
        return Response(news_json(n, full=True), status=201)


def apply_news(n, data, creating=False):
    if creating or "title" in data:
        n.title = clean_text(data.get("title"), "title", 200)
    if "sphere" in data:
        if data["sphere"] not in SPHERE_KEYS:
            raise ValidationError({"sphere": "Неизвестная сфера."})
        n.sphere = data["sphere"]
    if "kind" in data:
        n.kind = clean_text(data.get("kind"), "kind", 40, required=False) or "Обновление"
    if "summary" in data:
        n.summary = clean_text(data.get("summary"), "summary", 400, required=False)
    if "body" in data:
        n.body = clean_text(data.get("body"), "body", 20000, required=False)
    if "points" in data:
        pts = data.get("points") or []
        if not isinstance(pts, list) or len(pts) > 12:
            raise ValidationError({"points": "До 12 пунктов."})
        n.points = [clean_text(p, "points", 200) for p in pts if isinstance(p, str) and p.strip()]
    if "examples" in data:
        n.examples = clean_examples(data.get("examples"))
    if "audience" in data:
        if data["audience"] not in ("all", "team"):
            raise ValidationError({"audience": "Неверное значение."})
        n.audience = data["audience"]
    if "source" in data:
        n.source = clean_text(data.get("source"), "source", 120, required=False)
    if "status" in data:
        if data["status"] not in ("draft", "published"):
            raise ValidationError({"status": "Неверное значение."})
        if data["status"] == "published" and n.status != "published":
            n.published_at = timezone.now()
        n.status = data["status"]


class NewsDetailView(APIView):
    permission_classes = [InsiderAccess]

    def get_obj(self, request, pk):
        n = get_object_or_404(News, pk=pk)
        if not is_team(request.user) and (n.status != "published" or n.audience != "all"):
            raise Http404
        return n

    def get(self, request, pk):
        n = self.get_obj(request, pk)
        d = news_json(n, full=True)
        d["topics"] = [topic_json(t) for t in n.topics.select_related("section", "news")[:20]]
        return Response(d)

    def patch(self, request, pk):
        if not is_team(request.user):
            return Response({"detail": TeamOnly.message}, status=403)
        n = get_object_or_404(News, pk=pk)
        apply_news(n, request.data)
        n.save()
        return Response(news_json(n, full=True))

    def delete(self, request, pk):
        if not is_team(request.user):
            return Response({"detail": TeamOnly.message}, status=403)
        get_object_or_404(News, pk=pk).delete()
        return Response(status=204)


class SectionsView(APIView):
    permission_classes = [InsiderAccess]

    def get(self, request):
        rows = Section.objects.annotate(n=Count("topics")).order_by("order", "id")
        return Response([{"slug": s.slug, "name": s.name, "hint": s.hint, "topics": s.n} for s in rows])


class TopicListView(APIView):
    permission_classes = [InsiderAccess]

    def get_throttles(self):
        return [WriteThrottle()] if self.request.method == "POST" else super().get_throttles()

    def get(self, request):
        qs = Topic.objects.select_related("section", "news").annotate(metoo_n=Count("metoo", distinct=True))
        p = request.query_params
        if p.get("section"):
            qs = qs.filter(section__slug=p["section"])
        if p.get("status") in STATUS_KEYS:
            qs = qs.filter(status=p["status"])
        elif p.get("status") == "open":
            qs = qs.exclude(status__in=["done", "rejected"])
        if p.get("mine") == "1":
            qs = qs.filter(Q(author=request.user) | Q(follows__user=request.user)).distinct()
        q = (p.get("q") or "").strip()[:80]
        if q:
            qs = qs.filter(title__icontains=q)
        offset = max(0, int(p.get("offset") or 0))
        limit = min(50, max(1, int(p.get("limit") or 30)))
        items = list(qs[offset:offset + limit + 1])
        return Response({"results": [topic_json(t, request.user) for t in items[:limit]], "more": len(items) > limit})

    def post(self, request):
        u = request.user
        d = request.data
        section = Section.objects.filter(slug=d.get("section")).first()
        if not section:
            raise ValidationError({"section": "Выберите раздел."})
        kind = d.get("kind") if d.get("kind") in KIND_KEYS else "bug"
        title = clean_text(d.get("title"), "title", 200)
        body = clean_text(d.get("body"), "body", 8000)
        images = clean_images(d.get("images"))
        news = None
        if d.get("news"):
            nq = News.objects.filter(pk=d.get("news"), status="published")
            news = (nq if is_team(u) else nq.filter(audience="all")).first()
        with transaction.atomic():
            t = Topic.objects.create(
                section=section, title=title, kind=kind, author=u, news=news,
                author_name=display_name(u), company_name="" if is_team(u) else company_name(u), posts_count=1,
            )
            Post.objects.create(
                topic=t, author=u, author_name=t.author_name, company_name=t.company_name,
                is_team=is_team(u), body=body, images=images,
            )
            Follow.objects.get_or_create(topic=t, user=u)
        t.metoo_n = 0
        return Response(topic_json(t, u), status=201)


class TopicDetailView(APIView):
    permission_classes = [InsiderAccess]

    def get(self, request, pk):
        t = get_object_or_404(Topic.objects.select_related("section", "news").annotate(metoo_n=Count("metoo", distinct=True)), pk=pk)
        d = topic_json(t, request.user)
        d["posts"] = [post_json(p) for p in t.posts.all()]
        d["events"] = [
            {"old": e.old, "new": e.new, "by_name": e.by_name, "created_at": e.created_at.isoformat()}
            for e in t.events.all()
        ]
        d["i_metoo"] = MeToo.objects.filter(topic=t, user=request.user).exists()
        d["i_follow"] = Follow.objects.filter(topic=t, user=request.user).exists()
        d["followers"] = t.follows.count()
        return Response(d)

    def patch(self, request, pk):
        u = request.user
        if not is_team(u):
            return Response({"detail": TeamOnly.message}, status=403)
        t = get_object_or_404(Topic, pk=pk)
        d = request.data
        if "status" in d:
            if d["status"] not in STATUS_KEYS:
                raise ValidationError({"status": "Неизвестный статус."})
            if d["status"] != t.status:
                StatusEvent.objects.create(topic=t, old=t.status, new=d["status"], by_name=display_name(u))
                t.status = d["status"]
        if "assignee" in d:
            t.assignee = clean_text(d.get("assignee"), "assignee", 80, required=False)
        if "news" in d:
            t.news = News.objects.filter(pk=d["news"]).first() if d["news"] else None
        if "section" in d:
            s = Section.objects.filter(slug=d["section"]).first()
            if s:
                t.section = s
        t.save()
        return self.get(request, pk)

    def delete(self, request, pk):
        if not is_team(request.user):
            return Response({"detail": TeamOnly.message}, status=403)
        get_object_or_404(Topic, pk=pk).delete()
        return Response(status=204)


class TopicPostView(APIView):
    permission_classes = [InsiderAccess]
    throttle_classes = [WriteThrottle]

    def post(self, request, pk):
        u = request.user
        t = get_object_or_404(Topic, pk=pk)
        body = clean_text(request.data.get("body"), "body", 8000)
        images = clean_images(request.data.get("images"))
        with transaction.atomic():
            p = Post.objects.create(
                topic=t, author=u, author_name=display_name(u),
                company_name="" if is_team(u) else company_name(u), is_team=is_team(u), body=body, images=images,
            )
            Topic.objects.filter(pk=t.pk).update(posts_count=F("posts_count") + 1, last_post_at=timezone.now())
            Follow.objects.get_or_create(topic=t, user=u)
        return Response(post_json(p), status=201)


class PostDeleteView(APIView):
    permission_classes = [InsiderAccess]

    def delete(self, request, pk):
        p = get_object_or_404(Post, pk=pk)
        if not (is_team(request.user) or p.author_id == request.user.id):
            return Response({"detail": "Можно удалить только своё сообщение."}, status=403)
        t = p.topic
        if t.posts.order_by("created_at", "id").first().id == p.id:
            return Response({"detail": "Первое сообщение темы удалить нельзя."}, status=400)
        p.delete()
        Topic.objects.filter(pk=t.pk).update(posts_count=F("posts_count") - 1)
        return Response(status=204)


class ToggleView(APIView):
    permission_classes = [InsiderAccess]
    model = None

    def post(self, request, pk):
        t = get_object_or_404(Topic, pk=pk)
        obj, created = self.model.objects.get_or_create(topic=t, user=request.user)
        if not created:
            obj.delete()
        return Response({"on": created, "count": self.model.objects.filter(topic=t).count()})


class MeTooView(ToggleView):
    model = MeToo


class FollowView(ToggleView):
    model = Follow


class UploadView(APIView):
    permission_classes = [InsiderAccess]
    throttle_classes = [WriteThrottle]

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": "Нет файла."})
        if f.size > MAX_IMAGE:
            raise ValidationError({"file": "Картинка больше 5 МБ."})
        from PIL import Image

        try:
            img = Image.open(f)
            img.verify()
            fmt = (img.format or "").upper()
        except Exception:
            raise ValidationError({"file": "Это не картинка."})
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt)
        if not ext:
            raise ValidationError({"file": "Нужна картинка JPG, PNG, WEBP или GIF."})
        f.seek(0)
        now = timezone.now()
        path = f"insiders/{now:%Y/%m}/{uuid.uuid4().hex}.{ext}"
        saved = default_storage.save(path, ContentFile(f.read()))
        return Response({"url": "https://app.nurcrm.kg/media/" + saved.replace(os.sep, "/")}, status=201)
