"""Сумма прописью по-русски: сомы и тыйыны (деньги в тыйынах)."""

_ONES_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
         "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот",
             "девятьсот"]
# (форма для 1, для 2-4, для 5+, женский род)
_GROUPS = [
    None,
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
]


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _triplet(n: int, feminine: bool) -> list:
    out = []
    if n >= 100:
        out.append(_HUNDREDS[n // 100])
    n %= 100
    if 10 <= n < 20:
        out.append(_TEENS[n - 10])
    else:
        if n >= 20:
            out.append(_TENS[n // 10])
        d = n % 10
        if d:
            out.append((_ONES_F if feminine else _ONES_M)[d])
    return out


def number_to_words(n: int, feminine: bool = False) -> str:
    n = int(n)
    if n == 0:
        return "ноль"
    parts = []
    idx = 0
    neg = n < 0
    n = abs(n)
    while n > 0:
        n, chunk = divmod(n, 1000)
        if chunk:
            if idx == 0:
                parts = _triplet(chunk, feminine) + parts
            else:
                g = _GROUPS[idx]
                parts = _triplet(chunk, g[3]) + [plural(chunk, g[0], g[1], g[2])] + parts
        idx += 1
    return ("минус " if neg else "") + " ".join(parts)


def som_to_words(tyiyn: int) -> str:
    """12550 -> 'сто двадцать пять сомов пятьдесят тыйынов' (тыйыны, если не ноль)."""
    tyiyn = abs(int(tyiyn))
    som, ty = divmod(tyiyn, 100)
    text = f"{number_to_words(som)} {plural(som, 'сом', 'сома', 'сомов')}"
    if ty:
        text += f" {number_to_words(ty)} {plural(ty, 'тыйын', 'тыйына', 'тыйынов')}"
    return text


def fmt_som(tyiyn: int) -> str:
    """12550000 -> '125 500' ; 12550 -> '125,50' (пробел-разделитель тысяч)."""
    tyiyn = int(tyiyn)
    sign = "-" if tyiyn < 0 else ""
    som, ty = divmod(abs(tyiyn), 100)
    s = f"{som:,}".replace(",", " ")
    return f"{sign}{s}" + (f",{ty:02d}" if ty else "")
