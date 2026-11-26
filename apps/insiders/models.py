# Инсайдеры support.nurcrm.kg (24.11.2026): новости NurCRM + форум замечаний.
# Читают и пишут команда (is_platform_admin) и владельцы магазинов. Только новые таблицы insiders_*.
from django.conf import settings
from django.db import models

SPHERES = [
    ("market", "Маркет"), ("kassa", "Касса"), ("usluga", "Услуги"), ("app", "Приложение"),
    ("site", "Сайт магазина"), ("advisor", "Нур Советник"), ("other", "Другое"),
]
TOPIC_STATUSES = [
    ("new", "Новое"), ("accepted", "Принято"), ("in_progress", "В работе"),
    ("done", "Сделано"), ("rejected", "Не будем делать"),
]
TOPIC_KINDS = [("bug", "Ошибка"), ("idea", "Идея"), ("question", "Вопрос")]


class Section(models.Model):
    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    hint = models.CharField(max_length=160, blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class News(models.Model):
    title = models.CharField(max_length=200)
    sphere = models.CharField(max_length=16, choices=SPHERES, default="market")
    kind = models.CharField(max_length=40, blank=True, default="Обновление")
    summary = models.CharField(max_length=400, blank=True, default="")
    body = models.TextField(blank=True, default="")
    points = models.JSONField(default=list, blank=True)     # короткие пункты списком
    examples = models.JSONField(default=list, blank=True)   # [{before, after, caption}] — ссылки на картинки
    audience = models.CharField(max_length=8, choices=[("all", "Все"), ("team", "Только команда")], default="all")
    status = models.CharField(max_length=10, choices=[("draft", "Черновик"), ("published", "Опубликовано")], default="draft")
    source = models.CharField(max_length=120, blank=True, default="")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]


class Topic(models.Model):
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="topics")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=TOPIC_KINDS, default="bug")
    status = models.CharField(max_length=12, choices=TOPIC_STATUSES, default="new", db_index=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    author_name = models.CharField(max_length=120, blank=True, default="")
    company_name = models.CharField(max_length=255, blank=True, default="")
    assignee = models.CharField(max_length=80, blank=True, default="")
    news = models.ForeignKey(News, null=True, blank=True, on_delete=models.SET_NULL, related_name="topics")
    posts_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_post_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-last_post_at"]


class Post(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    author_name = models.CharField(max_length=120, blank=True, default="")
    company_name = models.CharField(max_length=255, blank=True, default="")
    is_team = models.BooleanField(default=False)
    body = models.TextField()
    images = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class StatusEvent(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="events")
    old = models.CharField(max_length=12)
    new = models.CharField(max_length=12)
    by_name = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class MeToo(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="metoo")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        unique_together = [("topic", "user")]


class Follow(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="follows")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        unique_together = [("topic", "user")]
