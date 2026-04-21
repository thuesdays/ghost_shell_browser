"""
config.py — Центральная конфигурация из YAML

Пример config.yaml:
  search:
    queries:
      - гудмедика
      - гудмедіка
      - goodmedika
    my_domains:
      - goodmedika.com.ua

  proxy:
    url: user:pass@host:port
    is_rotating: true
    rotation_api_url: null

  browser:
    profile_name: profile_01
    device_template: office_laptop
    auto_session: true
    enrich_on_create: true

  captcha:
    twocaptcha_key: YOUR_KEY

  behavior:
    open_background_tabs: true
    bg_tabs_count: [2, 4]
    idle_pauses: true
    pre_target_warmup: true
"""

import os
import logging


DEFAULT_CONFIG = {
    "search": {
        "queries":    ["гудмедика", "гудмедіка", "goodmedika"],
        "my_domains": ["goodmedika.com.ua", "goodmedika.ua", "goodmedika.com"],
    },
    "proxy": {
        "url":              "",
        "is_rotating":      True,
        "rotation_api_url": None,
    },
    "browser": {
        "profile_name":     "profile_01",
        "device_template":  "office_laptop",
        "auto_session":     True,
        "enrich_on_create": True,
    },
    "captcha": {
        "twocaptcha_key":   "",
    },
    "behavior": {
        "open_background_tabs": True,
        "bg_tabs_count":        [2, 4],
        "idle_pauses":          True,
        "pre_target_warmup":    True,
    },
    "scheduler": {
        "target_runs_per_day": 30,
        "active_hours":        [7, 20],
    },
    "reports": {
        "dir":    "reports",
        "format": "both",   # json | csv | both
    },
}


class Config:
    """
    Использование:
        cfg = Config.load()
        queries   = cfg.get("search.queries")
        proxy_url = cfg.get("proxy.url")
    """

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        """Загружает config. Если файла нет — использует defaults."""
        data = cls._deep_copy(DEFAULT_CONFIG)

        if not os.path.exists(path):
            logging.info(f"[Config] {path} не найден — используем defaults")
            return cls(data)

        # YAML optional — пробуем, если нет — используем JSON
        loaded = None
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except ImportError:
            logging.warning("[Config] PyYAML не установлен, пробуем JSON")
            try:
                import json
                # Пробуем тот же путь но как JSON
                json_path = path.replace(".yaml", ".json").replace(".yml", ".json")
                if os.path.exists(json_path):
                    with open(json_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
            except Exception as e:
                logging.error(f"[Config] JSON fallback: {e}")
        except Exception as e:
            logging.error(f"[Config] Ошибка чтения {path}: {e}")

        if loaded:
            data = cls._deep_merge(data, loaded)
            logging.info(f"[Config] Загружен: {path}")

        return cls(data)

    # ──────────────────────────────────────────────────────────
    # ДОСТУП
    # ──────────────────────────────────────────────────────────

    def get(self, path: str, default=None):
        """Доступ по точечному пути: cfg.get('search.queries')"""
        parts   = path.split(".")
        current = self._data
        for p in parts:
            if not isinstance(current, dict) or p not in current:
                return default
            current = current[p]
        return current

    def __getitem__(self, key):
        return self.get(key)

    # ──────────────────────────────────────────────────────────
    # УТИЛИТЫ
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _deep_copy(d):
        import copy
        return copy.deepcopy(d)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Рекурсивный merge — override поверх base"""
        result = dict(base)
        for key, value in (override or {}).items():
            if (key in result and isinstance(result[key], dict)
                    and isinstance(value, dict)):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


# ──────────────────────────────────────────────────────────────
# ДЕФОЛТНЫЙ ФАЙЛ — создаёт пример если config.yaml нет
# ──────────────────────────────────────────────────────────────

EXAMPLE_CONFIG_YAML = """# NK Browser конфигурация

search:
  queries:
    - гудмедика
    - гудмедіка
    - goodmedika
  my_domains:
    - goodmedika.com.ua
    - goodmedika.ua
    - goodmedika.com

proxy:
  url: "01kpjw4p1mrn74xw7eq1sd843q:nfoN0DTTFaoUizWj@109.236.84.23:16720"
  is_rotating: true
  rotation_api_url: null  # заполнить если у твоего тарифа asocks есть API

browser:
  profile_name: profile_01
  device_template: office_laptop  # office_desktop | office_laptop | gaming_mid | gaming_high_end | gaming_laptop | budget_desktop | amd_desktop_mid
  auto_session: true
  enrich_on_create: true  # обогащение History/Bookmarks для новых профилей

captcha:
  twocaptcha_key: ""  # ключ API 2Captcha

behavior:
  open_background_tabs: true
  bg_tabs_count: [2, 4]   # диапазон случайного числа табов
  idle_pauses: true       # случайные паузы "юзер отвлёкся"
  pre_target_warmup: true # 1 нейтральный поиск перед целевыми

scheduler:
  target_runs_per_day: 30  # сколько запусков в день
  active_hours: [7, 20]    # рабочее окно (часы)

reports:
  dir: reports
  format: both  # json | csv | both
"""


def create_example_config(path: str = "config.yaml"):
    """Создаёт пример config.yaml"""
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(EXAMPLE_CONFIG_YAML)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if create_example_config():
        print("✓ Создан пример config.yaml")
    else:
        print("config.yaml уже существует")
        cfg = Config.load()
        print(f"Queries: {cfg.get('search.queries')}")
        print(f"Profile: {cfg.get('browser.profile_name')}")
