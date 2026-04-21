# NK Browser — антидетект-автоматизация Chrome

Своя реализация антидетект-браузера на базе `undetected_chromedriver`.
Построена по принципам Dolphin/MultiLogin.

## Возможности

**Антидетект (49 блоков защиты в `fingerprints.js`):**
- Canvas / WebGL / Audio fingerprint spoofing с постоянным шумом
- Navigator properties (plugins, mimeTypes, hardwareConcurrency, deviceMemory)
- Client Hints (Sec-CH-UA-*) отправляются через HTTP-заголовки
- iframe consistency — отпечаток одинаковый в iframe и основном окне
- Web Worker + ServiceWorker navigator spoof
- WebGPU patching (Chrome 113+)
- Font enumeration через document.fonts
- MediaDevices, Battery API, Network Connection API
- Screen coordinates, outer dimensions
- Automation markers cleanup ($cdc_, $wdc_, __playwright и т.д.)
- DevTools detection defense
- Object.toString normalization (все патчи выглядят native)
- Performance.now() timing jitter

**Прокси:**
- Локальный форвардер для авторизованных прокси (без браузерного расширения)
- Поддержка rotating IP (asocks, bright data) с трекингом здоровья каждого IP
- Автоматическая ротация при капче
- Диагностика IP (страна, ASN, datacenter vs residential, WebRTC leak)

**Поведение:**
- Bezier mouse movement с easing
- Human typing с соседними клавишами для опечаток
- Time-of-day awareness (ночью медленнее)
- Search suggestions interaction (кликаем по автокомплиту)
- Tab management (фоновые вкладки как у живого юзера)
- Idle pauses с реалистичным распределением
- Smart dwell time (время на странице зависит от контента)

**Профили:**
- Обогащение новых профилей History/Bookmarks через SQLite
- Cookie warming (мгновенный прогрев через CDP)
- Session save/restore между запусками
- Persistent activity log
- Multi-profile pool с ротацией по здоровью
- Fingerprint immutability guarantee

**Управление:**
- Session quality monitor (авто-определение сгоревших профилей)
- Watchdog от зависаний
- Scheduler для запусков по расписанию
- HTML dashboard с графиками
- YAML конфигурация
- All-in-one diagnose.py

## Установка

```bash
git clone <repo>
cd nk-browser
pip install -r requirements.txt
```

Требования:
- Python 3.10+
- Google Chrome установлен в системе

## Быстрый старт

```bash
# 1. Создать конфиг
python config.py

# 2. Отредактировать config.yaml — прописать прокси, 2captcha ключ, запросы

# 3. Запустить диагностику
python diagnose.py

# 4. Если диагностика прошла — запускаем
python main.py

# 5. Посмотреть отчёт
python dashboard.py --open
```

## Структура

```
.
├── main.py                  # главный скрипт мониторинга
├── nk_browser.py            # ядро антидетект-браузера
├── fingerprints.js          # JS-инъекции (49 блоков защиты)
├── config.py                # загрузка config.yaml
├── config.yaml              # конфигурация
│
├── proxy_forwarder.py       # локальный форвардер прокси
├── proxy_diagnostics.py     # диагностика прокси/IP
├── rotating_proxy.py        # трекер rotating IP (asocks)
├── proxy_pool.py            # пул прокси
│
├── profile_manager.py       # CRUD профилей
├── profile_pool.py          # пул профилей
├── profile_enricher.py      # обогащение History/Bookmarks
├── session_manager.py       # cookies/localStorage save/restore
├── session_quality.py       # мониторинг здоровья профиля
├── cookie_warmer.py         # мгновенный прогрев через cookies
│
├── browsing_patterns.py     # паттерны серфинга
├── tab_manager.py           # управление вкладками
├── device_templates.py      # шаблоны устройств
│
├── watchdog.py              # защита от зависаний
├── scheduler.py             # запуски по расписанию
├── dashboard.py             # HTML-отчёт
├── diagnose.py              # полная диагностика
├── fingerprint_tester.py    # тест стабильности отпечатка
├── creepjs_check.py         # проверка через CreepJS
│
├── main_orchestrated.py     # main с пулами (для множественной автоматизации)
│
├── profiles/                # данные браузерных профилей
├── reports/                 # отчёты JSON/CSV/HTML
└── requirements.txt
```

## Утилиты

```bash
# Управление профилями
python profile_manager.py list
python profile_manager.py create profile_02
python profile_manager.py clone profile_01 profile_02

# Пулы
python profile_pool.py status
python proxy_pool.py status
python proxy_pool.py reset

# Тесты
python diagnose.py                        # полная диагностика
python fingerprint_tester.py profile_01   # стабильность отпечатка
python creepjs_check.py profile_01        # внешний сканер

# Отчёты
python dashboard.py --open

# Расписание
python scheduler.py                       # N запусков в день по окну
```

## Конфигурация (config.yaml)

См. `config.yaml.example`:

```yaml
search:
  queries: [гудмедика, гудмедіка, goodmedika]
  my_domains: [goodmedika.com.ua]

proxy:
  url: "user:pass@host:port"
  is_rotating: true

browser:
  profile_name: profile_01
  device_template: office_laptop

captcha:
  twocaptcha_key: "YOUR_KEY"

behavior:
  open_background_tabs: true
  idle_pauses: true

scheduler:
  target_runs_per_day: 30
  active_hours: [7, 20]
```

## Важные замечания

**Про детект.** Антидетект уменьшает шанс детекта, но не гарантирует его
отсутствие. Google следит не только за отпечатком, но и за поведением
(частота запросов, патрон переходов). Рекомендации:
- Не больше 20-30 запусков в день с одного профиля/IP
- Ротировать профили и прокси для масштаба
- Residential IP работает лучше datacenter

**Про капчу.** Если `session_quality.json` показывает capcha_rate > 50% —
профиль нужно пересоздать (удалить `profiles/<name>` или
`fingerprint.json` + `nk_session/`).

**Про права.** Скрипт для **легальных** задач:
- Мониторинг собственной рекламы
- Конкурентная разведка через публичный поиск
- Автоматизация рутинных задач

Не использовать для клик-фрода, взлома аккаунтов, подделки трафика.

## Лицензия

MIT (если не указано иное в отдельных файлах)
