"""
База знаний техподдержки NurCRM: поиск по Markdown-файлам (# Тема, ## Как …?, шаги).
Один и тот же файл работает в Нур Советнике (сервер NurCRM, apps/main/kb_search.py, папка /home/nur/kb_cache)
и в боте поддержки на домашнем сервере Compaq (/srv/support/kb_search.py, папка /srv/files/kb).
Без зависимостей и без ИИ: разбиваем на разделы и считаем совпадение слов (грубая «основа» — первые 5 букв).
"""
import math
import os
import re

_WORD = re.compile(r"[a-zа-яёөүң0-9]+", re.I)
_STOP = set("""как где что это для или при без над под про его еще ещё уже так вот мне мой моя мои наш наша мы вы
они она оно они нам вам чтобы если можно нужно надо есть был была были будет сделать сделаю делать почему какой какая
какие когда кто чем через из на по от до за не нет да ли же бы то все всё весь сам свой своя свои the and how what""".split())
_cache = {"key": None, "sections": []}


def _stems(text):
    out = []
    for w in _WORD.findall((text or "").lower().replace("ё", "е")):
        if len(w) < 3 or w in _STOP:
            continue
        out.append(w[:5])
    return out


def _load(folder):
    files = []
    try:
        for name in sorted(os.listdir(folder)):
            if name.endswith(".md"):
                p = os.path.join(folder, name)
                files.append((name, os.path.getmtime(p), os.path.getsize(p)))
    except OSError:
        return []
    key = (folder, tuple(files))
    if _cache["key"] == key:
        return _cache["sections"]
    sections = []
    for name, _m, size in files:
        if size > 200_000:
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        topic = name[:-3]
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            topic = m.group(1).strip()
        for part in re.split(r"^(?=##\s)", text, flags=re.M):
            part = part.strip()
            if not part.startswith("##"):
                continue
            title = part.splitlines()[0].lstrip("#").strip()
            body = part[:2500]
            sections.append({"file": name, "topic": topic, "title": title, "text": body,
                             "tp": set(_stems(topic)), "t": set(_stems(title)), "b": set(_stems(body))})
    # редкие слова весят больше частых («возврат» важнее «товара»)
    df = {}
    for sec in sections:
        for w in sec["t"] | sec["tp"] | sec["b"]:
            df[w] = df.get(w, 0) + 1
    n = max(len(sections), 1)
    _cache.update(key=key, sections=sections, idf={w: math.log(1 + n / c) for w, c in df.items()})
    return sections


def search(folder, query, limit=3):
    """[{"topic", "title", "text", "score"}] — лучшие разделы по вопросу; пусто, если ничего похожего."""
    q = set(_stems(query))
    if not q:
        return []
    scored = []
    secs = _load(folder)
    idf = _cache.get("idf") or {}
    q = {w for w in q if w in idf}
    if not q:
        return []
    for s in secs:
        score = sum(idf[w] * (3 * (w in s["t"]) + 2 * (w in s["tp"]) + (w in s["b"])) for w in q)
        if score >= 4:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [{"topic": s["topic"], "title": s["title"], "text": s["text"], "score": round(sc, 1)} for sc, s in scored[:limit]]


def count(folder):
    return len(_load(folder))
