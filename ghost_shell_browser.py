"""
NK Browser — Профессиональный антидетект браузер
Уровень защиты: Canvas, WebGL, Audio, Navigator, Screen, Plugins, Permissions
"""

import os
import json
import random
import logging
import time
import base64
import math
import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from datetime import datetime

# ──────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ──────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
]

LANGUAGES_LIST = [
    ["uk-UA", "uk", "ru", "en-US", "en"],
    ["ru-RU", "ru", "uk", "en-US", "en"],
    ["en-US", "en", "uk", "ru"],
]

WEBGL_CONFIGS = [
    {
        "vendor":   "Google Inc. (NVIDIA Corporation)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "vendor":   "Google Inc. (NVIDIA Corporation)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "vendor":   "Google Inc. (Intel)",
        "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "vendor":   "Google Inc. (AMD)",
        "renderer": "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
]

SCREEN_SIZES = [
    (1920, 1080),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (2560, 1440),
]

HARDWARE_CONFIGS = [
    {"hardware_concurrency": 8,  "device_memory": 8},
    {"hardware_concurrency": 12, "device_memory": 16},
    {"hardware_concurrency": 4,  "device_memory": 4},
    {"hardware_concurrency": 16, "device_memory": 32},
]


def _random_hash(length: int = 64) -> str:
    """Генерирует случайный hex-hash для deviceId/groupId"""
    import secrets
    return secrets.token_hex(length // 2)


# ──────────────────────────────────────────────────────────────
# ОСНОВНОЙ КЛАСС
# ──────────────────────────────────────────────────────────────

class GhostShellBrowser:
    def __init__(
        self,
        profile_name: str,
        proxy_str: str = None,
        base_dir: str = "profiles",
        browser_path: str = None,
        device_template: str = None,
        auto_session: bool = True,
        is_rotating_proxy: bool = False,
        rotation_api_url: str = None,
        enrich_on_create: bool = True,
    ):
        self.profile_name     = str(profile_name)
        self.user_data_path   = os.path.abspath(os.path.join(base_dir, self.profile_name))
        self.proxy_str        = proxy_str
        self.browser_path     = browser_path
        self.device_template  = device_template
        self.auto_session     = auto_session
        self.is_rotating_proxy = is_rotating_proxy
        self.rotation_api_url  = rotation_api_url
        self.enrich_on_create  = enrich_on_create
        self.driver           = None
        self._session_mgr     = None
        self._proxy_forwarder = None
        self._rotating_tracker = None  # RotatingProxyTracker если is_rotating_proxy

        # Обогащение профиля — до создания папок, пока его ещё нет
        is_new_profile = not os.path.exists(self.user_data_path)

        os.makedirs(self.user_data_path, exist_ok=True)
        self.session_dir = os.path.join(self.user_data_path, "nk_session")

        # Enrich новый профиль данными — имитируем что Chrome уже использовался
        if is_new_profile and enrich_on_create:
            try:
                from profile_enricher import ProfileEnricher
                ProfileEnricher(self.user_data_path).enrich_all()
            except Exception as e:
                logging.warning(f"[NKBrowser] Enrichment failed: {e}")

        self._js_template_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fingerprints.js"
        )

    # ──────────────────────────────────────────────────────────
    # CONTEXT MANAGER SUPPORT
    # ──────────────────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ──────────────────────────────────────────────────────────
    # ФИНГЕРПРИНТ
    # ──────────────────────────────────────────────────────────

    def _load_or_create_fingerprint(self) -> dict:
        fp_path = os.path.join(self.user_data_path, "fingerprint.json")
        if os.path.exists(fp_path):
            try:
                with open(fp_path, "r", encoding="utf-8") as f:
                    fp = json.load(f)
                # Проверяем целостность — должны быть все критичные поля
                required = [
                    "user_agent", "canvas_noise", "audio_noise",
                    "webgl_vendor", "webgl_renderer",
                    "hardware_concurrency", "device_memory",
                    "screen_width", "screen_height",
                ]
                missing = [k for k in required if k not in fp]
                if missing:
                    logging.warning(
                        f"[NKBrowser] Фингерпринт повреждён (нет полей: {missing}). "
                        f"Бэкапим старый и создаём новый."
                    )
                    # Бэкап повреждённого
                    backup = fp_path + f".broken.{int(time.time())}"
                    os.rename(fp_path, backup)
                else:
                    return fp
            except json.JSONDecodeError:
                logging.error(f"[NKBrowser] Фингерпринт нечитаем — создаём новый")
                os.rename(fp_path, fp_path + f".corrupted.{int(time.time())}")

        # Используем согласованный шаблон устройства вместо случайных полей
        from device_templates import get_template, validate_fingerprint
        template = get_template(self.device_template)

        # Battery: либо на зарядке (уровень ~100%), либо от батареи (75-95%)
        battery_charging = random.choice([True, False])

        # Извлекаем версию Chrome из UA для Client Hints
        ua = random.choice(USER_AGENTS)
        import re as _re
        chrome_version = _re.search(r'Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)', ua)
        full_ver  = chrome_version.group(0).split('/')[1] if chrome_version else "131.0.0.0"
        major_ver = chrome_version.group(1)              if chrome_version else "131"

        # Storage: реалистичные значения в байтах (50-200 GB)
        total_quota = random.randint(50, 200) * 1024**3
        used        = random.randint(5, 40)  * 1024**3

        fp = {
            "template_name":        template["template_name"],
            "user_agent":           ua,
            "chrome_version_full":  full_ver,
            "chrome_version_major": major_ver,
            "languages":            random.choice(LANGUAGES_LIST),
            # ─── Из шаблона устройства (согласованно) ─────────
            "webgl_vendor":         template["webgl_vendor"],
            "webgl_renderer":       template["webgl_renderer"],
            "hardware_concurrency": template["hardware_concurrency"],
            "device_memory":        template["device_memory"],
            "screen_width":         template["screen"][0],
            "screen_height":        template["screen"][1],
            # ──────────────────────────────────────────────────
            "platform":             "Win32",
            "timezone":             "Europe/Kyiv",
            # Уникальные шумы
            "canvas_noise":         random.randint(1, 15),
            "audio_noise":          round(random.uniform(0.00001, 0.0001), 6),
            # Battery
            "battery_charging":           battery_charging,
            "battery_level":              round(random.uniform(0.75, 1.0), 2) if battery_charging else round(random.uniform(0.4, 0.95), 2),
            "battery_discharging_time":   random.randint(3600, 18000),
            # Network Connection
            "connection_type":      random.choice(["4g", "4g", "4g", "3g"]),
            "connection_downlink":  round(random.uniform(5.0, 50.0), 1),
            "connection_rtt":       random.choice([50, 100, 50, 75, 100]),
            # Media devices
            "device_id_1":          _random_hash(64),
            "device_id_2":          _random_hash(64),
            "device_id_3":          _random_hash(64),
            "group_id_1":           _random_hash(64),
            "group_id_2":           _random_hash(64),
            # Storage
            "storage_quota":        total_quota,
            "storage_usage":        used,
            # CSS preferences
            "color_scheme":         random.choice(["light", "light", "dark"]),
            # History — имитация посещённых страниц
            "history_length":       random.randint(3, 15),
            # Положение окна на экране (не 0,0 как у бота по умолчанию)
            "window_x":             random.randint(20, 200),
            "window_y":             random.randint(20, 150),
            # Do Not Track: null — дефолт Chrome, редко меняют
            "do_not_track":         random.choice([None, None, None, "1"]),
        }

        # Валидация
        warnings = validate_fingerprint(fp)
        if warnings:
            logging.warning(f"[NKBrowser] Фингерпринт имеет предупреждения:")
            for w in warnings:
                logging.warning(f"  ⚠ {w}")

        with open(fp_path, "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=4, ensure_ascii=False)

        logging.info(f"[NKBrowser] Новый фингерпринт: профиль='{self.profile_name}', шаблон='{template['template_name']}'")
        return fp

        with open(fp_path, "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=4, ensure_ascii=False)

        logging.info(f"[NKBrowser] Новый фингерпринт создан для профиля '{self.profile_name}'")
        return fp

    def _build_injection_script(self, fp: dict) -> str:
        """Читаем JS-шаблон и подставляем фингерпринт"""
        if not os.path.exists(self._js_template_path):
            logging.warning("[NKBrowser] fingerprints.js не найден, JS-инъекции пропущены")
            return ""

        with open(self._js_template_path, "r", encoding="utf-8") as f:
            template = f.read()

        fp_json = json.dumps(fp, ensure_ascii=False)
        return template.replace("__FINGERPRINT__", fp_json)

    # ──────────────────────────────────────────────────────────
    # ПРОКСИ РАСШИРЕНИЕ
    # ──────────────────────────────────────────────────────────

    def _build_proxy_extension(self) -> str | None:
        if not self.proxy_str:
            return None

        ext_dir = os.path.join(self.user_data_path, "proxy_ext")
        os.makedirs(ext_dir, exist_ok=True)

        # Поддержка форматов: user:pass@host:port и host:port
        if "@" in self.proxy_str:
            auth, host_port = self.proxy_str.split("@", 1)
            user, password  = auth.split(":", 1)
        else:
            host_port = self.proxy_str
            user = password = None

        host, port = host_port.rsplit(":", 1)

        # Manifest V3 — обязательно для Chrome 127+ (MV2 удалён)
        manifest = {
            "manifest_version": 3,
            "name":             "NK Proxy",
            "version":          "1.0.0",
            "permissions": [
                "proxy",
                "webRequest",
                "webRequestAuthProvider",
                "storage",
            ],
            "host_permissions": ["<all_urls>"],
            "background": {
                "service_worker": "background.js",
            },
        }

        # Блок аутентификации через asyncBlocking (требует webRequestAuthProvider)
        auth_block = ""
        if user and password:
            auth_block = f"""
chrome.webRequest.onAuthRequired.addListener(
    (details, callback) => {{
        callback({{ authCredentials: {{ username: "{user}", password: "{password}" }} }});
    }},
    {{ urls: ["<all_urls>"] }},
    ["asyncBlocking"]
);"""

        # Service worker — регистрируем всё на top level
        background_js = f"""
chrome.proxy.settings.set({{
    value: {{
        mode: "fixed_servers",
        rules: {{
            singleProxy: {{ scheme: "http", host: "{host}", port: parseInt({port}) }},
            bypassList: ["localhost", "127.0.0.1"]
        }}
    }},
    scope: "regular"
}});
{auth_block}
"""

        with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=4)
        with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
            f.write(background_js)

        return ext_dir

    # ──────────────────────────────────────────────────────────
    # ЗАПУСК
    # ──────────────────────────────────────────────────────────

    def start(self) -> uc.Chrome:
        fp = self._load_or_create_fingerprint()

        # Preferences: WebRTC + язык
        pref_path = os.path.join(self.user_data_path, "Default", "Preferences")
        os.makedirs(os.path.dirname(pref_path), exist_ok=True)
        prefs = {
            "webrtc": {
                "ip_handling_policy": "disable_non_proxied_udp",
                "multiple_routes_enabled": False,
                "nonproxied_udp_enabled": False,
            },
            "profile": {
                "default_content_setting_values": {
                    "geolocation": 2,
                    "notifications": 2,
                    "media_stream_camera": 2,
                    "media_stream_mic": 2,
                }
            },
            "intl": {"accept_languages": ",".join(fp["languages"])},
        }
        with open(pref_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)

        # Chrome Options
        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={self.user_data_path}")
        options.add_argument(f"--user-agent={fp['user_agent']}")
        options.add_argument(f"--lang={fp['languages'][0]}")
        options.add_argument("--no-sandbox")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(f"--window-size={fp['screen_width']},{fp['screen_height'] - 80}")

        # Прокси через локальный форвардер (вместо расширения)
        if self.proxy_str:
            from proxy_forwarder import ProxyForwarder
            self._proxy_forwarder = ProxyForwarder(self.proxy_str)
            local_port = self._proxy_forwarder.start()
            options.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
            # Chrome не будет пытаться обойти прокси для localhost-пинга
            options.add_argument("--proxy-bypass-list=<-loopback>")

        # Запуск
        kwargs = dict(options=options, use_subprocess=False)
        if self.browser_path:
            kwargs["browser_executable_path"] = self.browser_path

        self.driver = uc.Chrome(**kwargs)

        # ── CDP инъекции (до первого запроса) ──

        # 1. Таймзона
        self.driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": fp["timezone"]}
        )

        # 2. User-Agent + Client Hints через CDP
        #    Client Hints (Sec-CH-UA заголовки) отправляются Chrome 90+
        #    и должны строго соответствовать User-Agent
        self.driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent":      fp["user_agent"],
            "acceptLanguage": ",".join(fp["languages"]),
            "platform":       fp["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not_A Brand",       "version": "8"},
                    {"brand": "Chromium",          "version": fp["chrome_version_major"]},
                    {"brand": "Google Chrome",     "version": fp["chrome_version_major"]},
                ],
                "fullVersionList": [
                    {"brand": "Not_A Brand",       "version": "8.0.0.0"},
                    {"brand": "Chromium",          "version": fp["chrome_version_full"]},
                    {"brand": "Google Chrome",     "version": fp["chrome_version_full"]},
                ],
                "fullVersion":  fp["chrome_version_full"],
                "platform":     "Windows",
                "platformVersion": "15.0.0",
                "architecture": "x86",
                "model":        "",
                "mobile":       False,
                "bitness":      "64",
                "wow64":        False,
            }
        })

        # 3. Главный JS-фингерпринт
        injection_script = self._build_injection_script(fp)
        if injection_script:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": injection_script}
            )

        # 4. Сетевые условия через CDP
        self.set_network_conditions()

        # 5. Extra HTTP headers — порядок важен, Chrome отправляет именно так
        self._set_extra_http_headers(fp)

        # 6. Viewport с небольшим случайным сдвигом
        self.apply_viewport_jitter()

        # 7. КРИТИЧНО: обязательная инициализирующая навигация на пустую страницу
        #    Без этого ПЕРВАЯ навигация пользователя (будь то youtube или google)
        #    идёт БЕЗ примененных CDP-инъекций fingerprints.js. Chrome получает
        #    "голый" реальный User-Agent/timezone → детект, показ "офлайн".
        #    data:URL не требует сети и гарантированно применяет инъекции.
        try:
            self.driver.get("data:text/html,<html><head><title>init</title></head><body></body></html>")
            time.sleep(0.8)

            # Проверка что инъекции действительно применились
            ua_ok = self.driver.execute_script(
                "return navigator.userAgent === arguments[0]", fp["user_agent"]
            )
            if not ua_ok:
                logging.warning("[NKBrowser] User-Agent injection не применился — перезапрашиваем")
                time.sleep(0.5)
        except Exception as e:
            logging.debug(f"[NKBrowser] init nav: {e}")

        # 8. Авто-восстановление сохранённой сессии (cookies + storage)
        if self.auto_session and os.path.exists(self.session_dir):
            try:
                self._auto_restore_session()
            except Exception as e:
                logging.warning(f"[NKBrowser] Не удалось восстановить сессию: {e}")

        logging.info(f"[NKBrowser] Браузер запущен. Профиль: {self.profile_name}")
        return self.driver

    def _auto_restore_session(self):
        """Восстанавливает сохранённую сессию из папки профиля"""
        from session_manager import SessionManager
        self._session_mgr = SessionManager(self.driver)

        cookies_path  = os.path.join(self.session_dir, "cookies.json")
        storage_path  = os.path.join(self.session_dir, "storage.json")

        if os.path.exists(cookies_path):
            count = self._session_mgr.import_cookies(cookies_path)
            if count > 0:
                logging.info(f"[NKBrowser] ↻ Восстановлено {count} cookies из прошлой сессии")

        if os.path.exists(storage_path):
            self._session_mgr.import_storage(storage_path, navigate_first=True)

    def _auto_save_session(self):
        """Сохраняет текущую сессию в папку профиля"""
        if self._session_mgr is None:
            from session_manager import SessionManager
            self._session_mgr = SessionManager(self.driver)

        os.makedirs(self.session_dir, exist_ok=True)
        try:
            self._session_mgr.export_cookies(os.path.join(self.session_dir, "cookies.json"))
            self._session_mgr.export_storage(os.path.join(self.session_dir, "storage.json"))
            logging.info(f"[NKBrowser] ↓ Сессия сохранена в {self.session_dir}")
        except Exception as e:
            logging.warning(f"[NKBrowser] Не удалось сохранить сессию: {e}")

    # ──────────────────────────────────────────────────────────
    # ЧЕЛОВЕКОПОДОБНЫЕ ДЕЙСТВИЯ
    # ──────────────────────────────────────────────────────────

    # Соседние клавиши QWERTY — для правдоподобных опечаток
    _KEYBOARD_NEIGHBORS = {
        "q": "wa",    "w": "qeas",   "e": "wrds",   "r": "etdf",
        "t": "ryfg",  "y": "tugh",   "u": "yihj",   "i": "uojk",
        "o": "ipkl",  "p": "ol",     "a": "qwsz",   "s": "awedxz",
        "d": "serfcx","f": "drtgvc", "g": "ftyhbv", "h": "gyujnb",
        "j": "huiknm","k": "jiolm",  "l": "kop",    "z": "asx",
        "x": "zsdc",  "c": "xdfv",   "v": "cfgb",   "b": "vghn",
        "n": "bhjm",  "m": "njk",
    }

    def _typo_for(self, char: str) -> str:
        """Возвращает правдоподобную опечатку для символа (соседняя клавиша)"""
        lower = char.lower()
        neighbors = self._KEYBOARD_NEIGHBORS.get(lower, "")
        if not neighbors:
            return random.choice("abcde")
        typo = random.choice(neighbors)
        return typo.upper() if char.isupper() else typo

    def human_type(self, element, text: str, wpm: int = None):
        """
        Печать с реальной скоростью, паузами и правдоподобными опечатками.
        wpm — words per minute. Если None — выбирается по времени суток.
        """
        from selenium.webdriver.common.keys import Keys
        from datetime import datetime as _dt

        # Time-of-day awareness — ночью и рано утром печатаем медленнее
        if wpm is None:
            hour = _dt.now().hour
            if 0 <= hour < 6:       # ночь — заспанный юзер
                wpm = random.randint(100, 140)
            elif 6 <= hour < 9:     # раннее утро — ещё сонный
                wpm = random.randint(130, 170)
            elif 9 <= hour < 12:    # рабочее утро — бодрый
                wpm = random.randint(170, 220)
            elif 12 <= hour < 14:   # обед — расслабленный
                wpm = random.randint(150, 190)
            elif 14 <= hour < 18:   # рабочий день — активный
                wpm = random.randint(180, 230)
            elif 18 <= hour < 22:   # вечер — средний темп
                wpm = random.randint(150, 190)
            else:                   # поздний вечер — устал
                wpm = random.randint(120, 160)

        delay_base = 60.0 / (wpm * 5)

        for i, char in enumerate(text):
            # 3% шанс на опечатку — но только на латинских буквах
            if random.random() < 0.03 and char.isalpha() and ord(char) < 128:
                typo = self._typo_for(char)
                element.send_keys(typo)
                time.sleep(random.uniform(0.15, 0.45))
                element.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.08, 0.2))

            element.send_keys(char)

            delay = delay_base * random.uniform(0.6, 1.4)

            # Редкая длинная пауза — "подумал"
            if random.random() < 0.03:
                delay += random.uniform(0.4, 1.2)

            # После пробела или знака — чуть дольше
            if char in " .,;!?":
                delay *= random.uniform(1.2, 1.8)

            time.sleep(delay)

    def human_move_and_click(self, element):
        """Плавное движение мыши к элементу → клик"""
        actions = ActionChains(self.driver)
        w = max(element.size.get("width", 10), 1)
        h = max(element.size.get("height", 10), 1)

        # Небольшое смещение от центра элемента (не выходим за его границы)
        offset_x = random.randint(-max(1, int(w / 4)), max(1, int(w / 4)))
        offset_y = random.randint(-max(1, int(h / 4)), max(1, int(h / 4)))
        actions.move_to_element_with_offset(element, offset_x, offset_y)
        actions.pause(random.uniform(0.1, 0.35))
        actions.click()
        actions.perform()

    def human_scroll(self, min_scrolls: int = 2, max_scrolls: int = 5):
        """Плавный скролл — имитирует колёсико мыши через JS"""
        for _ in range(random.randint(min_scrolls, max_scrolls)):
            total    = random.randint(200, 700)
            steps    = random.randint(8, 20)
            interval = random.uniform(0.03, 0.07)
            # Несколько маленьких шагов вместо одного большого прыжка
            for step in range(steps):
                # Замедление в конце (easing)
                progress  = step / steps
                eased     = math.sin(progress * math.pi / 2)
                step_size = int((total / steps) * (1 - eased * 0.3))
                self.driver.execute_script(f"window.scrollBy(0, {step_size});")
                time.sleep(interval)
            # Пауза между группами скроллов
            time.sleep(random.uniform(1.0, 3.5))
            # Иногда скролл назад — как реальный пользователь
            if random.random() < 0.15:
                self.driver.execute_script(f"window.scrollBy(0, -{random.randint(50, 150)});")
                time.sleep(random.uniform(0.5, 1.5))

    # ──────────────────────────────────────────────────────────
    # BEZIER MOUSE MOVEMENT
    # Реальная мышь движется по кривой, не по прямой.
    # Генерируем точки по кубической кривой Безье
    # ──────────────────────────────────────────────────────────

    def _bezier_point(self, t: float, p0, p1, p2, p3) -> tuple:
        """Кубическая кривая Безье: возвращает (x, y) для параметра t ∈ [0,1]"""
        u = 1 - t
        x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
        y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
        return (int(x), int(y))

    def bezier_move_to(self, element):
        """
        Плавное движение мыши к элементу + клик.
        Использует move_to_element_with_offset (от самого элемента) —
        это работает с абсолютными координатами относительно элемента.
        """
        try:
            # Прокручиваем элемент в вид если он вне экрана
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'})",
                element
            )
            time.sleep(random.uniform(0.2, 0.4))

            # Размеры элемента
            w = max(element.size.get("width", 10), 1)
            h = max(element.size.get("height", 10), 1)

            # Генерируем bezier-путь в координатах элемента
            # Начинаем с угла элемента, заканчиваем в его центре
            start = (random.randint(-int(w / 3), 0), random.randint(-int(h / 3), 0))
            end   = (random.randint(-int(w / 5), int(w / 5)),
                     random.randint(-int(h / 5), int(h / 5)))
            cp1   = (start[0] + random.randint(-30, 30), start[1] + random.randint(-30, 30))
            cp2   = (end[0]   + random.randint(-30, 30), end[1]   + random.randint(-30, 30))

            steps = random.randint(8, 15)

            # Каждый шаг — новый ActionChains с АБСОЛЮТНОЙ позицией от элемента
            for i in range(steps + 1):
                t = i / steps
                t_e = t * t * (3 - 2 * t)  # ease in/out
                px, py = self._bezier_point(t_e, start, cp1, cp2, end)
                try:
                    ac = ActionChains(self.driver)
                    ac.move_to_element_with_offset(element, int(px), int(py))
                    ac.perform()
                except Exception:
                    pass
                time.sleep(random.uniform(0.008, 0.025))

            # Финальная пауза и клик
            ac = ActionChains(self.driver)
            ac.move_to_element_with_offset(element, end[0], end[1])
            ac.pause(random.uniform(0.08, 0.25))
            ac.click()
            ac.perform()

        except Exception as e:
            logging.debug(f"[NKBrowser] bezier_move_to fallback: {e}")
            # Fallback — простой клик
            try:
                element.click()
            except Exception:
                self.driver.execute_script("arguments[0].click()", element)

    def warm_mouse(self):
        """Несколько случайных движений мыши по кривой Безье"""
        try:
            vp_w = self.driver.execute_script("return window.innerWidth")
            vp_h = self.driver.execute_script("return window.innerHeight")
            actions = ActionChains(self.driver)
            prev = (vp_w // 2, vp_h // 2)

            for _ in range(random.randint(3, 6)):
                target = (random.randint(100, vp_w - 100), random.randint(100, vp_h - 200))
                cp1 = (prev[0]   + random.randint(-100, 100), prev[1]   + random.randint(-100, 100))
                cp2 = (target[0] + random.randint(-100, 100), target[1] + random.randint(-100, 100))
                steps = random.randint(15, 30)

                for i in range(1, steps + 1):
                    t = i / steps
                    t_e = t * t * (3 - 2 * t)
                    px, py = self._bezier_point(t_e, prev, cp1, cp2, target)
                    dx, dy = px - (prev[0] if i == 1 else 0), py - (prev[1] if i == 1 else 0)
                    if i > 1:
                        last = self._bezier_point((i - 1) / steps * (1 - ((i-1)/steps)**2 * (3 - 2*(i-1)/steps)), prev, cp1, cp2, target)
                        dx, dy = px - last[0], py - last[1]
                    if dx != 0 or dy != 0:
                        try:
                            actions.move_by_offset(dx, dy)
                            actions.pause(random.uniform(0.005, 0.02))
                        except Exception:
                            break
                prev = target

            actions.perform()
            time.sleep(random.uniform(0.2, 0.6))
        except Exception as e:
            logging.debug(f"[NKBrowser] warm_mouse error: {e}")

    # ──────────────────────────────────────────────────────────
    # CDP NETWORK CONDITIONS
    # Эмулируем реальные сетевые условия через CDP
    # ──────────────────────────────────────────────────────────

    def set_network_conditions(self):
        """Устанавливаем реалистичные сетевые параметры через CDP"""
        fp = self._load_or_create_fingerprint()
        download_bytes = int(fp["connection_downlink"] * 1024 * 1024 / 8)
        upload_bytes   = int(download_bytes * 0.3)
        latency_ms     = fp["connection_rtt"]

        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd("Network.emulateNetworkConditions", {
                "offline":            False,
                "downloadThroughput": download_bytes,
                "uploadThroughput":   upload_bytes,
                "latency":            latency_ms,
            })
            logging.debug(f"[NKBrowser] Network: {fp['connection_downlink']}Mbps, RTT={latency_ms}ms")
        except Exception as e:
            logging.debug(f"[NKBrowser] CDP network conditions: {e}")

    def _set_extra_http_headers(self, fp: dict):
        """
        Устанавливает дополнительные HTTP-заголовки что отправляет настоящий Chrome.
        Chrome 90+ отправляет пачку Sec-CH-UA-* и Sec-Fetch-* заголовков.
        """
        # Client Hints — заголовки что соответствуют userAgentMetadata
        major = fp["chrome_version_major"]
        full  = fp["chrome_version_full"]

        headers = {
            "Accept-Language":  ",".join(fp["languages"]) + ";q=0.9",
            "Sec-CH-UA":        f'"Not_A Brand";v="8", "Chromium";v="{major}", "Google Chrome";v="{major}"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Platform-Version": '"15.0.0"',
            "Sec-CH-UA-Arch":   '"x86"',
            "Sec-CH-UA-Bitness": '"64"',
            "Sec-CH-UA-Full-Version":      f'"{full}"',
            "Sec-CH-UA-Full-Version-List": (
                f'"Not_A Brand";v="8.0.0.0", '
                f'"Chromium";v="{full}", '
                f'"Google Chrome";v="{full}"'
            ),
            "Sec-CH-UA-Model":   '""',
            "Sec-CH-UA-WoW64":   "?0",
        }

        try:
            self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": headers})
            logging.debug("[NKBrowser] Extra HTTP headers установлены")
        except Exception as e:
            logging.debug(f"[NKBrowser] Extra HTTP headers: {e}")

    # ──────────────────────────────────────────────────────────
    # VIEWPORT JITTER
    # window.innerWidth/Height не должны быть ровными числами
    # ──────────────────────────────────────────────────────────

    def apply_viewport_jitter(self):
        """Добавляем случайный сдвиг к размеру окна — как у реального пользователя"""
        fp = self._load_or_create_fingerprint()
        w = fp["screen_width"]  - 80  + random.randint(-15, 15)
        h = fp["screen_height"] - 120 + random.randint(-15, 15)
        self.driver.set_window_size(w, h)

    # ──────────────────────────────────────────────────────────
    # PROFILE WARMUP MANAGER
    # Посещаем сайты и создаём реальную историю профиля
    # ──────────────────────────────────────────────────────────

    def warmup_profile(self, depth: str = "light"):
        """
        Прогрев профиля — имитация живого пользователя.
        depth: 'fast' (5-10с через cookies) | 'hybrid' (20-30с) |
               'light' (1-2 мин) | 'medium' (3-5 мин) | 'full' (7-10 мин)
        """
        # Быстрый — только через cookies
        if depth == "fast":
            from cookie_warmer import CookieWarmer
            CookieWarmer(self.driver).fast_warmup()
            self._log_activity("warmup", "fast")
            return

        # Гибридный — cookies + короткие посещения
        if depth == "hybrid":
            from cookie_warmer import CookieWarmer
            CookieWarmer(self.driver).hybrid_warmup(short_visits=True)
            self._log_activity("warmup", "hybrid")
            return

        # Обычный прогрев с реальными посещениями
        sites = {
            "light": [
                "https://www.google.com",
                "https://www.youtube.com",
            ],
            "medium": [
                "https://www.google.com",
                "https://www.youtube.com",
                "https://www.wikipedia.org",
                "https://www.rozetka.com.ua",
            ],
            "full": [
                "https://www.google.com",
                "https://www.youtube.com",
                "https://www.wikipedia.org",
                "https://www.rozetka.com.ua",
                "https://www.ukr.net",
                "https://www.pravda.com.ua",
                "https://www.moyo.ua",
            ],
        }

        targets = sites.get(depth, sites["light"])
        logging.info(f"[NKBrowser] Прогрев профиля ({depth}): {len(targets)} сайтов")

        for url in targets:
            try:
                self.driver.get(url)
                wait_base = {"light": 5, "medium": 8, "full": 12}.get(depth, 5)
                time.sleep(random.uniform(wait_base, wait_base + 4))

                self._try_accept_cookies()
                self.warm_mouse()
                self.human_scroll(
                    min_scrolls={"light": 1, "medium": 2, "full": 3}.get(depth, 1),
                    max_scrolls={"light": 3, "medium": 5, "full": 7}.get(depth, 3),
                )

                # Записываем в localStorage — признак живого профиля
                self.driver.execute_script(f"""
                    try {{
                        localStorage.setItem('nk_visit_{url.replace("https://", "").replace("/", "_")}', Date.now());
                        localStorage.setItem('nk_visits_count',
                            parseInt(localStorage.getItem('nk_visits_count') || '0') + 1);
                    }} catch(e) {{}}
                """)

                time.sleep(random.uniform(3, 7))

            except Exception as e:
                logging.debug(f"[NKBrowser] warmup {url}: {e}")

        self._log_activity("warmup", depth)
        logging.info("[NKBrowser] Прогрев завершён")

    # ──────────────────────────────────────────────────────────
    # PERSISTENT ACTIVITY LOG
    # Профиль помнит что делал в прошлых сессиях — это делает
    # его более "живым" для long-term детекторов поведения
    # ──────────────────────────────────────────────────────────

    def _log_activity(self, event_type: str, detail: str = ""):
        """Записывает событие в activity.json профиля"""
        activity_file = os.path.join(self.user_data_path, "activity.json")
        entry = {
            "timestamp":  datetime.now().isoformat(timespec="seconds"),
            "event":      event_type,
            "detail":     detail,
        }
        try:
            if os.path.exists(activity_file):
                with open(activity_file, "r", encoding="utf-8") as f:
                    log = json.load(f)
            else:
                log = []
            log.append(entry)
            # Храним только последние 200 событий
            log = log[-200:]
            with open(activity_file, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.debug(f"[NKBrowser] activity log: {e}")

    def get_activity_stats(self) -> dict:
        """Возвращает статистику активности профиля"""
        activity_file = os.path.join(self.user_data_path, "activity.json")
        if not os.path.exists(activity_file):
            return {"total_events": 0, "first_seen": None, "last_seen": None}
        try:
            with open(activity_file, "r", encoding="utf-8") as f:
                log = json.load(f)
            if not log:
                return {"total_events": 0}
            return {
                "total_events": len(log),
                "first_seen":   log[0]["timestamp"],
                "last_seen":    log[-1]["timestamp"],
                "event_types":  {e["event"]: sum(1 for x in log if x["event"] == e["event"]) for e in log},
            }
        except Exception:
            return {"total_events": 0}

    def _try_accept_cookies(self):
        """Принимаем куки-баннеры если появились"""
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        selectors = [
            "//button[contains(text(),'Accept all')]",
            "//button[contains(text(),'Принять все')]",
            "//button[contains(text(),'Прийняти все')]",
            "//button[contains(text(),'Agree')]",
            "//button[@id='L2AGLb']",  # Google consent
        ]
        for xpath in selectors:
            try:
                btn = WebDriverWait(self.driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                time.sleep(random.uniform(1, 2))
                return
            except Exception:
                continue

    # ──────────────────────────────────────────────────────────
    # STEALTH NAVIGATION — реалистичные переходы
    # ──────────────────────────────────────────────────────────

    def stealth_get(self, url: str, referer: str = None):
        """
        Переход на URL с имитацией реального переходf — через referer.
        Если referer не указан, использует последний URL из истории.
        """
        if referer:
            # Устанавливаем Referer через CDP
            try:
                self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                    "headers": {"Referer": referer}
                })
            except Exception:
                pass

        self.driver.get(url)

        # Очищаем override
        try:
            self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}})
        except Exception:
            pass

    def stealth_navigate_chain(self, urls: list, min_pause: float = 3, max_pause: float = 8):
        """
        Переход через цепочку URL с имитацией клика по ссылкам.
        Каждый следующий URL получает Referer от предыдущего.
        """
        prev_url = None
        for url in urls:
            self.stealth_get(url, referer=prev_url)
            time.sleep(random.uniform(min_pause, max_pause))
            self.human_scroll(1, 3)
            prev_url = url

    # ──────────────────────────────────────────────────────────
    # REQUEST BLOCKING — блокировка трекеров и fingerprint-скриптов
    # ──────────────────────────────────────────────────────────

    # Известные fingerprint-библиотеки и трекеры
    _BLOCKED_PATTERNS = [
        "*fingerprintjs*",
        "*fpjs*",
        "*forter*",
        "*perimeterx*",
        "*distilnetworks*",
        "*datadome*",
        "*imperva*",
        "*castle.io*",
        "*sift.com*",
        "*shieldsquare*",
    ]

    def enable_request_blocking(self, extra_patterns: list = None):
        """
        Блокирует запросы к fingerprint-библиотекам через CDP.
        Снижает шанс детекта ценой возможной поломки некоторых сайтов.
        """
        patterns = self._BLOCKED_PATTERNS + (extra_patterns or [])
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": patterns})
            logging.info(f"[NKBrowser] Блокировка активна для {len(patterns)} паттернов")
        except Exception as e:
            logging.warning(f"[NKBrowser] Request blocking: {e}")

    def disable_request_blocking(self):
        """Снимает блокировку запросов"""
        try:
            self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # RETRY + SCREENSHOT ON ERROR
    # ──────────────────────────────────────────────────────────

    def safe_execute(self, action_fn, description: str = "action",
                     retries: int = 3, screenshot_on_fail: bool = True):
        """
        Выполняет action_fn с ретраями.
        При последнем провале — скриншот в папку профиля.

        Пример:
            browser.safe_execute(
                lambda: driver.find_element(By.NAME, 'q').send_keys('test'),
                description='search input'
            )
        """
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return action_fn()
            except Exception as e:
                last_error = e
                logging.warning(f"[NKBrowser] {description} попытка {attempt}/{retries}: {e}")
                if attempt < retries:
                    time.sleep(random.uniform(1.5, 3.5))

        if screenshot_on_fail:
            self.save_screenshot(f"error_{description.replace(' ', '_')}")
        raise last_error

    def save_screenshot(self, name: str = None) -> str:
        """Сохраняет скриншот в папку профиля/screenshots/"""
        if name is None:
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
        ss_dir = os.path.join(self.user_data_path, "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        path = os.path.join(ss_dir, f"{name}.png")
        try:
            self.driver.save_screenshot(path)
            logging.info(f"[NKBrowser] 📸 Скриншот: {path}")
            return path
        except Exception as e:
            logging.warning(f"[NKBrowser] Скриншот не удался: {e}")
            return ""

    # ──────────────────────────────────────────────────────────
    # FILE LOGGING — отдельный лог на каждый профиль
    # ──────────────────────────────────────────────────────────

    def setup_profile_logging(self, level=logging.INFO):
        """Добавляет файловый логгер для этого профиля"""
        log_dir = os.path.join(self.user_data_path, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, datetime.now().strftime("%Y%m%d.log"))

        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        logging.getLogger().addHandler(handler)
        logging.info(f"[NKBrowser] Лог: {log_file}")

    # ──────────────────────────────────────────────────────────
    # BROWSER RECOVERY — перезапуск при крахе
    # ──────────────────────────────────────────────────────────

    def is_alive(self) -> bool:
        """Проверяет жив ли драйвер"""
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def restart(self):
        """Перезапускает браузер с сохранением профиля и сессии"""
        logging.warning("[NKBrowser] Перезапуск браузера...")
        try:
            if self.auto_session and self.driver and self.is_alive():
                self._auto_save_session()
        except Exception:
            pass
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        time.sleep(2)
        self.start()
        logging.info("[NKBrowser] ✓ Браузер перезапущен")

    # ──────────────────────────────────────────────────────────
    # HEALTH CHECK — проверка качества фингерпринта
    # ──────────────────────────────────────────────────────────

    def health_check(self, verbose: bool = True) -> dict:
        """
        Проверяет качество фингерпринта через JS-тесты.
        Возвращает словарь { тест: результат }.
        Запускать ПОСЛЕ start() но до реальной работы.
        """
        self.driver.get("about:blank")
        time.sleep(1)

        tests = {
            "webdriver":           "navigator.webdriver === undefined",
            "plugins":             "navigator.plugins.length > 0",
            "mime_types":          "navigator.mimeTypes.length > 0",
            "languages":           "navigator.languages.length > 0",
            "chrome_object":       "typeof window.chrome === 'object' && window.chrome !== null",
            "permissions":         "typeof navigator.permissions === 'object'",
            "webgl_vendor":        "(() => { try { const c = document.createElement('canvas').getContext('webgl'); return c.getParameter(37445).includes('Google') || c.getParameter(37445).includes('NVIDIA') || c.getParameter(37445).includes('Intel') || c.getParameter(37445).includes('AMD'); } catch(e) { return false; }})()",
            "hardware_concurrency":"navigator.hardwareConcurrency > 0",
            "device_memory":       "navigator.deviceMemory > 0",
            "connection":          "navigator.connection !== undefined",
            "user_agent":          "navigator.userAgent.includes('Chrome') && !navigator.userAgent.includes('HeadlessChrome')",
            "outer_dimensions":    "window.outerWidth > 0 && window.outerHeight > 0",
            "no_cdc_leak":         "!Object.keys(window).some(k => k.startsWith('$cdc_') || k.startsWith('$wdc_'))",
            "iframe_webdriver":    "(() => { const f = document.createElement('iframe'); document.body.appendChild(f); const r = f.contentWindow.navigator.webdriver === undefined; f.remove(); return r; })()",
            "toString_native":     "HTMLCanvasElement.prototype.toDataURL.toString().includes('[native code]')",
            "no_automation_marks": "typeof window.__playwright === 'undefined' && typeof window.__puppeteer_evaluation_script__ === 'undefined' && typeof window._Selenium_IDE_Recorder === 'undefined'",
            "screen_coords":       "window.screenX > 0 || window.screenY > 0",
            "chrome_loadTimes":    "typeof window.chrome.loadTimes === 'function'",
            "media_devices":       "!!(navigator.mediaDevices && typeof navigator.mediaDevices.enumerateDevices === 'function') || !window.isSecureContext",
        }

        results = {}
        for name, code in tests.items():
            try:
                result = self.driver.execute_script(f"return Boolean({code});")
                results[name] = bool(result)
            except Exception as e:
                results[name] = f"error: {str(e)[:50]}"

        if verbose:
            passed = sum(1 for v in results.values() if v is True)
            total  = len(results)
            logging.info(f"[NKBrowser] Health check: {passed}/{total} тестов пройдено")
            for name, result in results.items():
                icon = "✓" if result is True else "✗"
                logging.info(f"  {icon} {name}: {result}")

        return results

    def health_check_external(self):
        """
        Открывает bot.sannysoft.com — визуальная проверка в браузере.
        Позволяет увидеть результаты популярного анти-бот теста.
        """
        logging.info("[NKBrowser] Открываем bot.sannysoft.com для визуальной проверки")
        self.driver.get("https://bot.sannysoft.com/")
        time.sleep(5)

    def smart_dwell(self, min_sec: float = 3.0, max_sec: float = 20.0):
        """
        Умное время "чтения" страницы — зависит от контента.
        Длинная страница → дольше читаем. Видео → ещё дольше.
        Короткая — быстрее уходим.
        """
        try:
            info = self.driver.execute_script("""
                return {
                    textLength:   (document.body.innerText || '').length,
                    hasVideo:     document.querySelector('video, iframe[src*="youtube"]') !== null,
                    hasForm:      document.querySelector('form') !== null,
                    imagesCount:  document.images.length,
                    scrollHeight: document.documentElement.scrollHeight,
                    viewHeight:   window.innerHeight,
                };
            """)

            # Базовое время чтения: ~250 слов в минуту = ~1500 символов/мин
            # Т.е. ~25 символов в секунду
            text_len = info.get("textLength", 0)
            base_read_time = text_len / 25 if text_len else 3

            # Люди не читают всё — только ~15-30% страницы
            actual_read = base_read_time * random.uniform(0.15, 0.30)

            # Корректировки
            if info.get("hasVideo"):
                actual_read += random.uniform(10, 30)  # остановились посмотреть видео
            if info.get("imagesCount", 0) > 10:
                actual_read += random.uniform(2, 5)    # разглядываем картинки

            # Ограничиваем пределами
            dwell = max(min_sec, min(max_sec, actual_read))

            logging.debug(f"[NKBrowser] smart_dwell: {dwell:.1f}с (text={text_len}, video={info.get('hasVideo')})")
            time.sleep(dwell)
        except Exception:
            time.sleep(random.uniform(min_sec, max_sec))

    def idle_pause(self, kind: str = "random"):
        """
        Имитация того что юзер отвлёкся.
        kind:
          "micro"  — 3-10 сек (посмотрел на телефон)
          "short"  — 30-90 сек (попил воды)
          "medium" — 5-15 мин (туалет, перекур)
          "long"   — 30-60 мин (обед, встреча)
          "random" — случайно выбирает с реалистичными весами
        """
        if kind == "random":
            # Распределение похожее на реального юзера:
            # большую часть времени ничего или микро-отвлечения
            kind = random.choices(
                ["none", "micro", "short", "medium", "long"],
                weights=[60, 25, 10, 4, 1],
                k=1,
            )[0]

        if kind == "none":
            return

        ranges = {
            "micro":  (3, 10),
            "short":  (30, 90),
            "medium": (300, 900),
            "long":   (1800, 3600),
        }
        low, high = ranges.get(kind, (5, 15))
        pause = random.uniform(low, high)

        logging.info(f"[NKBrowser] 💤 Пауза {kind}: {pause:.0f}с")
        time.sleep(pause)

    def random_pause(self, min_sec: float = 1.0, max_sec: float = 4.0):
        """Простая случайная пауза (для обратной совместимости)"""
        time.sleep(random.uniform(min_sec, max_sec))

    # ──────────────────────────────────────────────────────────
    # SEARCH SUGGESTIONS INTERACTION
    # ──────────────────────────────────────────────────────────

    def wait_and_interact_with_suggestions(
        self,
        search_box,
        click_probability: float = 0.35,
        partial_typing: bool = False,
    ) -> bool:
        """
        После ввода запроса ждёт появления autocomplete подсказок,
        с вероятностью click_probability кликает по одной из них
        вместо Enter. Это то что делает живой юзер.

        Возвращает True если кликнули по подсказке (тогда Enter не нужен),
        False если решили не кликать (делаем Enter как обычно).

        partial_typing — если True, значит ввели не весь запрос а только часть
        (для имитации: начал вводить, увидел что нужно в подсказках, кликнул)
        """
        # Ждём появления подсказок
        time.sleep(random.uniform(0.4, 1.0))

        try:
            suggestions = self.driver.find_elements(
                By.CSS_SELECTOR,
                'ul[role="listbox"] li[role="option"], '
                '.sbct, .wM6W7d, .G43f7e, [role="option"]'
            )
        except Exception:
            return False

        if not suggestions:
            return False

        # Фильтруем только видимые
        visible = []
        for s in suggestions:
            try:
                if s.is_displayed():
                    visible.append(s)
            except Exception:
                continue

        if not visible:
            return False

        logging.debug(f"[NKBrowser] Подсказок найдено: {len(visible)}")

        # С заданной вероятностью кликаем по случайной подсказке из первых 3
        if random.random() < click_probability:
            chosen = random.choice(visible[:min(3, len(visible))])
            logging.info(f"[NKBrowser] 👆 Клик по подсказке: '{chosen.text[:50]}'")

            # Даём подумать — как будто увидел и решил
            time.sleep(random.uniform(0.4, 1.2))

            try:
                self.bezier_move_to(chosen)
                return True
            except Exception:
                try:
                    chosen.click()
                    return True
                except Exception:
                    pass

        return False

    # ──────────────────────────────────────────────────────────
    # ЗАВЕРШЕНИЕ
    # ──────────────────────────────────────────────────────────

    def close(self):
        # Сохраняем сессию перед закрытием — только если драйвер жив
        if self.driver and self.auto_session and self.is_alive():
            try:
                self._auto_save_session()
            except Exception as e:
                logging.warning(f"[NKBrowser] Auto-save сессии: {e}")

        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

        # Останавливаем локальный прокси-форвардер
        if self._proxy_forwarder:
            try:
                self._proxy_forwarder.stop()
                logging.info("[NKBrowser] Прокси-форвардер остановлен")
            except Exception:
                pass

        logging.info(f"[NKBrowser] Сессия завершена. Профиль: {self.profile_name}")

    # ──────────────────────────────────────────────────────────
    # ROTATING PROXY SUPPORT
    # ──────────────────────────────────────────────────────────

    def get_rotating_tracker(self):
        """Ленивая инициализация трекера IP-ротации"""
        if self._rotating_tracker is None and self.is_rotating_proxy:
            from rotating_proxy import RotatingProxyTracker
            self._rotating_tracker = RotatingProxyTracker(
                proxy_url        = self.proxy_str,
                rotation_api_url = self.rotation_api_url,
                state_file       = os.path.join(self.user_data_path, "rotating_ips.json"),
            )
        return self._rotating_tracker

    def check_and_rotate_if_burned(self) -> str | None:
        """
        Проверяет текущий IP. Если он в списке сгоревших — форсит ротацию.
        Возвращает итоговый IP.
        """
        tracker = self.get_rotating_tracker()
        if not tracker:
            return None

        current_ip = tracker.get_current_ip(self.driver)
        if not current_ip:
            return None

        if tracker.is_ip_burned(current_ip):
            logging.warning(f"[NKBrowser] IP {current_ip} сгоревший — ротируем")
            tracker.force_rotate()
            time.sleep(random.uniform(3, 8))
            # Ждём смены
            new_ip = tracker.wait_for_rotation(self.driver, current_ip, timeout=60)
            if new_ip:
                current_ip = new_ip

        # Enrich метаданные (один раз на IP)
        tracker.enrich_ip_info(current_ip, self.driver)
        return current_ip

    def report_rotating(self, ip: str, success: bool = True, captcha: bool = False):
        """Отчитывается трекеру о результате"""
        tracker = self.get_rotating_tracker()
        if tracker and ip:
            tracker.report(ip, success=success, captcha=captcha)
