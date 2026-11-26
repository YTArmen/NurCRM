"""
Страница ссылки-приглашения https://app.nurcrm.kg/r/<код> (ТЗ 14.10, раздел 3).

Пробует открыть приложение по nurcrmapp://invite/<код>; если приложения нет — ссылка на установку
и сам код, чтобы ввести вручную. Без внешних скриптов и трекеров; код проверяется по алфавиту
кодов приглашения, в HTML попадает только безопасный текст.
"""
import re

from django.conf import settings
from django.http import HttpResponse
from django.utils.html import escape
from django.views import View

from .models import REFERRAL_ALPHABET

CODE_RE = re.compile(r"^[A-Za-z0-9]{4,16}$")
APP_SCHEME = "nurcrmapp"
PLAY_URL = "https://play.google.com/store/apps/details?id=kg.nurcrm.client"


def _is_valid_code(code: str) -> bool:
    code = (code or "").upper()
    return bool(CODE_RE.match(code)) and all(ch in REFERRAL_ALPHABET for ch in code)


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>NurCRM Бонусы — приглашение</title>
<style>
  body{{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#f4f6f9;color:#111;}}
  .card{{max-width:420px;margin:48px auto;padding:28px 24px;background:#fff;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.06);text-align:center}}
  h1{{font-size:22px;margin:0 0 8px}} p{{margin:8px 0;color:#444;line-height:1.45}}
  .code{{display:inline-block;margin:14px 0;padding:12px 20px;font-size:28px;letter-spacing:4px;font-weight:700;background:#f0f3f8;border-radius:12px}}
  a.btn{{display:block;margin:12px 0;padding:14px;border-radius:12px;text-decoration:none;font-weight:600;background:#1f6feb;color:#fff}}
  a.secondary{{background:#eef2f7;color:#1f3b63}} small{{color:#777}}
</style>
</head>
<body>
<div class="card">
  <h1>Вас пригласили в NurCRM Бонусы</h1>
  <p>Код приглашения:</p>
  <div class="code">{code}</div>
  <a class="btn" href="{deeplink}">Открыть в приложении</a>
  <a class="btn secondary" href="{play}">Установить из Google Play</a>
  <p><small>Если приложение не открылось, установите его и введите код при первом входе.</small></p>
  <p lang="ky"><small>Колдонмо ачылбаса, аны орнотуп, биринчи киргенде кодду жазыңыз.</small></p>
</div>
<script>
  // без трекеров: единственная попытка открыть приложение по схеме
  (function(){{ try {{ setTimeout(function(){{ window.location.href = "{deeplink}"; }}, 150); }} catch (e) {{}} }})();
</script>
</body>
</html>
"""

NOT_FOUND = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>NurCRM Бонусы</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;text-align:center;padding:48px 16px">
<h1 style="font-size:20px">Ссылка-приглашение неверна</h1><p>Попросите друга прислать ссылку ещё раз.</p>
<p><a href="{play}">NurCRM Бонусы в Google Play</a></p></body></html>"""


class ReferralLandingView(View):
    def get(self, request, code=""):
        play = getattr(settings, "CLIENT_APP_PLAY_URL", "") or PLAY_URL
        if not _is_valid_code(code):
            return HttpResponse(NOT_FOUND.format(play=escape(play)), status=404, content_type="text/html; charset=utf-8")
        code = code.upper()
        html = PAGE.format(code=escape(code), deeplink=f"{APP_SCHEME}://invite/{code}", play=escape(play))
        resp = HttpResponse(html, content_type="text/html; charset=utf-8")
        resp["Cache-Control"] = "public, max-age=3600"
        resp["X-Robots-Tag"] = "noindex"
        return resp
