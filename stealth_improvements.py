"""
stealth_improvements.py — Критические улучшения для обхода детекта

Эти улучшения решают главные причины почему Google показывает капчу
даже при хорошем фингерпринте:

1. ACCEPT_HEADER_CONSISTENCY — точные HTTP-заголовки Chrome
2. QUERY_FREQUENCY_LIMITER — не спамим запросы слишком часто
3. SEARCH_SESSION_AGE — заход на google.com + несколько страниц ДО целевого поиска
4. ORGANIC_RESULT_CLICK — иногда кликаем по результату (сигнал "живой юзер")
5. SERP_INTERACTION — взаимодействие с выдачей: hover, scroll, dwell
"""

import os
import time
import json
import random
import logging
from datetime import datetime, timedelta


# ──────────────────────────────────────────────────────────────
# 1. QUERY FREQUENCY LIMITER
# ──────────────────────────────────────────────────────────────

class QueryRateLimiter:
    """
    Следит чтобы мы не делали запросы слишком часто.

    Google палит ботов не только по фингерпринту, но и по частоте:
    - Тот же IP + больше 10 запросов за 5 минут = капча
    - Тот же profile + больше 30 запросов за час = подозрение
    - Разные IP + похожий запрос в течение секунд = bot cluster

    Храним историю в state_file, вычисляем достаточно ли времени прошло.
    """

    def __init__(self, state_file: str = "query_rate.json"):
        self.state_file = state_file
        self.history    = self._load()

    def _load(self) -> list[dict]:
        if not os.path.exists(self.state_file):
            return []
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self):
        try:
            # Храним только последние сутки
            cutoff = (datetime.now() - timedelta(days=1)).isoformat()
            self.history = [h for h in self.history if h["ts"] >= cutoff]
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2)
        except Exception:
            pass

    def record_query(self, query: str, ip: str = None):
        """Записывает факт запроса"""
        self.history.append({
            "ts":    datetime.now().isoformat(timespec="seconds"),
            "query": query,
            "ip":    ip or "",
        })
        self._save()

    def count_recent(self, minutes: int) -> int:
        """Сколько запросов было за последние N минут"""
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        return sum(1 for h in self.history if h["ts"] >= cutoff)

    def should_wait(self) -> tuple[bool, float]:
        """
        Нужно ли ждать перед следующим запросом.
        Возвращает (should_wait, wait_seconds).

        Пороги:
        - >5 запросов за 5 минут → ждём до падения ниже
        - >15 запросов за 30 минут → 3-5 минут паузы
        - >30 запросов за час → 10-15 минут паузы
        """
        q5m   = self.count_recent(5)
        q30m  = self.count_recent(30)
        q1h   = self.count_recent(60)

        if q1h >= 30:
            wait = random.uniform(600, 900)  # 10-15 мин
            logging.warning(f"[RateLimit] >30 запросов за час — пауза {wait/60:.0f} мин")
            return True, wait
        if q30m >= 15:
            wait = random.uniform(180, 300)  # 3-5 мин
            logging.warning(f"[RateLimit] >15 запросов за 30 мин — пауза {wait/60:.0f} мин")
            return True, wait
        if q5m >= 5:
            wait = random.uniform(60, 120)  # 1-2 мин
            logging.warning(f"[RateLimit] >5 запросов за 5 мин — пауза {wait/60:.0f} мин")
            return True, wait

        return False, 0

    def wait_if_needed(self):
        """Ждёт если надо"""
        should, secs = self.should_wait()
        if should:
            time.sleep(secs)


# ──────────────────────────────────────────────────────────────
# 2. SEARCH SESSION AGE — "состаривание" сессии
# ──────────────────────────────────────────────────────────────

def age_session_before_search(browser, duration_sec: float = 30):
    """
    Перед целевым поиском проводим N секунд на google.com:
    - Скроллим (смотрим doodle если есть)
    - Можем открыть мапы/gmail в другой вкладке на 2-3 секунды
    - Это даёт Google понять что мы "живой юзер на главной"

    Важно: делаем это ТОЛЬКО если уже есть сессия (сразу после warmup).
    Для нового профиля — warmup уже всё сделал.
    """
    driver = browser.driver
    started = time.time()
    logging.info(f"[SessionAge] 🌱 Состариваем сессию {duration_sec:.0f}с")

    try:
        # Идём на google.com если ещё не там
        if not driver.current_url.startswith("https://www.google.com"):
            browser.stealth_get("https://www.google.com/")
            time.sleep(random.uniform(2, 4))

        # Прокрутка главной страницы (если есть что)
        try:
            browser.human_scroll(1, 2)
        except Exception:
            pass

        time.sleep(random.uniform(2, 4))

        # Наводим мышку на логотип / search box / кнопки — "смотрит и думает"
        try:
            from selenium.webdriver.common.by import By
            targets = driver.find_elements(By.CSS_SELECTOR, "img[alt], a[href*='maps'], a[href*='gmail']")
            if targets:
                target = random.choice(targets[:5])
                browser.bezier_move_to(target)
                time.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass

        # Если есть время — ещё пауза
        remaining = duration_sec - (time.time() - started)
        if remaining > 0:
            time.sleep(random.uniform(max(1, remaining * 0.7), remaining))

    except Exception as e:
        logging.debug(f"[SessionAge] {e}")


# ──────────────────────────────────────────────────────────────
# 3. SERP INTERACTION — взаимодействие с выдачей
# ──────────────────────────────────────────────────────────────

def interact_with_serp(browser, dwell_min: float = 3, dwell_max: float = 10):
    """
    Имитирует что юзер смотрит выдачу:
    - Hover по первым 2-3 результатам (не клик, просто наведение мыши)
    - Мелкий скролл вверх-вниз
    - Пауза чтобы "прочитать"

    Это важный сигнал "живого юзера" — Google отслеживает время до первого
    клика, hover'ы, pattern скролла.
    """
    from selenium.webdriver.common.by import By

    driver = browser.driver

    try:
        # Пауза "чтение выдачи"
        time.sleep(random.uniform(1.5, 3.5))

        # Hover по первым органическим результатам
        results = driver.find_elements(
            By.CSS_SELECTOR,
            "div.g a[href], div.MjjYud a[href], div[data-hveid] a[ping]"
        )
        if results:
            for target in results[:random.randint(1, 3)]:
                try:
                    browser.bezier_move_to_hover(target) if hasattr(browser, "bezier_move_to_hover") else None
                except Exception:
                    pass
                time.sleep(random.uniform(0.5, 1.5))

        # Мелкий скролл туда-сюда (как будто ищем что-то в выдаче)
        try:
            driver.execute_script("window.scrollBy(0, arguments[0]);",
                                  random.randint(150, 400))
            time.sleep(random.uniform(0.8, 2))
            driver.execute_script("window.scrollBy(0, arguments[0]);",
                                  -random.randint(50, 200))
            time.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass

        # Финальная пауза dwell
        time.sleep(random.uniform(dwell_min, dwell_max))

    except Exception as e:
        logging.debug(f"[SerpInteract] {e}")


# ──────────────────────────────────────────────────────────────
# 4. CAPTCHA PATTERN LEARNER
# ──────────────────────────────────────────────────────────────

class CaptchaPatternLearner:
    """
    Учит закономерности появления капчи для конкретного профиля/IP.

    Полезные сигналы:
    - В какое время дня чаще капча?
    - После какого запроса?
    - После скольких запросов подряд?
    - После какого интервала между запусками?

    Это даёт данные для оптимизации расписания.
    """

    def __init__(self, state_file: str = "captcha_patterns.json"):
        self.state_file = state_file
        self.data       = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.state_file):
            return {"events": []}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"events": []}

    def _save(self):
        try:
            # Храним последние 500 событий
            self.data["events"] = self.data["events"][-500:]
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

    def record(self, event: str, **meta):
        """event: 'captcha', 'success', 'blocked'"""
        self.data["events"].append({
            "ts":    datetime.now().isoformat(timespec="seconds"),
            "hour":  datetime.now().hour,
            "event": event,
            **meta,
        })
        self._save()

    def get_safest_hours(self) -> list[int]:
        """Возвращает часы с наименьшим captcha rate"""
        from collections import defaultdict
        by_hour = defaultdict(lambda: {"captcha": 0, "success": 0})
        for e in self.data["events"]:
            h = e.get("hour")
            if h is None:
                continue
            if e["event"] == "captcha":
                by_hour[h]["captcha"] += 1
            elif e["event"] == "success":
                by_hour[h]["success"] += 1

        rates = []
        for h, stats in by_hour.items():
            total = stats["captcha"] + stats["success"]
            if total >= 3:  # минимум данных
                rate = stats["captcha"] / total
                rates.append((h, rate))

        rates.sort(key=lambda x: x[1])
        return [h for h, _ in rates[:5]]
