"""
cookie_warmer.py — Мгновенный прогрев через готовые cookies

Вместо реальных посещений сайтов (2-5 минут) устанавливает через CDP
правдоподобные cookies которые характерны для активного браузера:

- Consent cookies Google/YouTube (как будто юзер принял согласие раньше)
- NID cookie Google (идентификатор сессии)
- Preference cookies (языки, регион)
- localStorage для крупных сайтов

Это даёт тот же сигнал "я тут уже бывал" без траты времени на реальный прогрев.

ВАЖНО: cookies тут сгенерированы по правильному формату но со случайными
значениями — они не валидны для авторизации. Они работают как "присутствие",
а не "авторизация". Google видит что браузер имеет историю настроек.
"""

import time
import random
import logging
import string
from datetime import datetime, timezone


def _random_string(length: int, alphabet: str = None) -> str:
    alphabet = alphabet or string.ascii_letters + string.digits + "-_"
    return "".join(random.choices(alphabet, k=length))


def _future_timestamp(days: int) -> int:
    """Unix timestamp через N дней"""
    return int(time.time() + days * 86400)


# ──────────────────────────────────────────────────────────────
# ШАБЛОНЫ COOKIES ДЛЯ КРУПНЫХ САЙТОВ
# ──────────────────────────────────────────────────────────────

def google_cookies() -> list[dict]:
    """
    Cookies что устанавливает Google для активного юзера.
    Имитируем профиль который уже раньше заходил на google.com.
    """
    return [
        # Consent — был принят какое-то время назад
        {
            "name":   "CONSENT",
            "value":  f"YES+cb.{datetime.now().strftime('%Y%m%d')}-{random.randint(10,17)}-p0.uk+FX+{random.randint(100,999)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "SOCS",
            "value":  f"CAISHAgCEhJnd3NfMjAyN{_random_string(10)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365),
        },
        # NID — основной session cookie Google
        {
            "name":   "NID",
            "value":  f"511={_random_string(180)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "expiry": _future_timestamp(180),
        },
        # 1P_JAR — один из трекинговых
        {
            "name":   "1P_JAR",
            "value":  f"{datetime.now().strftime('%Y-%m-%d')}-{random.randint(0,23)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "sameSite": "None",
            "expiry": _future_timestamp(30),
        },
        # AEC — ещё один consent-related
        {
            "name":   "AEC",
            "value":  _random_string(80),
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "expiry": _future_timestamp(180),
        },
    ]


def youtube_cookies() -> list[dict]:
    """YouTube consent + preferences"""
    return [
        {
            "name":   "CONSENT",
            "value":  f"YES+cb.{datetime.now().strftime('%Y%m%d')}-{random.randint(10,17)}-p0.uk+FX+{random.randint(100,999)}",
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "VISITOR_INFO1_LIVE",
            "value":  _random_string(22),
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "expiry": _future_timestamp(180),
        },
        {
            "name":   "YSC",
            "value":  _random_string(16),
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            # Session cookie — без expiry
        },
        {
            "name":   "PREF",
            "value":  f"tz=Europe.Kiev&f6=400&hl=uk",
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
    ]


def common_analytics_cookies() -> list[dict]:
    """Google Analytics + общие трекеры — эти есть почти у каждого"""
    ga_id = f"GA1.2.{random.randint(1000000000, 9999999999)}.{int(time.time()) - random.randint(86400*7, 86400*60)}"
    return [
        {
            "name":   "_ga",
            "value":  ga_id,
            "domain": ".google.com",
            "path":   "/",
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "_gid",
            "value":  f"GA1.2.{random.randint(1000000000, 9999999999)}.{int(time.time()) - random.randint(0, 86400)}",
            "domain": ".google.com",
            "path":   "/",
            "expiry": _future_timestamp(1),
        },
    ]


# ──────────────────────────────────────────────────────────────
# ИНЖЕКТОР
# ──────────────────────────────────────────────────────────────

class CookieWarmer:
    """
    Использование:
        warmer = CookieWarmer(browser.driver)
        warmer.fast_warmup()   # 5-10 секунд вместо 2-5 минут
    """

    def __init__(self, driver):
        self.driver = driver

    def _inject_cookies_via_cdp(self, cookies: list[dict]):
        """Устанавливает cookies через CDP Network.setCookie — без посещения страницы"""
        # Включаем Network domain
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

        injected = 0
        for c in cookies:
            try:
                params = {
                    "name":   c["name"],
                    "value":  c["value"],
                    "domain": c["domain"],
                    "path":   c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                }
                if "sameSite" in c:
                    params["sameSite"] = c["sameSite"]
                if "expiry" in c:
                    params["expires"] = c["expiry"]

                self.driver.execute_cdp_cmd("Network.setCookie", params)
                injected += 1
            except Exception as e:
                logging.debug(f"[CookieWarmer] Не удалось установить {c['name']}: {e}")
        return injected

    def fast_warmup(self):
        """
        Быстрый прогрев: устанавливаем cookies без реальных посещений.
        Занимает 3-5 секунд вместо 2-5 минут.
        """
        logging.info("[CookieWarmer] ⚡ Быстрый прогрев через cookies...")
        started = time.time()

        all_cookies = (
            google_cookies() +
            youtube_cookies() +
            common_analytics_cookies()
        )

        count = self._inject_cookies_via_cdp(all_cookies)

        # Также добавляем записи в localStorage для Google/YouTube
        # Это делается через посещение — но очень короткое
        self._seed_local_storage()

        duration = time.time() - started
        logging.info(f"[CookieWarmer] ✓ Установлено {count} cookies за {duration:.1f}с")

    def _seed_local_storage(self):
        """Засеиваем localStorage — только на текущей странице (без переходов)"""
        try:
            # Мы не будем прыгать по доменам, просто посеем базовые вещи
            # если мы уже на google.com
            if "google.com" in self.driver.current_url:
                data = {
                    "_grecaptcha":            _random_string(30),
                    "google_experiment_mod":  str(random.randint(1000, 9999)),
                }
                for key, value in data.items():
                    self.driver.execute_script(
                        "try { localStorage.setItem(arguments[0], arguments[1]); } catch(e) {}",
                        key, value
                    )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # ГИБРИДНЫЙ ПРОГРЕВ — быстрый + короткие посещения
    # ──────────────────────────────────────────────────────────

    def hybrid_warmup(self, short_visits: bool = True):
        """
        Гибридный: сначала cookies, потом 1-2 коротких реальных посещения
        для максимальной достоверности. Занимает 20-30 секунд.
        """
        self.fast_warmup()

        if not short_visits:
            return

        logging.info("[CookieWarmer] Дополняем короткими посещениями...")

        # Первым делом идём на google.com — он самый "тёплый" (cookies уже есть)
        # и с него проверяем что сеть вообще работает
        quick_sites = [
            "https://www.google.com/",
            "https://www.youtube.com/",
        ]

        for url in quick_sites[:2]:
            try:
                self.driver.get(url)
                # Ждём загрузки document (не networkidle — слишком строго)
                self._wait_page_ready(timeout=15)

                # Проверяем что не показалась офлайн-страница
                if self._is_offline_page():
                    logging.warning(f"[CookieWarmer] {url} показал офлайн — пропускаем визиты")
                    # Возвращаемся на blank чтобы не оставлять офлайн-страницу
                    try:
                        self.driver.get("about:blank")
                    except Exception:
                        pass
                    return

                time.sleep(random.uniform(3, 5))
                # Небольшой скролл
                try:
                    self.driver.execute_script(f"window.scrollBy(0, {random.randint(200, 500)});")
                except Exception:
                    pass
                time.sleep(random.uniform(1, 2))
            except Exception as e:
                logging.debug(f"[CookieWarmer] {url}: {e}")

    def _wait_page_ready(self, timeout: int = 15):
        """Ждём document.readyState === complete"""
        started = time.time()
        while time.time() - started < timeout:
            try:
                state = self.driver.execute_script("return document.readyState;")
                if state == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _is_offline_page(self) -> bool:
        """Проверяет что Chrome не показал офлайн-страницу"""
        try:
            # Офлайн-страницы Chrome имеют специфичный title или текст
            title = (self.driver.title or "").lower()
            if any(marker in title for marker in ("офлайн", "offline", "недоступно")):
                return True
            # Или специфичный class на body
            body_text = self.driver.execute_script(
                "return (document.body && document.body.innerText || '').substring(0, 200).toLowerCase();"
            )
            offline_markers = [
                "підключіться до інтернету", "connect to the internet",
                "в режимі офлайн", "you're offline", "you are offline",
                "нет соединения", "подключитесь к интернету",
            ]
            return any(m in body_text for m in offline_markers)
        except Exception:
            return False
