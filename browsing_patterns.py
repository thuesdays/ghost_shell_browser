"""
browsing_patterns.py — Правдоподобные паттерны поведения

Реальный пользователь не идёт сразу к целевому поиску — он сначала
делает что-то нейтральное (новости, погода, YouTube), и только потом
ищет нужное. Это делает сессию неотличимой от живой.

Использование:
    patterns = BrowsingPatterns(browser)
    patterns.casual_browsing(duration_sec=60)  # 1 мин нейтрального серфинга
    # ... потом твой целевой поиск ...
"""

import time
import random
import logging


class BrowsingPatterns:
    """Различные шаблоны поведения перед целевой задачей"""

    # Поисковые запросы "для камуфляжа" — такие запросы делает обычный юзер
    CASUAL_QUERIES = {
        "news": [
            "новини сьогодні", "погода київ", "курс долара",
            "новости украины", "курс валют", "пробки киев",
        ],
        "how_to": [
            "як зварити борщ", "как приготовить кофе", "время полёта до стамбула",
            "сколько стоит iphone", "как заказать такси",
        ],
        "entertainment": [
            "фильмы 2025", "youtube trending", "anime онлайн",
            "сериалы новые", "смешные видео",
        ],
        "shopping": [
            "купить наушники", "новый iphone цена", "акции rozetka",
            "скидки magnit", "черная пятница",
        ],
    }

    NEUTRAL_SITES = [
        "https://www.youtube.com/",
        "https://uk.wikipedia.org/wiki/Головна_сторінка",
        "https://www.rozetka.com.ua/",
        "https://www.ukr.net/",
        "https://www.pravda.com.ua/",
    ]

    def __init__(self, browser):
        """browser — экземпляр NKBrowser"""
        self.browser = browser
        self.driver  = browser.driver

    # ──────────────────────────────────────────────────────────
    # ПАТТЕРНЫ
    # ──────────────────────────────────────────────────────────

    def casual_browsing(self, duration_sec: float = 60):
        """
        Случайный нейтральный серфинг примерно указанное время.
        Смесь: Google поиск нейтральных запросов + посещение сайтов.
        """
        logging.info(f"[Patterns] 🏄 Нейтральный серфинг ~{duration_sec:.0f}с")
        started = time.time()

        actions = [
            self._action_casual_search,
            self._action_visit_neutral_site,
            self._action_visit_neutral_site,  # чаще сайтов чем поисков
        ]

        while time.time() - started < duration_sec:
            action = random.choice(actions)
            try:
                action()
            except Exception as e:
                logging.debug(f"[Patterns] action error: {e}")
            # Между действиями пауза
            time.sleep(random.uniform(2, 5))

        logging.info(f"[Patterns] Серфинг завершён ({time.time() - started:.0f}с)")

    def pre_target_warmup(self):
        """
        Короткий предварительный серфинг перед целевым действием.
        Открывает Google, делает 1 нейтральный поиск, потом готов к целевому.
        """
        logging.info("[Patterns] 🎯 Pre-target warmup")

        try:
            self.driver.get("https://www.google.com")
            time.sleep(random.uniform(3, 5))
            self.browser.warm_mouse()

            # Один нейтральный запрос
            query = random.choice(self.CASUAL_QUERIES["news"] + self.CASUAL_QUERIES["how_to"])
            self._do_search(query, follow_through=True)

        except Exception as e:
            logging.debug(f"[Patterns] pre_target error: {e}")

    def post_target_cooldown(self, min_sec: float = 10, max_sec: float = 40):
        """После целевого поиска — что-то нейтральное, имитируем что юзер
        не прицельно искал бренд а просто гуглил что-то"""
        logging.info("[Patterns] 🌡 Post-target cooldown")

        # Случайное: либо ещё один поиск, либо просто посещение
        if random.random() < 0.5:
            query = random.choice(
                self.CASUAL_QUERIES["news"] +
                self.CASUAL_QUERIES["entertainment"]
            )
            try:
                self._do_search(query, follow_through=False)
            except Exception:
                pass
        else:
            try:
                self._action_visit_neutral_site()
            except Exception:
                pass

        time.sleep(random.uniform(min_sec, max_sec))

    # ──────────────────────────────────────────────────────────
    # ЭЛЕМЕНТАРНЫЕ ДЕЙСТВИЯ
    # ──────────────────────────────────────────────────────────

    def _action_casual_search(self):
        """Поиск нейтрального запроса в Google"""
        category = random.choice(list(self.CASUAL_QUERIES.keys()))
        query    = random.choice(self.CASUAL_QUERIES[category])
        self._do_search(query, follow_through=random.random() < 0.4)

    def _action_visit_neutral_site(self):
        """Заходим на нейтральный сайт и скроллим"""
        url = random.choice(self.NEUTRAL_SITES)
        logging.debug(f"[Patterns] → {url}")
        try:
            self.driver.get(url)
            time.sleep(random.uniform(3, 6))
            self.browser.smart_dwell(min_sec=3, max_sec=15)
            self.browser.human_scroll(
                min_scrolls=random.randint(1, 3),
                max_scrolls=random.randint(3, 6),
            )
        except Exception:
            pass

    def _do_search(self, query: str, follow_through: bool = False):
        """
        Выполняет поиск в Google. Предполагается что мы уже на google.com
        или будет туда переход.
        follow_through=True — кликаем по первому органическому результату
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        logging.info(f"[Patterns] 🔎 '{query}'")

        try:
            # Если не на Google — идём туда
            if "google." not in self.driver.current_url:
                self.driver.get("https://www.google.com")
                time.sleep(random.uniform(2, 4))

            search_box = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.NAME, "q"))
            )

            self.browser.bezier_move_to(search_box)
            time.sleep(random.uniform(0.3, 0.8))
            search_box.send_keys(Keys.CONTROL + "a")
            search_box.send_keys(Keys.BACKSPACE)
            time.sleep(random.uniform(0.2, 0.5))

            self.browser.human_type(search_box, query)
            time.sleep(random.uniform(0.5, 1.2))
            search_box.send_keys(Keys.RETURN)
            time.sleep(random.uniform(3, 6))

            # Скролл по выдаче — как будто читаем
            self.browser.human_scroll(1, 3)

            # Опционально — клик по первому результату
            if follow_through:
                try:
                    results = self.driver.find_elements(
                        By.CSS_SELECTOR, "div.g a h3, div.MjjYud a h3"
                    )
                    if results:
                        chosen = random.choice(results[:3])  # один из первых 3
                        self.browser.bezier_move_to(chosen)
                        time.sleep(random.uniform(5, 15))
                        # Возвращаемся назад
                        self.driver.back()
                        time.sleep(random.uniform(2, 4))
                except Exception as e:
                    logging.debug(f"[Patterns] follow_through: {e}")

        except Exception as e:
            logging.debug(f"[Patterns] search '{query}': {e}")
