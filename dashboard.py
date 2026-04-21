"""
dashboard.py — HTML дэшборд со статистикой

Генерирует один HTML-файл с интерактивными графиками:
- Здоровье всех профилей
- Статистика прокси/IP
- Капча-rate по дням
- Топ конкурентов в рекламе
- График запусков

Использует Chart.js из CDN — не требует установки.

Запуск:
    python dashboard.py               # генерирует reports/dashboard.html
    python dashboard.py --open        # генерирует и открывает в браузере
"""

import os
import json
import glob
import logging
import webbrowser
from datetime import datetime, timedelta
from collections import defaultdict


def collect_profile_stats(profiles_dir: str = "profiles") -> list[dict]:
    """Собирает статистику по всем профилям"""
    if not os.path.exists(profiles_dir):
        return []

    profiles = []
    for name in sorted(os.listdir(profiles_dir)):
        path = os.path.join(profiles_dir, name)
        if not os.path.isdir(path):
            continue

        profile_data = {
            "name":         name,
            "has_session":  os.path.exists(os.path.join(path, "nk_session")),
            "health":       None,
            "activity":     [],
            "metrics":      [],
            "fingerprint":  None,
        }

        # Здоровье
        sq_file = os.path.join(path, "session_quality.json")
        if os.path.exists(sq_file):
            try:
                with open(sq_file, "r", encoding="utf-8") as f:
                    profile_data["metrics"] = json.load(f)
            except Exception:
                pass

        # Активность
        act_file = os.path.join(path, "activity.json")
        if os.path.exists(act_file):
            try:
                with open(act_file, "r", encoding="utf-8") as f:
                    profile_data["activity"] = json.load(f)
            except Exception:
                pass

        # Fingerprint (краткая инфа)
        fp_file = os.path.join(path, "fingerprint.json")
        if os.path.exists(fp_file):
            try:
                with open(fp_file, "r", encoding="utf-8") as f:
                    fp = json.load(f)
                profile_data["fingerprint"] = {
                    "template":  fp.get("template_name"),
                    "webgl":     fp.get("webgl_renderer", "")[:50],
                    "screen":    f"{fp.get('screen_width')}x{fp.get('screen_height')}",
                    "chrome":    fp.get("chrome_version_major"),
                    "languages": fp.get("languages"),
                }
            except Exception:
                pass

        profiles.append(profile_data)

    return profiles


def collect_latest_competitors(reports_dir: str = "reports", limit: int = 1) -> list[dict]:
    """Возвращает конкурентов из последнего JSON отчёта"""
    if not os.path.exists(reports_dir):
        return []

    files = sorted(glob.glob(os.path.join(reports_dir, "competitors_*.json")), reverse=True)
    if not files:
        return []

    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def collect_proxy_stats() -> dict:
    """Статистика по rotating-прокси"""
    for profile_dir in glob.glob("profiles/*/"):
        state_file = os.path.join(profile_dir, "rotating_ips.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    # Fallback — глобальный state
    if os.path.exists("rotating_proxy_state.json"):
        try:
            with open("rotating_proxy_state.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"ips": {}}


# ──────────────────────────────────────────────────────────────
# АГРЕГАЦИЯ
# ──────────────────────────────────────────────────────────────

def aggregate_by_day(metrics: list[dict]) -> dict:
    """Агрегирует метрики по дням"""
    by_day = defaultdict(lambda: {"searches": 0, "captchas": 0, "blocks": 0, "empty": 0})

    for m in metrics:
        try:
            day = m["timestamp"][:10]  # YYYY-MM-DD
            event = m.get("event", "")
            if event in ("search_ok",):
                by_day[day]["searches"] += 1
            elif event == "search_empty":
                by_day[day]["empty"] += 1
            elif event == "captcha":
                by_day[day]["captchas"] += 1
            elif event == "blocked":
                by_day[day]["blocks"] += 1
        except Exception:
            continue

    # Сортируем
    return dict(sorted(by_day.items()))


# ──────────────────────────────────────────────────────────────
# HTML ГЕНЕРАЦИЯ
# ──────────────────────────────────────────────────────────────

def generate_html(
    profiles:    list[dict],
    competitors: list[dict],
    proxy_stats: dict,
    output:      str = "reports/dashboard.html",
) -> str:
    """Генерирует HTML dashboard"""

    # Агрегация данных для графиков по всем профилям
    all_metrics = []
    for p in profiles:
        all_metrics.extend(p.get("metrics", []))
    daily = aggregate_by_day(all_metrics)

    # Топ IP
    ips = proxy_stats.get("ips", {})
    ip_list = sorted(
        [(ip, s) for ip, s in ips.items()],
        key=lambda x: x[1].get("total_uses", 0),
        reverse=True,
    )[:10]

    # Данные для JS
    js_data = {
        "dailyLabels":   list(daily.keys()),
        "dailySearches": [d["searches"] for d in daily.values()],
        "dailyCaptchas": [d["captchas"] for d in daily.values()],
        "dailyBlocks":   [d["blocks"]   for d in daily.values()],
        "profiles": [
            {
                "name":   p["name"],
                "health": _compute_profile_health(p["metrics"]),
                "searches": sum(1 for m in p["metrics"] if m.get("event") == "search_ok"),
                "captchas": sum(1 for m in p["metrics"] if m.get("event") == "captcha"),
            }
            for p in profiles
        ],
    }

    html = _HTML_TEMPLATE.replace("{{JS_DATA}}", json.dumps(js_data, ensure_ascii=False))
    html = html.replace("{{PROFILES_TABLE}}",    _render_profiles_table(profiles))
    html = html.replace("{{COMPETITORS_TABLE}}", _render_competitors_table(competitors))
    html = html.replace("{{IP_TABLE}}",          _render_ip_table(ip_list))
    html = html.replace("{{GENERATED_AT}}",      datetime.now().strftime("%Y-%m-%d %H:%M"))

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)

    logging.info(f"[Dashboard] ✓ Сгенерирован: {output}")
    return output


def _compute_profile_health(metrics: list[dict]) -> str:
    """Быстрая оценка здоровья по метрикам"""
    if not metrics:
        return "unknown"

    # Последние 24ч
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    recent = [m for m in metrics if m.get("timestamp", "") >= cutoff]

    if not recent:
        return "idle"

    searches = sum(1 for m in recent if m.get("event") in ("search_ok", "search_empty"))
    captchas = sum(1 for m in recent if m.get("event") == "captcha")
    total    = searches + captchas

    if total == 0:
        return "idle"
    rate = captchas / total

    if rate >= 0.5:
        return "critical"
    elif rate >= 0.2:
        return "warning"
    return "healthy"


def _render_profiles_table(profiles: list[dict]) -> str:
    if not profiles:
        return "<tr><td colspan='6' class='empty'>Профилей пока нет</td></tr>"

    rows = []
    for p in profiles:
        health   = _compute_profile_health(p.get("metrics", []))
        searches = sum(1 for m in p.get("metrics", []) if m.get("event") == "search_ok")
        captchas = sum(1 for m in p.get("metrics", []) if m.get("event") == "captcha")
        fp       = p.get("fingerprint") or {}

        health_class = {
            "healthy":  "status-healthy",
            "warning":  "status-warning",
            "critical": "status-critical",
            "idle":     "status-idle",
            "unknown":  "status-idle",
        }.get(health, "status-idle")

        rows.append(f"""
          <tr>
            <td><strong>{p['name']}</strong></td>
            <td><span class="status {health_class}">{health}</span></td>
            <td>{searches}</td>
            <td>{captchas}</td>
            <td>{fp.get('template', '-')}</td>
            <td class="muted">{fp.get('webgl', '-')}</td>
          </tr>
        """)
    return "\n".join(rows)


def _render_competitors_table(competitors: list[dict]) -> str:
    if not competitors:
        return "<tr><td colspan='4' class='empty'>Конкурентов пока не найдено</td></tr>"

    sorted_comp = sorted(competitors, key=lambda c: -len(c.get("queries", [])))
    rows = []
    for c in sorted_comp:
        queries = " · ".join(c.get("queries", []))
        clean = c.get("clean_url") or c.get("real_url", "")
        rows.append(f"""
          <tr>
            <td><strong>{c.get('domain', '?')}</strong></td>
            <td>{c.get('title', '')[:80]}</td>
            <td><a href="{clean}" target="_blank" class="link">{clean[:60]}</a></td>
            <td class="muted">{queries}</td>
          </tr>
        """)
    return "\n".join(rows)


def _render_ip_table(ip_list: list) -> str:
    if not ip_list:
        return "<tr><td colspan='5' class='empty'>IP статистики пока нет</td></tr>"

    rows = []
    for ip, s in ip_list:
        uses  = s.get("total_uses", 0)
        capt  = s.get("total_captchas", 0)
        rate  = (capt / uses) if uses else 0
        burned = bool(s.get("burned_at"))

        status = "🔥 burned" if burned else ("⚠ warning" if rate > 0.3 else "🟢 good")
        rows.append(f"""
          <tr>
            <td>{ip}</td>
            <td>{s.get('country', '-')}</td>
            <td>{uses}</td>
            <td>{rate:.0%}</td>
            <td>{status}</td>
          </tr>
        """)
    return "\n".join(rows)


# ──────────────────────────────────────────────────────────────
# HTML ТЕМПЛЕЙТ
# ──────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<title>NK Browser Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg:        #0f1419;
    --card:      #1a1f29;
    --border:    #2a2f3a;
    --text:      #e6e6e6;
    --muted:     #8a8f99;
    --accent:    #5a9fed;
    --healthy:   #4ade80;
    --warning:   #f59e0b;
    --critical:  #ef4444;
    --idle:      #6b7280;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.5;
  }
  header {
    max-width: 1280px;
    margin: 0 auto 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  header h1 { font-size: 28px; font-weight: 600; }
  .generated { color: var(--muted); font-size: 13px; }

  .grid { max-width: 1280px; margin: 0 auto; display: grid; gap: 20px; }
  .grid.g2 { grid-template-columns: 1fr 1fr; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }
  .card h2 {
    font-size: 16px;
    font-weight: 500;
    margin-bottom: 16px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .card h2 .count {
    background: var(--border);
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text);
    margin-left: 8px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    padding: 8px 12px;
    color: var(--muted);
    font-weight: 500;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }
  tr:last-child td { border-bottom: none; }
  .muted { color: var(--muted); font-size: 12px; }
  .empty { text-align: center; color: var(--muted); padding: 24px; }

  .link { color: var(--accent); text-decoration: none; }
  .link:hover { text-decoration: underline; }

  .status {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .status-healthy  { background: rgba(74, 222, 128, 0.15); color: var(--healthy); }
  .status-warning  { background: rgba(245, 158, 11, 0.15); color: var(--warning); }
  .status-critical { background: rgba(239, 68, 68, 0.15);  color: var(--critical); }
  .status-idle     { background: rgba(107, 114, 128, 0.15); color: var(--idle); }

  canvas { max-height: 280px; }
</style>
</head>
<body>

<header>
  <h1>NK Browser Dashboard</h1>
  <div class="generated">Обновлено: {{GENERATED_AT}}</div>
</header>

<div class="grid">

  <!-- График по дням -->
  <div class="card">
    <h2>Активность по дням</h2>
    <canvas id="dailyChart"></canvas>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>Профили</h2>
      <table>
        <thead>
          <tr>
            <th>Имя</th>
            <th>Статус</th>
            <th>Поиски</th>
            <th>Капчи</th>
            <th>Шаблон</th>
            <th>GPU</th>
          </tr>
        </thead>
        <tbody>{{PROFILES_TABLE}}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>IP Statistics</h2>
      <table>
        <thead>
          <tr>
            <th>IP</th>
            <th>Страна</th>
            <th>Uses</th>
            <th>Captcha %</th>
            <th>Статус</th>
          </tr>
        </thead>
        <tbody>{{IP_TABLE}}</tbody>
      </table>
    </div>
  </div>

  <!-- Конкуренты -->
  <div class="card">
    <h2>Конкуренты в контекстной рекламе</h2>
    <table>
      <thead>
        <tr>
          <th>Домен</th>
          <th>Заголовок</th>
          <th>URL</th>
          <th>По запросам</th>
        </tr>
      </thead>
      <tbody>{{COMPETITORS_TABLE}}</tbody>
    </table>
  </div>

</div>

<script>
const data = {{JS_DATA}};

// График по дням
new Chart(document.getElementById('dailyChart'), {
  type: 'line',
  data: {
    labels: data.dailyLabels,
    datasets: [
      {
        label: 'Успешные поиски',
        data: data.dailySearches,
        borderColor: '#4ade80',
        backgroundColor: 'rgba(74, 222, 128, 0.1)',
        tension: 0.3,
        fill: true,
      },
      {
        label: 'Капчи',
        data: data.dailyCaptchas,
        borderColor: '#f59e0b',
        backgroundColor: 'rgba(245, 158, 11, 0.1)',
        tension: 0.3,
        fill: true,
      },
      {
        label: 'Блокировки',
        data: data.dailyBlocks,
        borderColor: '#ef4444',
        backgroundColor: 'rgba(239, 68, 68, 0.1)',
        tension: 0.3,
        fill: true,
      },
    ],
  },
  options: {
    responsive: true,
    plugins: {
      legend: { labels: { color: '#e6e6e6' } },
    },
    scales: {
      y: { ticks: { color: '#8a8f99' }, grid: { color: '#2a2f3a' } },
      x: { ticks: { color: '#8a8f99' }, grid: { color: '#2a2f3a' } },
    },
  },
});
</script>

</body>
</html>
"""


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    profiles    = collect_profile_stats()
    competitors = collect_latest_competitors()
    proxy_stats = collect_proxy_stats()

    output = generate_html(profiles, competitors, proxy_stats)
    print(f"✓ Dashboard: {output}")

    if "--open" in sys.argv:
        webbrowser.open("file://" + os.path.abspath(output))
