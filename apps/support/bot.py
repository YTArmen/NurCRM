"""
Технический Telegram-бот команды NurMarket (ТЗ ч.7, п. 3.5).

Публичный помощник для других приложений (стабильная сигнатура):

    from apps.support.bot import send_team_alert
    send_team_alert("текст в HTML")   # -> bool, общий лимит 20 сообщений/час

Текст для send_team_alert — HTML (parse_mode=HTML): всё пользовательское
экранируйте через html.escape (или apps.support.bot.esc).
"""
import html
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import httpx
from django.conf import settings
from django.core.cache import cache
from django.db.models import Case, Count, IntegerField, Sum, Value, When
from django.utils import timezone

logger = logging.getLogger("support.bot")
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_TEXT = 4096

MSG_COUNT_KEY = "support_bot:msg_count"
MSG_WINDOW = 3600

CATEGORY_LABELS = {
    "payment": "Оплата",
    "shift": "Смена",
    "print": "Печать",
    "drawer": "Денежный ящик",
    "scales": "Весы",
    "sync": "Синхронизация",
    "offline": "Офлайн",
    "login": "Вход",
    "bot": "Бот",
    "update": "Обновление",
    "crash": "Падение",
    "ui": "Интерфейс",
    "other": "Прочее",
}
APP_LABELS = {"kassa": "касса", "owner": "программа владельца"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def fmt_dt(dt, pattern: str = "%d.%m %H:%M") -> str:
    if not dt:
        return "—"
    try:
        return timezone.localtime(dt).strftime(pattern)
    except Exception:
        return dt.strftime(pattern)


# ---------------------------------------------------------------- конфигурация

@dataclass
class SupportConfig:
    token: str
    chat_id: str
    webhook_secret: str
    enabled: bool
    hourly_msg_limit: int

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)


_CONFIG_CACHE: dict = {"at": 0.0, "cfg": None}
_CONFIG_TTL = 30.0


def _setting(name: str) -> str:
    value = getattr(settings, name, "") or os.getenv(name, "")
    return str(value).strip()


def get_support_config(force: bool = False) -> SupportConfig:
    """
    Настройки бота: строка SupportBotConfig (если поле заполнено), иначе
    settings/окружение SUPPORT_TELEGRAM_BOT_TOKEN, SUPPORT_TELEGRAM_CHAT_ID,
    SUPPORT_TELEGRAM_WEBHOOK_SECRET. Кэшируется в процессе на 30 с.
    """
    now = time.monotonic()
    if not force and _CONFIG_CACHE["cfg"] is not None and now - _CONFIG_CACHE["at"] < _CONFIG_TTL:
        return _CONFIG_CACHE["cfg"]

    row = None
    try:
        from apps.support.models import SupportBotConfig

        row = SupportBotConfig.objects.order_by("id").first()
    except Exception:
        logger.debug("SupportBotConfig unavailable", exc_info=True)

    cfg = SupportConfig(
        token=(row.token.strip() if row and row.token else "") or _setting("SUPPORT_TELEGRAM_BOT_TOKEN"),
        chat_id=(row.chat_id.strip() if row and row.chat_id else "") or _setting("SUPPORT_TELEGRAM_CHAT_ID"),
        webhook_secret=(row.webhook_secret.strip() if row and row.webhook_secret else "")
        or _setting("SUPPORT_TELEGRAM_WEBHOOK_SECRET"),
        enabled=bool(row.enabled) if row else True,
        hourly_msg_limit=(row.hourly_msg_limit if row and row.hourly_msg_limit else 20),
    )
    _CONFIG_CACHE.update(at=now, cfg=cfg)
    return cfg


# ---------------------------------------------------------------- Telegram API

def _truncate(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_TEXT:
        return text
    return text[: TELEGRAM_MAX_TEXT - 1] + "…"


def _send_raw(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id or not text:
        return False
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": _truncate(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram sendMessage failed: %s", data.get("description"))
        return bool(data.get("ok"))
    except Exception as exc:
        logger.error("Failed to send telegram message to team: %s", type(exc).__name__)
        return False


def send_document(token: str, chat_id: str, file_field, filename: str, caption: str = "") -> bool:
    if not token or not chat_id:
        return False
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendDocument"
    try:
        file_field.open("rb")
        try:
            content = file_field.read()
        finally:
            file_field.close()
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                url,
                data={"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"},
                files={"document": (filename, content, "application/zip")},
            )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram sendDocument failed: %s", data.get("description"))
        return bool(data.get("ok"))
    except Exception as exc:
        logger.error("Failed to send telegram document: %s", type(exc).__name__)
        return False


def _incr_counter(key: str, timeout: int) -> int:
    cache.add(key, 0, timeout)
    try:
        return int(cache.incr(key))
    except ValueError:
        # ключ истёк между add и incr
        cache.set(key, 1, timeout)
        return 1


def _active_critical_count() -> int:
    try:
        from apps.support.models import SupportIssue

        since = timezone.now() - timedelta(hours=1)
        return (
            SupportIssue.objects.filter(severity=SupportIssue.Severity.CRITICAL, last_seen__gte=since)
            .exclude(status=SupportIssue.Status.FIXED)
            .exclude(status=SupportIssue.Status.MUTED, muted_until__gt=timezone.now())
            .count()
        )
    except Exception:
        return 0


def send_team_alert(text: str) -> bool:
    """
    Отправляет сообщение (HTML) в Telegram-группу команды.
    Не больше hourly_msg_limit (по умолчанию 20) сообщений в час на все процессы;
    первое сверх лимита заменяется одним «⚠ Массовый сбой: N проблем, смотрите /top»,
    остальные отбрасываются. Возвращает True, если сообщение ушло.
    Никогда не бросает исключений.
    """
    try:
        cfg = get_support_config()
        if not cfg.ready:
            logger.info("Support bot not configured or disabled; alert skipped: %s", (text or "")[:80])
            return False

        limit = max(1, int(cfg.hourly_msg_limit or 20))
        n = _incr_counter(MSG_COUNT_KEY, MSG_WINDOW)
        if n <= limit:
            return _send_raw(cfg.token, cfg.chat_id, text)
        if n == limit + 1:
            problems = _active_critical_count()
            _send_raw(
                cfg.token,
                cfg.chat_id,
                f"⚠ <b>Массовый сбой: {problems} проблем</b>, смотрите /top",
            )
        logger.warning("Support bot hourly limit exceeded (%d/%d); alert dropped", n, limit)
        return False
    except Exception:
        logger.exception("send_team_alert failed")
        return False


# ---------------------------------------------------------------- тексты оповещений

def _category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category or "", category or "—")


def format_new_critical_alert(issue, report) -> str:
    shop = issue.sample_company_name or "магазин не определён"
    login = issue.sample_login or "—"
    app = APP_LABELS.get(report.app, report.app or "—")
    return (
        f"🔴 <b>{esc(_category_label(issue.category))}: {esc(issue.title[:120])}</b>\n"
        f"Магазинов: {issue.companies_count} · случаев: {issue.occurrences} · "
        f"версия {esc(report.version)} · с {fmt_dt(issue.first_seen, '%H:%M')}\n"
        f"Пример: «{esc(shop)}», {esc(app)}, логин {esc(login)}\n"
        f"/issue_{issue.id}"
    )


def format_surge_alert(issue, hour_shops: int, hour_cases: int) -> str:
    return (
        f"📈 <b>Всплеск: {esc(_category_label(issue.category))}: {esc(issue.title[:120])}</b>\n"
        f"За час: магазинов {hour_shops} · случаев {hour_cases}\n"
        f"Всего: магазинов {issue.companies_count} · случаев {issue.occurrences}\n"
        f"/issue_{issue.id}"
    )


def format_regression_alert(issue, version: str) -> str:
    return (
        f"♻️ <b>Возврат исправленной ошибки</b>\n"
        f"{esc(_category_label(issue.category))}: {esc(issue.title[:120])}\n"
        f"Исправлена в {esc(issue.fixed_version or '—')}, снова появилась в {esc(version)}\n"
        f"Магазинов: {issue.companies_count} · случаев: {issue.occurrences}\n"
        f"/issue_{issue.id}"
    )


def format_version_worse_alert(app: str, version: str, prev_version: str,
                               new_share: float, prev_share: float,
                               new_crit: int, new_total: int) -> str:
    return (
        f"📉 <b>Новая версия хуже прошлой</b>: {esc(APP_LABELS.get(app, app))} {esc(version)}\n"
        f"Магазинов с критичными ошибками: {new_share:.0%} ({new_crit} из {new_total}) "
        f"против {prev_share:.0%} в {esc(prev_version)}\n"
        f"/version {esc(version)}"
    )


# ---------------------------------------------------------------- команды

HELP_TEXT = (
    "<b>Команды технического бота:</b>\n"
    "/top — главные открытые проблемы\n"
    "/issue &lt;id&gt; — подробно, пример стека, магазины\n"
    "/company &lt;slug&gt; — проблемы магазина за 7 дней\n"
    "/version &lt;1.17.38&gt; — проблемы версии\n"
    "/ack &lt;id&gt; — взять в работу\n"
    "/fixed &lt;id&gt; &lt;версия&gt; — исправлена в версии\n"
    "/mute &lt;id&gt; 24h — заглушить (m/h/d)\n"
    "/report &lt;client_report_id&gt; — zip журналов, если приложен"
)


def _parse_duration(value: str) -> Optional[timedelta]:
    import re

    m = re.fullmatch(r"(\d{1,4})([mhd]?)", (value or "").strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "h"
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    if delta <= timedelta(0) or delta > timedelta(days=365):
        return None
    return delta


def _parse_int(value: str) -> Optional[int]:
    try:
        return int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return None


def handle_support_bot_command(text: str, chat_id: Optional[str] = None) -> Optional[str]:
    """
    Обрабатывает команду и возвращает HTML-ответ. Команды вида /top@BotName
    поддерживаются. Для /report при переданном chat_id zip отправляется документом.
    Возвращает None, если отвечать не нужно (не команда).
    """
    from apps.support.models import SupportIssue

    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None

    cmd = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if cmd.startswith("/issue_"):
        issue_id = _parse_int(cmd[len("/issue_"):])
        return get_issue_details(issue_id) if issue_id else "Неверный номер проблемы."

    if cmd == "/req":  # П2-6 p2614: статус обращения
        from apps.support.requests import handle_req_command

        return handle_req_command(args)

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/top":
        return get_top_issues()

    if cmd == "/issue":
        issue_id = _parse_int(args[0]) if args else None
        return get_issue_details(issue_id) if issue_id else "Использование: /issue &lt;id&gt;"

    if cmd == "/company":
        return get_company_issues(args[0]) if args else "Использование: /company &lt;slug&gt;"

    if cmd == "/version":
        return get_version_issues(args[0]) if args else "Использование: /version &lt;1.17.38&gt;"

    if cmd == "/ack":
        issue_id = _parse_int(args[0]) if args else None
        if not issue_id:
            return "Использование: /ack &lt;id&gt;"
        updated = SupportIssue.objects.filter(id=issue_id).update(
            status=SupportIssue.Status.ACKNOWLEDGED, muted_until=None
        )
        return f"✅ Проблема #{issue_id} взята в работу." if updated else f"Проблема #{issue_id} не найдена."

    if cmd == "/fixed":
        from apps.support.rules import is_valid_version

        issue_id = _parse_int(args[0]) if args else None
        fix_ver = args[1].strip() if len(args) > 1 else ""
        if not issue_id or not fix_ver:
            return "Использование: /fixed &lt;id&gt; &lt;версия&gt;, например /fixed 412 1.17.38"
        if not is_valid_version(fix_ver):
            return f"Неверная версия: {esc(fix_ver)}"
        updated = SupportIssue.objects.filter(id=issue_id).update(
            status=SupportIssue.Status.FIXED,
            fixed_version=fix_ver[:64],
            fixed_at=timezone.now(),
            muted_until=None,
        )
        if not updated:
            return f"Проблема #{issue_id} не найдена."
        return f"✅ Проблема #{issue_id} исправлена в версии {esc(fix_ver)}."

    if cmd == "/mute":
        issue_id = _parse_int(args[0]) if args else None
        delta = _parse_duration(args[1]) if len(args) > 1 else timedelta(hours=24)
        if not issue_id or delta is None:
            return "Использование: /mute &lt;id&gt; 24h (единицы: m, h, d)"
        until = timezone.now() + delta
        updated = SupportIssue.objects.filter(id=issue_id).update(
            status=SupportIssue.Status.MUTED, muted_until=until
        )
        if not updated:
            return f"Проблема #{issue_id} не найдена."
        return f"🔇 Проблема #{issue_id} заглушена до {fmt_dt(until)}."

    if cmd == "/report":
        return get_report(args[0] if args else "", chat_id)

    return HELP_TEXT


def get_top_issues() -> str:
    from apps.support.models import SupportIssue

    now = timezone.now()
    issues = list(
        SupportIssue.objects.filter(
            status__in=[SupportIssue.Status.NEW, SupportIssue.Status.ACKNOWLEDGED, SupportIssue.Status.MUTED]
        )
        .exclude(status=SupportIssue.Status.MUTED, muted_until__gt=now)
        .filter(last_seen__gte=now - timedelta(days=7))
        .annotate(_crit=Case(When(severity=SupportIssue.Severity.CRITICAL, then=Value(0)),
                             default=Value(1), output_field=IntegerField()))
        .order_by("_crit", "-companies_count", "-occurrences")[:10]
    )
    if not issues:
        return "🎉 Открытых проблем за 7 дней нет."

    icons = {"critical": "🔴", "error": "🟠", "warning": "🟡"}
    lines = ["<b>Главные открытые проблемы:</b>"]
    for iss in issues:
        lines.append(
            f"{icons.get(iss.severity, '⚪')} /issue_{iss.id} {esc(_category_label(iss.category))}: "
            f"{esc(iss.title[:70])}\n"
            f"    магазинов {iss.companies_count} · случаев {iss.occurrences} · {esc(iss.get_status_display())}"
        )
    return "\n".join(lines)


def get_issue_details(issue_id: int) -> str:
    from apps.support.models import SupportErrorReport, SupportIssue

    issue = SupportIssue.objects.filter(id=issue_id).first()
    if not issue:
        return f"Проблема #{issue_id} не найдена."

    status = esc(issue.get_status_display())
    if issue.status == SupportIssue.Status.FIXED and issue.fixed_version:
        status += f" в {esc(issue.fixed_version)}"
    if issue.status == SupportIssue.Status.MUTED and issue.muted_until:
        status += f" до {fmt_dt(issue.muted_until)}"

    lines = [
        f"<b>Проблема #{issue.id}</b> · {esc(issue.get_severity_display())}",
        f"{esc(_category_label(issue.category))}: {esc(issue.title[:300])}",
        f"Статус: {status}",
        f"Магазинов: {issue.companies_count} · случаев: {issue.occurrences}",
        f"Версии: {esc(', '.join(issue.versions or []) or '—')}",
        f"Впервые: {fmt_dt(issue.first_seen)} · последний раз: {fmt_dt(issue.last_seen)}",
    ]

    shops = list(
        SupportErrorReport.objects.filter(issue=issue, company__isnull=False)
        .order_by()
        .values("company__name", "company__slug")
        .annotate(cases=Sum("count"))
        .order_by("-cases")[:15]
    )
    if shops:
        lines.append("\n<b>Магазины:</b>")
        for s in shops:
            lines.append(f"• {esc(s['company__name'])} ({esc(s['company__slug'])}) — {s['cases']}")
        if issue.companies_count > len(shops):
            lines.append(f"…и ещё {issue.companies_count - len(shops)}")

    if issue.sample_stack:
        stack = issue.sample_stack[:1500]
        lines.append(f"\n<b>Пример стека:</b>\n<pre>{esc(stack)}</pre>")
    return "\n".join(lines)


def get_company_issues(slug_or_name: str) -> str:
    from apps.support.models import SupportErrorReport
    from apps.users.models import Company

    comp = (
        Company.objects.filter(slug__iexact=slug_or_name).first()
        or Company.objects.filter(name__icontains=slug_or_name).order_by("name").first()
    )
    if not comp:
        return f"Магазин «{esc(slug_or_name)}» не найден."

    since = timezone.now() - timedelta(days=7)
    rows = list(
        SupportErrorReport.objects.filter(company=comp, last_at__gte=since, issue__isnull=False)
        .order_by()
        .values("issue_id", "issue__title", "issue__category", "issue__severity")
        .annotate(cases=Sum("count"))
        .order_by("-cases")[:15]
    )
    if not rows:
        return f"У магазина «{esc(comp.name)}» за 7 дней ошибок нет."

    lines = [f"<b>Магазин «{esc(comp.name)}» ({esc(comp.slug)}) — 7 дней:</b>"]
    for r in rows:
        icon = "🔴" if r["issue__severity"] == "critical" else "•"
        lines.append(
            f"{icon} /issue_{r['issue_id']} {esc(_category_label(r['issue__category']))}: "
            f"{esc((r['issue__title'] or '')[:70])} — {r['cases']}"
        )
    return "\n".join(lines)


def get_version_issues(version: str) -> str:
    from apps.support.models import SupportErrorReport

    since = timezone.now() - timedelta(days=14)
    qs = SupportErrorReport.objects.filter(version=version, created_at__gte=since)
    total_shops = qs.filter(company__isnull=False).values("company_id").distinct().count()
    rows = list(
        qs.filter(issue__isnull=False)
        .order_by()
        .values("issue_id", "issue__title", "issue__category", "issue__severity")
        .annotate(shops=Count("company", distinct=True), cases=Sum("count"))
        .order_by("-shops", "-cases")[:15]
    )
    if not rows:
        return f"Для версии {esc(version)} за 14 дней ошибок нет."

    lines = [f"<b>Версия {esc(version)}</b> — магазинов с ошибками: {total_shops}"]
    for r in rows:
        icon = "🔴" if r["issue__severity"] == "critical" else "•"
        lines.append(
            f"{icon} /issue_{r['issue_id']} {esc(_category_label(r['issue__category']))}: "
            f"{esc((r['issue__title'] or '')[:60])} — магазинов {r['shops']}, случаев {r['cases']}"
        )
    return "\n".join(lines)


def get_report(client_report_id: str, chat_id: Optional[str]) -> str:
    import uuid

    from apps.support.models import SupportErrorReport, SupportReportAttachment

    try:
        rid = uuid.UUID(str(client_report_id).strip())
    except (ValueError, AttributeError):
        return "Использование: /report &lt;client_report_id&gt;"

    report = SupportErrorReport.objects.filter(client_report_id=rid).select_related("company", "issue").first()
    att = SupportReportAttachment.objects.filter(client_report_id=rid).first()
    if not report and not att:
        return f"Отчёт {esc(rid)} не найден."

    head = f"Отчёт {esc(rid)}"
    if report:
        shop = report.company.name if report.company else "без компании"
        head += (
            f"\n{esc(_category_label(report.category))} · {esc(report.level)} · "
            f"{esc(APP_LABELS.get(report.app, report.app))} {esc(report.version)} · «{esc(shop)}»"
        )
        if report.issue_id:
            head += f"\n/issue_{report.issue_id}"

    if not att or not att.file:
        return head + "\nZIP не приложен."

    cfg = get_support_config()
    if chat_id and cfg.token:
        ok = send_document(cfg.token, chat_id, att.file, f"report-{rid}.zip", caption=head)
        if ok:
            return "📎 ZIP отправлен."
        return head + "\nНе удалось отправить ZIP (см. логи сервера)."
    return head + "\nZIP приложен."
