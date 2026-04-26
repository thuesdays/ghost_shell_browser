"""
query_expander.py -- Long-tail commercial query expansion.

Why this exists:
  Brand-only queries ("гудмедика") are classified as navigational
  intent by Google -> SERPs come back with very few or zero ads.
  Long-tail commercial variants ("гудмедика крем купити", "гудмедика
  ціна київ") trigger commercial-intent inference and get 3-10x more
  ad density -- both classic text ads AND shopping carousels.

  The expander generates these on the fly. Pure Python -- no browser,
  no API. The runtime calls expand(brand) when it needs a richer
  query set than the user originally typed.

Usage:
    from ghost_shell.actions.query_expander import expand_query, COMMERCIAL_SUFFIXES_UA

    # Default: 5 random long-tails for a UA-locale profile
    queries = expand_query("гудмедика", locale="UA", n=5)
    # ['гудмедика крем', 'гудмедика купити', 'гудмедика відгуки',
    #  'гудмедика ціна київ', 'гудмедика instagram']

    # Mixed-locale: pulls from both UA and RU pools
    queries = expand_query("гудмедика", locale="UA+RU", n=8)

    # Deterministic via seed (for reproducible runs / testing)
    queries = expand_query("гудмедика", locale="UA", n=5,
                           seed="profile_01:2026-04-26")
"""

__author__ = "Mykola Kovhanko"
__email__  = "thuesdays@gmail.com"

import random
from typing import Iterable


# ────────────────────────────────────────────────────────────────
# Suffix packs by locale
# ────────────────────────────────────────────────────────────────
# Curated for UA-commerce monitoring. Each entry is appended to the
# brand verbatim (no automatic stemming -- we keep the bare brand
# token so search intent stays focused). Mix of:
#   - product modifiers: крем, лікування, обладнання
#   - intent verbs: купити, замовити
#   - price/comparison: ціна, відгуки, відгук
#   - geo: київ, харків, львів, україна
#   - channel: instagram, telegram, сайт
#   - long-tail comparison: vs, аналог, замінник

COMMERCIAL_SUFFIXES_UA = [
    "купити", "ціна", "відгуки", "відгук",
    "доставка", "оплата", "розстрочка",
    "київ", "харків", "львів", "одеса", "дніпро", "україна",
    "каталог", "інтернет магазин", "магазин",
    "акція", "знижка", "знижки",
    "instagram", "telegram", "офіційний сайт", "сайт",
    "аналог", "замінник", "відгуки клієнтів",
    "як замовити", "як купити",
]

# Russian-language suffixes (some UA profiles still see RU-search
# queries because of bilingual users). Keeps brand visibility broader.
COMMERCIAL_SUFFIXES_RU = [
    "купить", "цена", "отзывы",
    "доставка", "оплата", "рассрочка",
    "киев", "харьков", "львов", "одесса", "украина",
    "каталог", "интернет магазин", "магазин",
    "акция", "скидка", "скидки",
    "instagram", "telegram", "официальный сайт", "сайт",
    "аналог", "заменитель",
    "отзывы покупателей", "как заказать",
]

# English (fallback or international targeting)
COMMERCIAL_SUFFIXES_EN = [
    "buy", "price", "reviews", "review",
    "delivery", "shipping", "discount", "sale",
    "online store", "shop", "official",
    "instagram", "alternative", "vs",
    "kiev", "kyiv", "ukraine",
]

# Category-specific suffixes layered on top of generic commercial ones.
# Heuristic: if the brand looks medical (medika/medical/health/pharma)
# we add medical suffixes; if it looks beauty/cosmetic, beauty ones.
# Keeps the long-tail relevant instead of generic.

CATEGORY_SUFFIXES_UA = {
    "medical": [
        "крем", "мазь", "ліки",
        "медичне обладнання", "обладнання", "інструменти",
        "інтернет аптека", "аптека",
    ],
    "beauty": [
        "крем для обличчя", "крем для рук",
        "косметика", "догляд за шкірою",
        "органічна косметика", "натуральна косметика",
    ],
    "tech": [
        "характеристики", "огляд", "тести",
        "навушники", "ноутбук", "телефон",
    ],
    "auto": [
        "запчастини", "оригінал", "аналог",
        "купити в україні", "ціна київ",
    ],
}

# Heuristic mapping: keywords in brand -> category. Multi-language
# stems intentionally inclusive so we catch brand misspellings.
_BRAND_KEYWORDS = {
    "medical": ["med", "медик", "medika", "medica", "медиц", "medic",
                "pharma", "аптек", "apteka", "клиника", "klinika",
                "здоров", "health"],
    "beauty":  ["beauty", "krasot", "krasa", "красот", "cosmet",
                "космет", "крем", "krem", "skin", "шкір", "lotion"],
    "tech":    ["tech", "техно", "soft", "tronic", "lab", "labs",
                "digital", "цифр"],
    "auto":    ["auto", "авто", "moto", "tyre", "шин", "колес"],
}


def detect_category(brand: str) -> str | None:
    """Best-guess product category from the brand string.

    Returns one of "medical" / "beauty" / "tech" / "auto" / None
    (None means "no strong signal -- use only generic commercial
    suffixes"). Case-insensitive substring match against the
    _BRAND_KEYWORDS table.
    """
    if not brand:
        return None
    b = brand.lower()
    for cat, keywords in _BRAND_KEYWORDS.items():
        if any(k in b for k in keywords):
            return cat
    return None


def _resolve_pool(locale: str, brand: str) -> list[str]:
    """Build the suffix pool given a locale spec ("UA", "UA+RU",
    "UA+RU+EN", "EN") plus category-specific suffixes derived from
    the brand. Returns a flat list, deduplicated, preserving the
    locale-priority order."""
    parts = [p.strip().upper() for p in (locale or "UA").split("+")]
    pool: list[str] = []
    seen: set[str] = set()

    def add(items: Iterable[str]):
        for s in items:
            if s and s not in seen:
                seen.add(s); pool.append(s)

    locale_map = {
        "UA": COMMERCIAL_SUFFIXES_UA,
        "RU": COMMERCIAL_SUFFIXES_RU,
        "EN": COMMERCIAL_SUFFIXES_EN,
    }
    for loc in parts:
        if loc in locale_map:
            add(locale_map[loc])

    # Category-specific layered on top -- only UA pool for now since
    # that's our primary target. Easy to extend later.
    cat = detect_category(brand)
    if cat and cat in CATEGORY_SUFFIXES_UA:
        add(CATEGORY_SUFFIXES_UA[cat])

    return pool


def expand_query(brand: str,
                 *,
                 locale: str = "UA",
                 n: int = 5,
                 include_brand: bool = True,
                 seed: str | None = None) -> list[str]:
    """Generate N long-tail commercial variants of a brand query.

    Args:
      brand:         The base brand or product token.
      locale:        Locale spec, e.g. "UA", "UA+RU", "UA+RU+EN".
      n:             How many variants to return.
      include_brand: If True (default), the bare brand is included
                     as the FIRST result. The user's monitor still
                     wants to see what the bare brand SERP looks like.
      seed:          Optional rng seed for deterministic output.

    Returns:
      List of query strings, length min(n, available_pool + 1).
    """
    if not brand or not brand.strip():
        return []
    brand = brand.strip()

    pool = _resolve_pool(locale, brand)
    rng = random.Random(seed) if seed else random.Random()
    rng.shuffle(pool)

    # Reserve slot for bare brand if requested
    suffix_count = max(0, n - (1 if include_brand else 0))
    chosen_suffixes = pool[:suffix_count]

    results: list[str] = []
    if include_brand:
        results.append(brand)
    for s in chosen_suffixes:
        results.append(f"{brand} {s}")
    return results


def expand_many(brands: list[str],
                *,
                locale: str = "UA",
                per_brand: int = 3,
                seed: str | None = None) -> list[str]:
    """Expand a list of brands into a flat shuffled query list.

    Useful when the user has 2-3 brand spellings and wants long-tail
    variants of all of them mixed together for a single run.
    """
    rng = random.Random(seed) if seed else random.Random()
    out: list[str] = []
    for b in brands or []:
        out.extend(expand_query(b, locale=locale, n=per_brand, seed=seed))
    rng.shuffle(out)
    return out


# Convenience alias for runtime callers that just want one knob.
def commercial_inflate_queries(brand: str,
                               n_pre: int = 2,
                               locale: str = "UA",
                               seed: str | None = None) -> list[str]:
    """Pick N short commercial pre-search queries to fire BEFORE the
    main brand search. Goal: warms up Google's recent-query
    commercial-intent context so the subsequent brand SERP comes back
    with denser ads.

    Picks generic commercial searches (no brand reference) -- they
    look like a user shopping in the same vertical as the brand,
    without yet revealing which brand they ultimately want. Examples
    for a beauty brand: "крем для обличчя купити", "косметика онлайн
    магазин київ".
    """
    cat = detect_category(brand) or "medical"  # safe default
    base = {
        "medical": [
            "купити медичне обладнання",
            "медичне обладнання київ",
            "інтернет аптека україна",
            "ліки замовити онлайн",
        ],
        "beauty": [
            "крем для обличчя купити",
            "косметика онлайн магазин",
            "органічна косметика україна",
            "догляд за шкірою бренди",
        ],
        "tech": [
            "купити ноутбук інтернет магазин",
            "техніка для дому акції",
            "знижки на електроніку",
        ],
        "auto": [
            "купити шини інтернет магазин",
            "автозапчастини україна",
        ],
    }
    pool = list(base.get(cat, base["medical"]))
    rng = random.Random(seed) if seed else random.Random()
    rng.shuffle(pool)
    return pool[:max(1, n_pre)]
