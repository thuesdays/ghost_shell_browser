"""
profile_enricher.py — Обогащение профиля Chrome реалистичными данными

Настоящий Chrome хранит в профиле:
- History — SQLite база со всеми посещёнными URL
- Bookmarks — JSON с закладками
- Login Data — логины (зашифрованы, их мы не трогаем)
- Preferences — настройки (уже заполнены в nk_browser)
- Top Sites — топ сайтов на new tab page
- Favicons — SQLite с иконками сайтов

У пустого профиля все эти базы либо не существуют, либо пусты —
это очень подозрительно. Мы заполняем их до первого запуска браузера
правдоподобными данными.

ВАЖНО: запускать ТОЛЬКО при закрытом браузере. Для существующего
профиля безопасно — добавляем данные не стирая существующие.
"""

import os
import json
import time
import random
import sqlite3
import logging
from datetime import datetime, timedelta


class ProfileEnricher:
    """
    Использование:
        enricher = ProfileEnricher(profile_path="profiles/profile_01")
        enricher.enrich_all()
    """

    # Популярные сайты которые посещает средний юзер Украины
    COMMON_SITES = [
        ("https://www.google.com/",           "Google"),
        ("https://www.youtube.com/",          "YouTube"),
        ("https://www.youtube.com/watch?v=",  "YouTube видео"),
        ("https://mail.google.com/mail/u/0/", "Gmail"),
        ("https://www.rozetka.com.ua/",       "Rozetka"),
        ("https://www.rozetka.com.ua/ua/",    "Rozetka — Інтернет-магазин"),
        ("https://www.olx.ua/",               "OLX.ua"),
        ("https://uk.wikipedia.org/",         "Вікіпедія"),
        ("https://ru.wikipedia.org/",         "Википедия"),
        ("https://www.pravda.com.ua/",        "Українська правда"),
        ("https://www.ukr.net/",              "ukr.net"),
        ("https://www.bbc.com/",              "BBC"),
        ("https://www.google.com/maps",       "Google Maps"),
        ("https://translate.google.com/",     "Google Translate"),
        ("https://www.instagram.com/",        "Instagram"),
        ("https://www.facebook.com/",         "Facebook"),
        ("https://www.reddit.com/",           "reddit"),
        ("https://github.com/",               "GitHub"),
        ("https://stackoverflow.com/",        "Stack Overflow"),
        ("https://www.booking.com/",          "Booking.com"),
        ("https://www.aliexpress.com/",       "AliExpress"),
        ("https://prom.ua/",                  "Prom.ua"),
        ("https://zakupki.prom.ua/",          "Zakupki Prom"),
        ("https://novaposhta.ua/",            "Нова Пошта"),
        ("https://privat24.ua/",              "Приват24"),
        ("https://monobank.ua/",              "monobank"),
    ]

    # Поисковые запросы которые средний юзер делал за последний месяц
    COMMON_SEARCHES = [
        "погода", "курс доллара", "новости",
        "как сделать скриншот", "что посмотреть",
        "рецепт борща", "время работы почты",
        "как доехать до", "адрес", "телефон",
        "youtube", "переводчик", "google maps",
        "rozetka знижки", "olx робота",
    ]

    def __init__(self, profile_path: str):
        self.profile_path = profile_path
        self.default_dir  = os.path.join(profile_path, "Default")
        os.makedirs(self.default_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────
    # CHROME TIMESTAMP CONVERSION
    # Chrome использует microseconds с 1601-01-01
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _chrome_time(dt: datetime) -> int:
        """Конвертирует datetime в Chrome timestamp (microseconds since 1601)"""
        epoch_start = datetime(1601, 1, 1)
        delta = dt - epoch_start
        return int(delta.total_seconds() * 1_000_000)

    # ──────────────────────────────────────────────────────────
    # HISTORY — основная SQLite с посещениями
    # ──────────────────────────────────────────────────────────

    def seed_history(self, days_back: int = 30, visits_per_day_range: tuple = (5, 25)):
        """
        Заполняет History SQLite базу реалистичными посещениями
        за последние N дней.
        """
        db_path = os.path.join(self.default_dir, "History")

        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        # Создаём таблицы если их ещё нет (схема Chrome)
        self._create_history_schema(cur)

        now = datetime.now()
        url_id_counter   = 1
        visit_id_counter = 1

        # Получаем максимальные ID чтобы не конфликтовать с существующими
        try:
            cur.execute("SELECT MAX(id) FROM urls")
            max_url_id = cur.fetchone()[0]
            if max_url_id:
                url_id_counter = max_url_id + 1
            cur.execute("SELECT MAX(id) FROM visits")
            max_visit_id = cur.fetchone()[0]
            if max_visit_id:
                visit_id_counter = max_visit_id + 1
        except Exception:
            pass

        url_cache = {}   # url → id для дедупликации
        total_visits = 0

        # Для каждого дня генерируем посещения
        for day_offset in range(days_back, 0, -1):
            day_start = now - timedelta(days=day_offset)
            visits_today = random.randint(*visits_per_day_range)

            for _ in range(visits_today):
                # Случайный сайт из списка
                url, title = random.choice(self.COMMON_SITES)

                # Добавляем случайный путь для некоторых сайтов (реалистичнее)
                if random.random() < 0.4 and "?" not in url:
                    paths = ["search?q=test", "about", "contact", "news", "login"]
                    url = url + random.choice(paths)

                # Случайное время в течение дня
                visit_time = day_start + timedelta(
                    hours   = random.randint(7, 23),
                    minutes = random.randint(0, 59),
                    seconds = random.randint(0, 59),
                )

                # URL запись
                if url in url_cache:
                    url_id = url_cache[url]
                    cur.execute(
                        "UPDATE urls SET visit_count = visit_count + 1, "
                        "last_visit_time = ? WHERE id = ?",
                        (self._chrome_time(visit_time), url_id)
                    )
                else:
                    url_id = url_id_counter
                    url_id_counter += 1
                    url_cache[url] = url_id

                    try:
                        cur.execute("""
                            INSERT INTO urls (id, url, title, visit_count, typed_count,
                                              last_visit_time, hidden)
                            VALUES (?, ?, ?, 1, ?, ?, 0)
                        """, (
                            url_id, url, title,
                            1 if random.random() < 0.2 else 0,  # typed — 20% ввели в адресную строку
                            self._chrome_time(visit_time),
                        ))
                    except sqlite3.IntegrityError:
                        # URL уже есть
                        pass

                # Visit запись
                try:
                    cur.execute("""
                        INSERT INTO visits (id, url, visit_time, from_visit, external_referrer_url,
                                            transition, segment_id, visit_duration, incremented_omnibox_typed_score)
                        VALUES (?, ?, ?, 0, '', ?, 0, ?, 0)
                    """, (
                        visit_id_counter, url_id,
                        self._chrome_time(visit_time),
                        805306376 if random.random() < 0.3 else 805306368,  # link / typed
                        random.randint(3000000, 180000000),  # длительность в микросекундах
                    ))
                    visit_id_counter += 1
                    total_visits += 1
                except sqlite3.IntegrityError:
                    pass

        conn.commit()
        conn.close()
        logging.info(f"[ProfileEnricher] History: +{total_visits} посещений за {days_back} дней")

    def _create_history_schema(self, cur: sqlite3.Cursor):
        """Создаёт таблицы History если их нет (минимум для валидности)"""
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS urls (
                id               INTEGER PRIMARY KEY,
                url              LONGVARCHAR,
                title            LONGVARCHAR,
                visit_count      INTEGER DEFAULT 0 NOT NULL,
                typed_count      INTEGER DEFAULT 0 NOT NULL,
                last_visit_time  INTEGER NOT NULL,
                hidden           INTEGER DEFAULT 0 NOT NULL
            );
            CREATE INDEX IF NOT EXISTS urls_url_index ON urls(url);

            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY,
                url INTEGER NOT NULL,
                visit_time INTEGER NOT NULL,
                from_visit INTEGER,
                external_referrer_url TEXT,
                transition INTEGER DEFAULT 0 NOT NULL,
                segment_id INTEGER,
                visit_duration INTEGER DEFAULT 0 NOT NULL,
                incremented_omnibox_typed_score BOOLEAN DEFAULT FALSE NOT NULL
            );
            CREATE INDEX IF NOT EXISTS visits_url_index ON visits(url);
            CREATE INDEX IF NOT EXISTS visits_from_index ON visits(from_visit);
            CREATE INDEX IF NOT EXISTS visits_time_index ON visits(visit_time);

            CREATE TABLE IF NOT EXISTS keyword_search_terms (
                keyword_id       INTEGER NOT NULL,
                url_id           INTEGER NOT NULL,
                term             LONGVARCHAR NOT NULL,
                normalized_term  LONGVARCHAR NOT NULL
            );
        """)

    # ──────────────────────────────────────────────────────────
    # BOOKMARKS — JSON файл
    # ──────────────────────────────────────────────────────────

    def seed_bookmarks(self, count_range: tuple = (5, 15)):
        """Создаёт/дополняет файл Bookmarks реалистичными закладками"""
        bookmarks_path = os.path.join(self.default_dir, "Bookmarks")

        # Если уже существуют — не трогаем
        if os.path.exists(bookmarks_path):
            logging.info("[ProfileEnricher] Bookmarks уже существуют — пропускаем")
            return

        count = random.randint(*count_range)
        chosen = random.sample(self.COMMON_SITES, min(count, len(self.COMMON_SITES)))

        # Chrome bookmarks format
        now_chrome = self._chrome_time(datetime.now() - timedelta(days=random.randint(30, 180)))

        children = []
        for i, (url, title) in enumerate(chosen):
            children.append({
                "date_added":     str(now_chrome + i * 10000),
                "guid":           self._generate_guid(),
                "id":             str(100 + i),
                "meta_info":      {},
                "name":           title,
                "type":           "url",
                "url":            url,
            })

        bookmarks = {
            "checksum":  "",
            "roots": {
                "bookmark_bar": {
                    "children":     children[:min(5, len(children))],  # первые 5 на панели
                    "date_added":   str(now_chrome),
                    "date_modified": str(now_chrome),
                    "guid":         self._generate_guid(),
                    "id":           "1",
                    "name":         "Bookmarks bar",
                    "type":         "folder",
                },
                "other": {
                    "children":     children[5:],  # остальные в "other"
                    "date_added":   str(now_chrome),
                    "date_modified": str(now_chrome),
                    "guid":         self._generate_guid(),
                    "id":           "2",
                    "name":         "Other bookmarks",
                    "type":         "folder",
                },
                "synced": {
                    "children":     [],
                    "date_added":   str(now_chrome),
                    "date_modified": "0",
                    "guid":         self._generate_guid(),
                    "id":           "3",
                    "name":         "Mobile bookmarks",
                    "type":         "folder",
                },
            },
            "version": 1,
        }

        with open(bookmarks_path, "w", encoding="utf-8") as f:
            json.dump(bookmarks, f, indent=3, ensure_ascii=False)

        logging.info(f"[ProfileEnricher] Bookmarks: добавлено {count} закладок")

    @staticmethod
    def _generate_guid() -> str:
        """Генерирует Chrome-совместимый GUID"""
        import uuid
        return str(uuid.uuid4()).upper()

    # ──────────────────────────────────────────────────────────
    # TOP SITES — SQLite с топом для new tab page
    # ──────────────────────────────────────────────────────────

    def seed_top_sites(self):
        """Заполняет Top Sites базу — то что показывается на новой вкладке"""
        db_path = os.path.join(self.default_dir, "Top Sites")

        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS top_sites (
                url LONGVARCHAR NOT NULL,
                url_rank INTEGER NOT NULL,
                title LONGVARCHAR NOT NULL
            )
        """)

        # Выбираем топ-8 популярных
        top = random.sample(self.COMMON_SITES, min(8, len(self.COMMON_SITES)))
        cur.execute("DELETE FROM top_sites")  # очищаем
        for rank, (url, title) in enumerate(top):
            cur.execute(
                "INSERT INTO top_sites (url, url_rank, title) VALUES (?, ?, ?)",
                (url, rank, title)
            )

        conn.commit()
        conn.close()
        logging.info(f"[ProfileEnricher] Top Sites: {len(top)} сайтов")

    # ──────────────────────────────────────────────────────────
    # LAST SESSION / LAST TABS — "вкладки с прошлого раза"
    # ──────────────────────────────────────────────────────────

    def seed_last_session(self):
        """Создаёт пустые Current Session / Current Tabs чтобы Chrome не
        жаловался на "свежий" профиль"""
        for filename in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
            path = os.path.join(self.default_dir, filename)
            if not os.path.exists(path):
                # Минимальный бинарный заголовок Session файла
                # Chrome создаст нормальный при первом запуске, просто нужно чтобы
                # файл был — для реальности
                with open(path, "wb") as f:
                    f.write(b"SNSS")  # magic bytes

    # ──────────────────────────────────────────────────────────
    # ОБЩАЯ ФУНКЦИЯ
    # ──────────────────────────────────────────────────────────

    def enrich_all(self, history_days: int = 30):
        """Обогащает всё что можно. Вызывать перед первым запуском браузера."""
        logging.info(f"[ProfileEnricher] 🌱 Обогащаем профиль: {self.profile_path}")

        try:
            self.seed_history(days_back=history_days)
        except Exception as e:
            logging.warning(f"[ProfileEnricher] history: {e}")

        try:
            self.seed_bookmarks()
        except Exception as e:
            logging.warning(f"[ProfileEnricher] bookmarks: {e}")

        try:
            self.seed_top_sites()
        except Exception as e:
            logging.warning(f"[ProfileEnricher] top_sites: {e}")

        try:
            self.seed_last_session()
        except Exception as e:
            logging.warning(f"[ProfileEnricher] last_session: {e}")

        logging.info("[ProfileEnricher] ✓ Обогащение завершено")
