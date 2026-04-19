"""
scheduler.py — Планировщик запусков мониторинга

Запускает main.run_monitor() N раз в день в заданном временном окне
со случайными интервалами (не равномерно — это палит бота).

Особенности:
- Хранит состояние в scheduler_state.json (можно перезапускать)
- Автоматический reset счётчика в полночь
- Сон вне рабочего окна
- Exponential backoff при повторных ошибках
- Graceful shutdown по Ctrl+C
"""

import os
import json
import time
import random
import logging
import signal
import sys
from datetime import datetime, timedelta, time as dtime

from main import run_monitor

# ──────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────────────────────

TARGET_RUNS_PER_DAY = 100
ACTIVE_HOURS        = (7, 20)   # [7:00, 20:00)
STATE_FILE          = "scheduler_state.json"
MIN_INTERVAL_SEC    = 180       # минимум 3 минуты между запусками
MAX_INTERVAL_SEC    = 1200      # максимум 20 минут
MAX_CONSECUTIVE_FAILS = 5       # после 5 подряд падений — длинная пауза

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ]
)


# ──────────────────────────────────────────────────────────────
# СОСТОЯНИЕ
# ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "date":           "",
            "runs_today":     0,
            "fails_today":    0,
            "consecutive_fails": 0,
            "last_run":       None,
            "history":        [],
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "runs_today": 0, "fails_today": 0, "consecutive_fails": 0, "last_run": None, "history": []}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def reset_if_new_day(state: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        # Сохраняем вчерашнюю статистику в историю
        if state.get("date"):
            state["history"].append({
                "date":  state["date"],
                "runs":  state.get("runs_today", 0),
                "fails": state.get("fails_today", 0),
            })
            # Храним только последние 30 дней
            state["history"] = state["history"][-30:]

        logging.info(f"🌅 Новый день: {today}")
        state.update({
            "date":              today,
            "runs_today":        0,
            "fails_today":       0,
            "consecutive_fails": 0,
            "last_run":          None,
        })
        save_state(state)
    return state


# ──────────────────────────────────────────────────────────────
# ВРЕМЯ
# ──────────────────────────────────────────────────────────────

def is_active_time() -> bool:
    now = datetime.now().time()
    return dtime(ACTIVE_HOURS[0]) <= now < dtime(ACTIVE_HOURS[1])


def time_until_next_active() -> float:
    """Сколько секунд до следующего запуска рабочего окна"""
    now    = datetime.now()
    target = now.replace(hour=ACTIVE_HOURS[0], minute=0, second=0, microsecond=0)
    if now.time() >= dtime(ACTIVE_HOURS[1]):
        # После закрытия — ждём завтра
        target += timedelta(days=1)
    elif now.time() >= dtime(ACTIVE_HOURS[0]):
        # Мы внутри окна, но зачем-то вызвали? всё равно ждём следующее утро
        target += timedelta(days=1)
    return max(60, (target - now).total_seconds())


def minutes_remaining_today() -> float:
    """Сколько минут осталось до конца рабочего окна"""
    now      = datetime.now()
    end_time = now.replace(hour=ACTIVE_HOURS[1], minute=0, second=0, microsecond=0)
    return max(0, (end_time - now).total_seconds() / 60)


def calc_interval(state: dict) -> float:
    """
    Рассчитывает интервал до следующего запуска с учётом:
    - Сколько осталось времени до конца окна
    - Сколько запусков осталось сделать
    - Случайный jitter 50-150%
    """
    remaining_runs = max(1, TARGET_RUNS_PER_DAY - state["runs_today"])
    remaining_min  = minutes_remaining_today()

    if remaining_min <= 0:
        return 0  # окно закрыто

    # Средний интервал
    avg_sec = (remaining_min * 60) / remaining_runs

    # Jitter — случайное отклонение 50%...150%
    interval = avg_sec * random.uniform(0.5, 1.5)

    # Ограничиваем
    interval = max(MIN_INTERVAL_SEC, min(MAX_INTERVAL_SEC, interval))

    # Backoff при ошибках
    if state.get("consecutive_fails", 0) > 0:
        backoff_mult = min(8, 2 ** state["consecutive_fails"])
        interval *= backoff_mult
        logging.warning(f"⚠ Backoff x{backoff_mult} (подряд ошибок: {state['consecutive_fails']})")

    return interval


# ──────────────────────────────────────────────────────────────
# ОСТАНОВКА
# ──────────────────────────────────────────────────────────────

_shutdown = False

def _signal_handler(signum, frame):
    global _shutdown
    logging.info("🛑 Получен сигнал остановки, завершаем после текущей итерации...")
    _shutdown = True

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def interruptible_sleep(seconds: float):
    """Спит указанное время, но с проверкой на shutdown"""
    end = time.time() + seconds
    while time.time() < end and not _shutdown:
        time.sleep(min(5, end - time.time()))


# ──────────────────────────────────────────────────────────────
# ОСНОВНОЙ ЦИКЛ
# ──────────────────────────────────────────────────────────────

def main():
    logging.info("═" * 60)
    logging.info(f" ПЛАНИРОВЩИК ЗАПУЩЕН")
    logging.info(f" Цель: {TARGET_RUNS_PER_DAY} запусков/день")
    logging.info(f" Окно: {ACTIVE_HOURS[0]:02d}:00 – {ACTIVE_HOURS[1]:02d}:00")
    logging.info("═" * 60)

    while not _shutdown:
        state = load_state()
        state = reset_if_new_day(state)

        # ── Проверка рабочего окна ───────────────────
        if not is_active_time():
            sleep_sec = time_until_next_active()
            next_time = datetime.now() + timedelta(seconds=sleep_sec)
            logging.info(f"💤 Вне рабочего окна. Сплю до {next_time.strftime('%Y-%m-%d %H:%M')} ({sleep_sec/3600:.1f} ч)")
            interruptible_sleep(sleep_sec)
            continue

        # ── Проверка квоты ───────────────────────────
        if state["runs_today"] >= TARGET_RUNS_PER_DAY:
            sleep_sec = time_until_next_active()
            logging.info(f"✅ Квота на сегодня выполнена ({state['runs_today']}/{TARGET_RUNS_PER_DAY}). Сплю до завтра.")
            interruptible_sleep(sleep_sec)
            continue

        # ── Запуск ──────────────────────────────────
        run_num = state["runs_today"] + 1
        logging.info("")
        logging.info(f"▶ Запуск {run_num}/{TARGET_RUNS_PER_DAY} — {datetime.now().strftime('%H:%M:%S')}")

        started_at = time.time()
        try:
            run_monitor()
            duration = time.time() - started_at
            state["runs_today"]        += 1
            state["consecutive_fails"]  = 0
            state["last_run"]           = datetime.now().isoformat(timespec="seconds")
            logging.info(f"✓ Запуск {run_num} успешен ({duration:.0f}с)")

        except KeyboardInterrupt:
            break

        except Exception as e:
            duration = time.time() - started_at
            state["fails_today"]       += 1
            state["consecutive_fails"] += 1
            logging.error(f"✗ Запуск {run_num} провален ({duration:.0f}с): {type(e).__name__}: {e}")

            # Слишком много подряд — длинная пауза
            if state["consecutive_fails"] >= MAX_CONSECUTIVE_FAILS:
                logging.error(f"🚨 {MAX_CONSECUTIVE_FAILS} ошибок подряд — длинная пауза 30 минут")
                save_state(state)
                interruptible_sleep(1800)  # 30 минут
                state["consecutive_fails"] = 0

        save_state(state)

        if _shutdown:
            break

        # ── Сон до следующего запуска ────────────────
        interval = calc_interval(state)
        if interval <= 0:
            continue  # окно закрылось, на следующей итерации пойдёт в sleep

        next_time = datetime.now() + timedelta(seconds=interval)
        logging.info(f"⏰ Следующий запуск в {next_time.strftime('%H:%M:%S')} (через {interval/60:.1f} мин)")
        interruptible_sleep(interval)

    # ── Shutdown ────────────────────────────────────
    logging.info("")
    logging.info("═" * 60)
    logging.info(" ПЛАНИРОВЩИК ОСТАНОВЛЕН")
    logging.info("═" * 60)
    state = load_state()
    logging.info(f" Запусков сегодня: {state.get('runs_today', 0)}/{TARGET_RUNS_PER_DAY}")
    logging.info(f" Ошибок: {state.get('fails_today', 0)}")


if __name__ == "__main__":
    main()
