import csv
import json
import math
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

VERSION = "1.0"
REPO = "clipsearch-app/ClipSearch"
RELEASE_NOTES = """- Первый публичный выпуск ClipSearch.
- Поиск клипов Twitch по стримерам, категориям, авторам и словам из названия за любой период.
- Скачивание в 1080p, 720p или 480p, вырезание фрагмента во встроенном плеере, очередь загрузок.
- Обновления внутри программы."""
GITHUB_API = os.environ.get("CLIPS_GITHUB_API", "https://api.github.com")

WORKER_URL = os.environ.get("CLIPS_WORKER_URL", "https://clipsearch-api.clipsearch-app.workers.dev").rstrip("/")
API_BASE = os.environ.get("CLIPS_API_BASE", WORKER_URL + "/helix")
HEALTH_URL = (API_BASE[: -len("/helix")] if API_BASE.endswith("/helix") else API_BASE) + "/health"
GQL_URL = os.environ.get("CLIPS_GQL_URL", "https://gql.twitch.tv/gql")
GQL_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

MAX_RESULTS = 2000
TWITCH_CLIPS_START = datetime(2016, 5, 1, tzinfo=timezone.utc)

def config_path():
    override = os.environ.get("CLIPS_CONFIG")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ClipSearch", "config.json")

def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_config(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def plural(n, one, few, many):
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many

def period_label(minutes):
    if minutes == 0:
        return "за всё время"
    table = [
        (43200, "месяц", "месяца", "месяцев"),
        (10080, "неделю", "недели", "недель"),
        (1440, "день", "дня", "дней"),
        (60, "час", "часа", "часов"),
        (1, "минуту", "минуты", "минут"),
    ]
    if minutes % 43200 == 0:
        row = table[0]
    elif minutes % 10080 == 0:
        row = table[1]
    elif minutes % 1440 == 0 and minutes > 2880:
        row = table[2]
    elif minutes % 60 == 0:
        row = table[3]
    else:
        row = table[4]
    n = minutes // row[0]
    return f"за {n} {plural(n, row[1], row[2], row[3])}"

def parse_period(raw):
    t = str(raw).lower().replace(",", ".").strip()
    if not t:
        return None
    if re.fullmatch(r"(всё|все|all)(\s*время)?", t):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zа-яё]+)", t)
    if not m:
        return None
    n = float(m.group(1))
    u = m.group(2)
    if re.match(r"(мес|mo)", u):
        mult = 43200
    elif re.match(r"(мин|min|м$|m$)", u):
        mult = 1
    elif re.match(r"(ч|h)", u):
        mult = 60
    elif re.match(r"(д|d)", u):
        mult = 1440
    elif re.match(r"(н|w)", u):
        mult = 10080
    else:
        return None
    minutes = round(n * mult)
    if minutes < 1 or minutes > 3650 * 1440:
        return None
    return minutes

class Cancelled(Exception):
    pass

class TwitchError(Exception):
    pass

def rfc3339(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

class Twitch:

    def get(self, path, params, cancel=None):
        url = API_BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"X-ClipSearch": VERSION, "User-Agent": "ClipSearch/" + VERSION, "Accept": "application/json"}
        last = None
        for attempt in range(5):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    msg = str(json.load(e).get("message", "")).strip()
                except Exception:
                    msg = ""
                if e.code == 429 or (e.code >= 500 and attempt < 2):
                    last = msg or f"ответ {e.code}"
                    time.sleep(2 + attempt * 3)
                    continue
                if e.code >= 500:
                    raise TwitchError(msg or f"Сервер ClipSearch временно недоступен (ответ {e.code}).")
                raise TwitchError(f"Twitch ответил {e.code}. {msg}".strip())
            except urllib.error.URLError as e:
                last = getattr(e, "reason", e)
                time.sleep(1 + attempt)
            except (TimeoutError, OSError) as e:
                last = e
                time.sleep(1 + attempt)
        raise TwitchError(f"Нет связи с Twitch: {last}")

    def user(self, login):
        d = self.get("/users", {"login": login}).get("data") or []
        if not d:
            raise TwitchError(f"Стример «{login}» не найден.")
        return d[0]

    def categories(self, query, cancel=None):
        d = self.get("/search/categories", {"query": query, "first": 25}, cancel).get("data") or []
        return [{"id": c["id"], "name": c["name"],
                 "art": str(c.get("box_art_url", "")).replace("{width}", "52").replace("{height}", "72")} for c in d]

    def games(self, ids, cancel=None):
        ids = sorted({i for i in ids if i})
        out = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            for g in self.get("/games", {"id": chunk}, cancel).get("data") or []:
                out[g["id"]] = g["name"]
        return out

def check_link():
    req = urllib.request.Request(HEALTH_URL, headers={"X-ClipSearch": VERSION, "User-Agent": "ClipSearch/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        if d.get("ok") and d.get("keys", True):
            return {"ok": True}
        return {"ok": False, "error": "Сервер ClipSearch пока не настроен."}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Сервер ClipSearch ответил {e.code}."}
    except Exception as e:
        return {"ok": False, "error": f"Нет связи: {getattr(e, 'reason', e)}"}

def split_words(text):
    include, exclude = [], []
    for w in text.lower().split():
        if w.startswith("-") and len(w) > 1:
            exclude.append(w[1:])
        else:
            include.append(w)
    return include, exclude

def make_windows(start, end, deep):
    if not deep:
        return [(start, end)]
    span_days = max(1, math.ceil((end - start).total_seconds() / 86400))
    step = max(7, math.ceil(span_days / 600))
    windows = []
    cur = end
    while cur > start:
        lo = max(start, cur - timedelta(days=step))
        windows.append((lo, cur))
        cur = lo
    return windows

def search_clips(tw, login="", game_id="", words="", start=None, end=None, creator="", min_views=0,
                 max_pages=10, deep=False, progress=None, cancel=None, game_names=None):
    logins = [login] if isinstance(login, str) else list(login or [])
    logins = [l for l in logins if l]
    gids = [game_id] if isinstance(game_id, str) else list(game_id or [])
    gids = [g for g in dict.fromkeys(str(g).strip() for g in gids) if g]
    creators = [creator] if isinstance(creator, str) else list(creator or [])
    creators = [c.strip().lower().lstrip("@") for c in creators if str(c).strip()]
    if not logins and not gids:
        raise ValueError("Укажите стримера или категорию (можно и то и другое).")
    include, exclude = split_words(words)
    gset = set(gids)

    targets = []
    for l in logins:
        u = tw.user(l)
        try:
            created = parse_time(u["created_at"][:19] + "Z")
        except Exception:
            created = None
        targets.append(({"first": 100, "broadcaster_id": u["id"]}, created, u.get("display_name") or l))
    if not targets:
        for g in gids:
            targets.append(({"first": 100, "game_id": g}, None, (game_names or {}).get(g, "")))

    now = datetime.now(timezone.utc)
    if end is not None and end > now:
        end = now
    if start is not None and end is not None and start >= end:
        raise ValueError("Дата «с» должна быть раньше даты «по».")
    dated = start is not None or end is not None or deep

    found, seen = [], set()
    scanned = 0
    truncated = False
    for ti, (base, created, who) in enumerate(targets):
        if dated:
            t_end = end or now
            t_start = start or (max(created, TWITCH_CLIPS_START) if created else TWITCH_CLIPS_START)
            windows = make_windows(t_start, t_end, deep) if t_start < t_end else []
        else:
            windows = [(None, None)]
        for wi, (lo, hi) in enumerate(windows):
            cursor = None
            for page in range(max_pages):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                params = dict(base)
                if dated:
                    params["started_at"] = rfc3339(lo)
                    params["ended_at"] = rfc3339(hi)
                if cursor:
                    params["after"] = cursor
                r = tw.get("/clips", params, cancel)
                data = r.get("data") or []
                scanned += len(data)
                for c in data:
                    if c["id"] in seen:
                        continue
                    seen.add(c["id"])
                    title = str(c.get("title", "")).lower()
                    if include and not all(w in title for w in include):
                        continue
                    if exclude and any(w in title for w in exclude):
                        continue
                    if creators:
                        who_made = str(c.get("creator_name", "")).lower()
                        if not any(cr in who_made for cr in creators):
                            continue
                    if c.get("view_count", 0) < min_views:
                        continue
                    if logins and gset and c.get("game_id") not in gset:
                        continue
                    found.append(c)
                cursor = (r.get("pagination") or {}).get("cursor")
                if progress:
                    inner = (wi + (1.0 if not cursor else min(1.0, (page + 1) / max_pages))) / len(windows)
                    progress({"scanned": scanned, "found": len(found), "step": wi + 1, "steps": len(windows),
                              "who": who, "ti": ti + 1, "tn": len(targets),
                              "frac": (ti + inner) / len(targets)})
                if len(found) >= MAX_RESULTS or not cursor:
                    break
            else:
                if cursor and not deep:
                    truncated = True
            if len(found) >= MAX_RESULTS:
                truncated = True
                break
        if len(found) >= MAX_RESULTS:
            break

    found.sort(key=lambda c: c.get("view_count", 0), reverse=True)
    games = tw.games([c.get("game_id") for c in found], cancel)
    rows = []
    for c in found:
        try:
            created_at = parse_time(c["created_at"])
        except Exception:
            created_at = now
        rows.append({
            "id": c["id"],
            "url": c.get("url") or f"https://clips.twitch.tv/{c['id']}",
            "title": str(c.get("title", "")).strip() or "без названия",
            "creator": c.get("creator_name", ""),
            "broadcaster": c.get("broadcaster_name", ""),
            "game": games.get(c.get("game_id"), ""),
            "created": created_at,
            "views": int(c.get("view_count", 0)),
            "duration": float(c.get("duration", 0)),
            "thumb": c.get("thumbnail_url", ""),
        })
    return rows, {"scanned": scanned, "truncated": truncated}

QUALITIES = ("1080", "720", "480")

def get_download_url(slug, preview=False, quality=None):
    body = json.dumps([{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_HASH}},
    }]).encode()
    req = urllib.request.Request(GQL_URL, data=body, headers={"Client-Id": GQL_CLIENT_ID, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            clip = json.load(r)[0]["data"]["clip"]
        qs = sorted(clip["videoQualities"], key=lambda q: (-float(q["quality"]), -(q.get("frameRate") or 0)))
        best = qs[0]
        if quality and not preview:
            fit = [q for q in qs if float(q["quality"]) <= float(quality) + 1]
            best = fit[0] if fit else qs[-1]
        if preview:
            light = [q for q in qs if 300 <= float(q["quality"]) <= 540]
            best = light[0] if light else qs[-1]
        tok = clip["playbackAccessToken"]
        url = f'{best["sourceURL"]}?sig={tok["signature"]}&token={urllib.parse.quote(tok["value"], safe="")}'
        return url, best["quality"]
    except Exception:
        raise TwitchError(f"Не удалось получить ссылку на скачивание клипа {slug}.")

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:80] or "clip"

def find_ffmpeg():
    p = os.environ.get("CLIPS_FFMPEG")
    if p and os.path.isfile(p):
        return p
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        cand = os.path.join(here, name)
        if os.path.isfile(cand):
            return cand
    import shutil
    return shutil.which("ffmpeg")

def mmss(sec):
    sec = int(sec)
    return f"{sec // 60}m{sec % 60:02d}s"

def trim_video(src, dst, start, end, cancel=None, progress=None):
    ff = find_ffmpeg()
    if not ff:
        raise TwitchError("Для обрезки нужен ffmpeg, а он не найден рядом с программой. Пересоберите программу новым build-exe.yml.")
    import subprocess
    dur = max(0.1, end - start)
    cmd = [ff, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", dst]
    flags = 0x08000000 if os.name == "nt" else 0
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    err = []
    threading.Thread(target=lambda: err.extend(pr.stderr.read().decode("utf-8", "replace").splitlines()[-6:]), daemon=True).start()
    for line in pr.stdout:
        if cancel is not None and cancel.is_set():
            pr.kill()
            raise Cancelled()
        m = re.match(rb"out_time_(?:us|ms)=(\d+)", line)
        if m and progress:
            progress(min(99, int(int(m.group(1)) / 1e6 / dur * 100)))
    pr.wait()
    if pr.returncode != 0 or not os.path.isfile(dst):
        log("ffmpeg: " + " | ".join(err))
        raise TwitchError("Не удалось обрезать клип (ffmpeg). Подробности в log.txt.")

def download_clip(row, folder, progress=None, cancel=None, start=None, end=None, quality=None):
    url, got = get_download_url(row["id"], quality=quality)
    cut = start is not None and end is not None and end > start
    tail = f" ({mmss(start)}-{mmss(end)})" if cut else ""
    if quality and quality != "1080":
        tail += f" [{int(float(got))}p]"
    name = f'{safe_name(row["broadcaster"] or "clip")} - {safe_name(row["title"])} - {row["id"]}{tail}.mp4'
    path = os.path.join(folder, name)
    tmp = path + ".part"
    if cut:
        full = tmp + ".src.mp4"
        try:
            download_clip_to(url, full, (lambda p: progress(p * 6 // 10)) if progress else None, cancel)
            trim_video(full, tmp + ".mp4", start, end, cancel, (lambda p: progress(60 + p * 4 // 10)) if progress else None)
            os.replace(tmp + ".mp4", path)
        finally:
            for t in (full, tmp + ".mp4"):
                try:
                    os.remove(t)
                except OSError:
                    pass
        return path
    download_clip_to(url, path, progress, cancel)
    return path

def download_clip_to(url, path, progress=None, cancel=None):
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done * 100 // total)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def ago(dt):
    mins = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} мин назад"
    h = round(mins / 60)
    if h < 48:
        return f"{h} ч назад"
    d = round(h / 24)
    if d < 60:
        return f"{d} дн назад"
    return f"{round(d / 30)} мес назад"

def local_text(dt):
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")

def export_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Название", "Автор клипа", "Стример", "Игра", "Дата", "Просмотры", "Секунд", "Ссылка"])
        for r in rows:
            w.writerow([r["title"], r["creator"], r["broadcaster"], r["game"], local_text(r["created"]),
                        r["views"], round(r["duration"]), r["url"]])

PRESETS = ["1 час", "24 часа", "7 дней", "30 дней", "Всё время"]
COLUMNS = [
    ("n", "№", 45, "e"),
    ("title", "Название", 360, "w"),
    ("creator", "Автор клипа", 130, "w"),
    ("game", "Игра", 150, "w"),
    ("date", "Дата", 190, "w"),
    ("views", "Просмотры", 90, "e"),
    ("dur", "Секунд", 70, "e"),
]

import base64
import secrets
import socketserver
import subprocess
import http.server

UI_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Поиск клипов</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root {
  --sky-0: #070b1f;
  --sky-1: #0c1538;
  --sky-2: #16245a;
  --panel: rgba(17, 26, 62, 0.58);
  --panel-solid: #121b44;
  --panel-hi: rgba(30, 44, 98, 0.7);
  --line: rgba(150, 172, 255, 0.15);
  --line-strong: rgba(150, 172, 255, 0.28);
  --text: #e9edff;
  --muted: #95a0d0;
  --faint: #626ea3;
  --accent: #5b7cff;
  --accent-2: #7a63ff;
  --glow: 0 0 26px rgba(80, 110, 255, 0.5);
  --ok: #3ddc97;
  --warn: #ffb454;
  --bad: #ff6b7e;
  --radius-lg: 24px;
  --radius-md: 16px;
  --radius-sm: 11px;
  --font: "Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; margin: 0; }
body {
  font-family: var(--font);
  color: var(--text);
  font-size: 14px;
  line-height: 1.45;
  background:
    radial-gradient(900px 420px at 12% 105%, rgba(255, 150, 90, 0.26), transparent 62%),
    radial-gradient(1000px 520px at 85% -5%, rgba(90, 120, 255, 0.3), transparent 62%),
    linear-gradient(180deg, var(--sky-0) 0%, var(--sky-1) 55%, #121e4e 100%);
  overflow: hidden;
}
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid #9db0ff; outline-offset: 2px; }
svg.i { width: 1em; height: 1em; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }

#stars { position: fixed; inset: 0; pointer-events: none; }
#hills { position: fixed; left: 0; right: 0; bottom: 0; width: 100%; height: 24vh; pointer-events: none; }

.shell {
  position: relative;
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  height: 100vh;
  background: rgba(10, 16, 46, 0.55);
  box-shadow: inset 0 0 0 1px rgba(150, 172, 255, 0.22);
  overflow: hidden;
}
.side {
  display: flex; flex-direction: column;
  padding: 26px 18px 20px;
  background: rgba(6, 10, 30, 0.5);
  border-right: 1px solid var(--line);
}
.brand { display: flex; align-items: center; gap: 11px; padding: 0 6px 30px; }
.brand img { width: 34px; height: 34px; }
.brand b { font-size: 19px; font-weight: 600; letter-spacing: 0.01em; }
.brand span { font-weight: 300; color: var(--muted); }
.nav { display: grid; gap: 10px; }
.nav button {
  display: flex; align-items: center; gap: 12px;
  background: none; border: 0; padding: 6px 8px 6px 6px; border-radius: 40px;
  color: var(--muted); text-align: left; font-size: 14px;
  transition: color .15s, background .15s;
}
.nav button .ic {
  width: 38px; height: 38px; border-radius: 50%;
  display: grid; place-items: center; font-size: 17px;
  background: rgba(16, 24, 60, 0.9); border: 1px solid var(--line);
  transition: background .15s, box-shadow .15s;
}
.nav button:hover { color: var(--text); }
.nav button.on { color: var(--text); }
.nav button.on .ic {
  background: radial-gradient(circle at 35% 30%, #8aa2ff, #4262f0 70%);
  border-color: transparent; box-shadow: var(--glow);
}
.nav .count { margin-left: auto; font-size: 12px; background: var(--accent); color: #fff; padding: 1px 8px; border-radius: 10px; }
.side .spacer { flex: 1; }
.side .foot { font-size: 12px; color: var(--faint); padding: 14px 8px 0; }

.main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.top {
  display: flex; align-items: center; gap: 16px;
  padding: 22px 30px 0;
}
.top { user-select: none; }
.dragbar { position: fixed; top: 0; left: 224px; right: 150px; height: 54px; -webkit-app-region: drag; z-index: 1; }
.top button, .top .pill, .wc, .wc button { -webkit-app-region: no-drag; }
.brand { -webkit-app-region: drag; user-select: none; }
.wc { position: fixed; top: 0; right: 0; z-index: 70; display: flex; -webkit-app-region: no-drag; }
.wc button { width: 48px; height: 34px; background: none; border: 0; border-radius: 0; color: var(--muted); display: grid; place-items: center; font-size: 15px; }
.wc button:hover { background: rgba(150, 172, 255, 0.16); color: #fff; }
.wc button.x:hover { background: #e5384f; color: #fff; }
body.has-wc .top { padding-right: 170px; }
.crumbs { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
.crumbs svg { font-size: 15px; }
.crumbs .sep { color: var(--faint); }
.crumbs .here { color: var(--text); }
.top .grow { flex: 1; }
.pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 30px;
  background: rgba(10, 16, 44, 0.75); border: 1px solid var(--line);
  color: var(--muted); font-size: 13px;
}
.pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
.pill.ok .dot { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
.pill.bad .dot { background: var(--bad); }
.pill button { background: none; border: 0; padding: 0; color: #a9b9ff; text-decoration: underline; text-underline-offset: 3px; }
.round-btn {
  width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
  background: rgba(10, 16, 44, 0.75); border: 1px solid var(--line); color: var(--muted); font-size: 17px;
}
.round-btn:hover { color: var(--text); border-color: var(--line-strong); }

.scroller { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 8px 30px 30px; scrollbar-width: thin; scrollbar-color: #2b3a7a transparent; }
.scroller::-webkit-scrollbar { width: 10px; }
.scroller::-webkit-scrollbar-thumb { background: #2b3a7a; border-radius: 10px; border: 2px solid transparent; background-clip: content-box; }

h1 { margin: 22px 0 6px; font-size: 38px; font-weight: 300; letter-spacing: -0.01em; line-height: 1.15; }
h1 b { font-weight: 500; background: linear-gradient(90deg, #7f9bff, #a38bff); -webkit-background-clip: text; background-clip: text; color: transparent; }
.lede { margin: 0 0 22px; color: var(--muted); max-width: 62ch; }

.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 20px;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}
.card h3 { margin: 0 0 14px; font-size: 16px; font-weight: 500; display: flex; align-items: center; gap: 10px; }
.card h3 .grow { flex: 1; }

.filters { display: grid; gap: 16px; position: relative; z-index: 20; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: stretch; }
.field {
  position: relative; flex: 1 1 200px; min-width: 0;
  display: flex; align-items: center; gap: 10px;
  padding: 0 14px; height: 46px;
  background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); border-radius: 30px;
  color: var(--muted); transition: border-color .15s, box-shadow .15s;
}
.field:focus-within { border-color: #7d93ff; box-shadow: 0 0 0 3px rgba(91, 124, 255, 0.18); color: #c9d3ff; }
.field input { flex: 1; min-width: 0; background: none; border: 0; outline: 0; color: var(--text); height: 100%; }
.field input::placeholder { color: var(--faint); }
.field.wide { flex: 2 1 320px; }
.field.small { flex: 0 1 190px; }
.field .x { background: none; border: 0; color: var(--faint); display: none; padding: 2px; font-size: 14px; }
.field.has .x { display: grid; }

.sug {
  position: absolute; left: 0; right: 0; top: calc(100% + 8px); z-index: 30;
  background: #101a45; border: 1px solid var(--line-strong); border-radius: var(--radius-md);
  padding: 6px; max-height: 300px; overflow: auto; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
}
.sug button {
  display: flex; align-items: center; gap: 12px; width: 100%; text-align: left;
  background: none; border: 0; padding: 7px 10px; border-radius: 10px; color: var(--text);
}
.sug button:hover, .sug button.act { background: rgba(91, 124, 255, 0.2); }
.sug img { width: 28px; height: 38px; border-radius: 5px; object-fit: cover; background: #1b2757; }
.sug .none { color: var(--muted); padding: 10px; }

.seg { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.chip {
  height: 38px; padding: 0 16px; border-radius: 30px;
  background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); color: var(--muted);
  display: inline-flex; align-items: center; gap: 8px; transition: all .15s;
}
.chip:hover { color: var(--text); border-color: var(--line-strong); }
.chip.on { color: #fff; background: linear-gradient(180deg, #5a7bff, #3f5ee8); border-color: transparent; box-shadow: 0 0 20px rgba(80, 110, 255, 0.4); }
.seg .lbl { color: var(--muted); margin-right: 4px; }

.switch { display: inline-flex; align-items: center; gap: 10px; color: var(--muted); background: none; border: 0; padding: 0; height: 38px; }
.switch .tr { width: 40px; height: 22px; border-radius: 20px; background: rgba(8, 13, 38, 0.9); border: 1px solid var(--line-strong); position: relative; transition: background .15s; }
.switch .tr::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #8d99cf; transition: all .15s; }
.switch[aria-checked="true"] .tr { background: #4262f0; border-color: transparent; }
.switch[aria-checked="true"] .tr::after { left: 20px; background: #fff; }
.switch[aria-checked="true"] { color: var(--text); }

.go { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.btn {
  height: 46px; padding: 0 22px; border-radius: 30px; border: 1px solid var(--line-strong);
  background: rgba(10, 16, 44, 0.8); color: var(--text);
  display: inline-flex; align-items: center; justify-content: center; gap: 9px; transition: all .15s;
}
.btn:hover { border-color: #8aa0ff; }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn.primary {
  background: linear-gradient(180deg, #5f80ff, #3b59e6); border-color: transparent;
  box-shadow: 0 0 28px rgba(80, 110, 255, 0.45); padding: 0 30px; font-weight: 500;
}
.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }
.btn.slim { height: 38px; padding: 0 16px; font-size: 13px; }
.btn.danger { color: #ffb3bd; }
.hint { color: var(--faint); font-size: 12.5px; }

.pop {
  position: absolute; z-index: 40; width: 640px; max-width: calc(100vw - 80px);
  background: #0f1843; border: 1px solid var(--line-strong); border-radius: 22px;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6); padding: 16px; display: grid;
  grid-template-columns: 150px 1fr; gap: 16px;
}
.pop .presets { display: grid; gap: 4px; align-content: start; }
.pop .presets button {
  text-align: left; background: none; border: 0; padding: 8px 12px; border-radius: 10px; color: var(--muted);
}
.pop .presets button:hover { background: rgba(91, 124, 255, 0.16); color: var(--text); }
.pop .cal { min-width: 0; }
.pop .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.pop .head button { width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--line); background: rgba(8, 13, 38, 0.7); display: grid; place-items: center; }
.pop .head button:hover { border-color: #8aa0ff; }
.months { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.month .name { text-align: center; font-weight: 500; margin-bottom: 8px; text-transform: capitalize; }
.dow, .days { display: grid; grid-template-columns: repeat(7, 1fr); }
.dow span { text-align: center; color: var(--faint); font-size: 12px; padding: 4px 0; }
.days button {
  height: 34px; background: none; border: 0; color: var(--text); font-variant-numeric: tabular-nums; position: relative; border-radius: 0;
}
.days button:hover:not(:disabled):not(.sel) { background: rgba(91, 124, 255, 0.22); border-radius: 10px; }
.days button:disabled { color: #3c4676; cursor: default; }
.days button.in { background: rgba(91, 124, 255, 0.2); }
.days button.sel { background: linear-gradient(180deg, #5f80ff, #3d5be6); color: #fff; border-radius: 10px; box-shadow: 0 0 14px rgba(80, 110, 255, 0.5); z-index: 1; }
.days button.today::after { content: ""; position: absolute; left: 50%; bottom: 4px; width: 4px; height: 4px; margin-left: -2px; border-radius: 50%; background: var(--warn); }
.days i { display: block; }
.pop .inputs { display: flex; gap: 10px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
.pop .inputs label { color: var(--muted); font-size: 13px; }
.pop .inputs input {
  width: 118px; height: 38px; border-radius: 30px; text-align: center; background: rgba(8, 13, 38, 0.8);
  border: 1px solid var(--line-strong); outline: 0; font-variant-numeric: tabular-nums;
}
.pop .inputs input:focus { border-color: #8aa0ff; }
.pop .inputs input.bad { border-color: var(--bad); }
.pop .last { display: flex; gap: 8px; align-items: center; margin-top: 12px; color: var(--muted); flex-wrap: wrap; }
.pop .last input { width: 64px; height: 36px; border-radius: 30px; text-align: center; background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line-strong); outline: 0; }
.pop .last select { height: 36px; border-radius: 30px; background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line-strong); padding: 0 10px; outline: 0; }
.pop .last select option { background: #0f1843; }
.pop .foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }

.grid { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 22px; margin-top: 22px; align-items: start; }
.res-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.res-head h2 { margin: 0; font-size: 20px; font-weight: 400; }
.res-head .status { color: var(--muted); font-size: 13px; flex: 1 1 200px; min-width: 0; }
.sorts { display: flex; gap: 6px; flex-wrap: wrap; }
.sorts .chip { height: 32px; padding: 0 13px; font-size: 13px; }
.list { display: grid; gap: 12px; }
.clip {
  display: grid; grid-template-columns: auto 184px minmax(0, 1fr) auto; gap: 16px; align-items: center;
  padding: 12px 14px 12px 14px; border-radius: 20px; cursor: pointer;
  background: rgba(14, 22, 56, 0.62); border: 1px solid var(--line);
  transition: border-color .15s, background .15s;
}
.clip:hover { border-color: var(--line-strong); }
.clip.sel { background: linear-gradient(100deg, rgba(37, 60, 170, 0.85), rgba(22, 36, 106, 0.8)); border-color: rgba(130, 156, 255, 0.55); box-shadow: inset 0 -30px 50px -30px rgba(90, 120, 255, 0.5); }
.tick {
  width: 22px; height: 22px; border-radius: 50%; border: 1.5px solid var(--faint); display: grid; place-items: center;
  color: transparent; font-size: 13px; transition: all .15s; background: none; padding: 0;
}
.clip.sel .tick { background: var(--accent); border-color: transparent; color: #fff; }
.thumb { position: relative; width: 184px; aspect-ratio: 16 / 9; border-radius: 13px; overflow: hidden; background: #172254; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb .dur { position: absolute; right: 7px; bottom: 7px; background: rgba(5, 8, 24, 0.82); padding: 1px 7px; border-radius: 8px; font-size: 12px; font-variant-numeric: tabular-nums; }
.clip h4 { margin: 0 0 8px; font-size: 15px; font-weight: 500; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--muted); font-size: 12.5px; }
.meta span { display: inline-flex; align-items: center; gap: 6px; }
.meta .rel { color: var(--faint); }
.acts { display: flex; gap: 8px; }
.acts button {
  width: 38px; height: 38px; border-radius: 50%; display: grid; place-items: center; font-size: 16px;
  background: rgba(8, 13, 38, 0.8); border: 1px solid var(--line); color: var(--muted);
}
.acts button:hover { color: #fff; border-color: #8aa0ff; background: rgba(60, 90, 230, 0.4); }
.more { margin: 16px auto 0; display: flex; }
.empty { text-align: center; padding: 56px 20px; color: var(--muted); }
.empty .big { width: 78px; height: 78px; margin: 0 auto 16px; border-radius: 50%; display: grid; place-items: center; font-size: 32px; background: rgba(10, 16, 44, 0.8); border: 1px solid var(--line); color: #8aa0ff; box-shadow: var(--glow); }
.empty h3 { margin: 0 0 6px; font-weight: 500; color: var(--text); font-size: 18px; }
.empty p { margin: 0 auto; max-width: 46ch; }
.alert { margin-bottom: 14px; padding: 12px 16px; border-radius: 16px; background: rgba(255, 107, 126, 0.12); border: 1px solid rgba(255, 107, 126, 0.45); color: #ffc2cb; }

.skel { height: 96px; border-radius: 20px; background: linear-gradient(90deg, rgba(20, 30, 70, 0.6), rgba(40, 56, 120, 0.5), rgba(20, 30, 70, 0.6)); background-size: 200% 100%; animation: sh 1.4s linear infinite; }
@keyframes sh { to { background-position: -200% 0; } }

.ghostbtn { background: none; border-color: var(--line); color: var(--muted); padding: 0 20px; }
.ghostbtn:hover { color: var(--text); }
.searching { text-align: center; padding: 34px 20px 40px; color: var(--muted); border-radius: 24px; border: 1px solid var(--line); background: rgba(10, 16, 44, 0.55); }
.lens-scene { position: relative; width: 300px; height: 130px; margin: 0 auto 14px; }
.lens-scene .tile { position: absolute; top: 40px; width: 74px; height: 46px; border-radius: 10px; background: linear-gradient(135deg, #18255a, #0e1740); border: 1px solid var(--line-strong); display: grid; place-items: center; animation: tilepulse 3.2s ease-in-out infinite; }
.lens-scene .tile:nth-child(1) { left: 6px; }
.lens-scene .tile:nth-child(2) { left: 113px; animation-delay: .5s; }
.lens-scene .tile:nth-child(3) { left: 220px; animation-delay: 1s; }
.lens-scene .tile i { width: 0; height: 0; border-left: 11px solid #5f7bff; border-top: 7px solid transparent; border-bottom: 7px solid transparent; margin-left: 3px; opacity: .8; }
.lens { position: absolute; left: 0; top: 0; width: 76px; height: 76px; animation: lensmove 3.2s ease-in-out infinite; filter: drop-shadow(0 0 14px rgba(110, 140, 255, 0.7)); }
@keyframes lensmove {
  0% { transform: translate(-6px, 6px) rotate(-8deg); }
  25% { transform: translate(100px, 18px) rotate(6deg); }
  50% { transform: translate(206px, 6px) rotate(-6deg); }
  75% { transform: translate(100px, 26px) rotate(8deg); }
  100% { transform: translate(-6px, 6px) rotate(-8deg); }
}
@keyframes tilepulse { 0%, 100% { box-shadow: 0 0 0 rgba(95, 123, 255, 0); } 40% { box-shadow: 0 0 22px rgba(95, 123, 255, 0.55); border-color: #8aa0ff; } }
.searching h3 { margin: 0 0 8px; font-weight: 500; font-size: 20px; color: var(--text); }
.searching h3 .dots span { animation: dotb 1.2s infinite; opacity: .2; }
.searching h3 .dots span:nth-child(2) { animation-delay: .2s; }
.searching h3 .dots span:nth-child(3) { animation-delay: .4s; }
@keyframes dotb { 0%, 60%, 100% { opacity: .2; } 30% { opacity: 1; } }
.searching .numbers { font-size: 14px; color: var(--text); margin-top: 4px; }
.searching .eta { font-size: 13px; color: var(--muted); margin-top: 6px; }
.searching .hatch { max-width: 380px; margin: 16px auto 0; }
@media (prefers-reduced-motion: reduce) { .lens, .lens-scene .tile { animation: none; } }

#fLogin { height: auto; min-height: 46px; flex-wrap: wrap; padding-top: 5px; padding-bottom: 5px; row-gap: 6px; }
#fLogin input { height: 34px; flex: 1 1 140px; }
#fCat, #fCreator { height: auto; min-height: 46px; flex-wrap: wrap; padding-top: 5px; padding-bottom: 5px; row-gap: 6px; }
#fCat input { height: 34px; flex: 1 1 150px; }
#fCreator { flex: 1 1 230px; }
#fCreator input { height: 34px; flex: 1 1 90px; }
.chip-l img { width: 18px; height: 24px; border-radius: 4px; object-fit: cover; margin-left: -6px; background: #1b2757; }
.chip-l { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 6px 0 12px; border-radius: 20px; background: rgba(70, 98, 235, 0.32); border: 1px solid rgba(130, 156, 255, 0.5); color: var(--text); font-size: 13px; white-space: nowrap; }
.chip-l button { width: 20px; height: 20px; border-radius: 50%; border: 0; background: rgba(255, 255, 255, 0.08); color: var(--muted); display: grid; place-items: center; padding: 0; }
.chip-l button:hover { background: #e5384f; color: #fff; }
.chip-l button .i { width: 12px; height: 12px; }

.thumb .playov { position: absolute; inset: 0; display: grid; place-items: center; background: rgba(4, 8, 26, 0.45); opacity: 0; transition: opacity .15s; cursor: pointer; border: 0; padding: 0; width: 100%; color: #fff; }
.thumb .playov span { width: 44px; height: 44px; border-radius: 50%; display: grid; place-items: center; background: rgba(91, 124, 255, 0.9); box-shadow: 0 0 22px rgba(91, 124, 255, 0.7); }
.thumb:hover .playov, .thumb .playov:focus-visible { opacity: 1; }
.player { width: min(1040px, 94vw); max-height: 94vh; overflow: auto; padding: 20px 22px 18px; }
.player .ph { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 12px; }
.player .ph .grow { flex: 1; min-width: 0; }
.player h2 { font-size: 19px; font-weight: 500; margin: 0 0 6px; overflow-wrap: anywhere; }
.player .xbtn { width: 38px; height: 38px; border-radius: 50%; border: 1px solid var(--line); background: none; color: var(--muted); display: grid; place-items: center; flex: none; }
.player .xbtn:hover { color: #fff; border-color: #8aa0ff; }
.vbox { position: relative; border-radius: 16px; overflow: hidden; background: #000; aspect-ratio: 16 / 9; max-height: 58vh; margin: 0 auto; }
.vbox video { width: 100%; height: 100%; display: block; background: #000; }
.vbox .vmsg { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); text-align: center; padding: 20px; }
.trim { margin-top: 14px; }
.trim .tl { position: relative; height: 34px; border-radius: 10px; background: rgba(8, 13, 38, 0.9); border: 1px solid var(--line); cursor: pointer; touch-action: none; user-select: none; }
.trim .rng { position: absolute; top: 0; bottom: 0; background: rgba(91, 124, 255, 0.32); border-top: 2px solid #7d93ff; border-bottom: 2px solid #7d93ff; }
.trim .hd { position: absolute; top: -4px; bottom: -4px; width: 14px; margin-left: -7px; border-radius: 6px; background: #7d93ff; box-shadow: 0 0 12px rgba(125, 147, 255, 0.8); cursor: ew-resize; border: 0; padding: 0; }
.trim .hd::after { content: ""; position: absolute; left: 5px; top: 30%; bottom: 30%; width: 4px; border-left: 1px solid rgba(255,255,255,.7); border-right: 1px solid rgba(255,255,255,.7); }
.trim .ph2 { position: absolute; top: -6px; bottom: -6px; width: 2px; margin-left: -1px; background: #fff; border-radius: 2px; pointer-events: none; box-shadow: 0 0 6px rgba(255,255,255,.8); }
.trim .tinfo { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; margin-top: 10px; color: var(--muted); font-size: 13px; }
.trim .tinfo b { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
.trim .tbtns { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.player .foot { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; justify-content: flex-end; align-items: center; }
.player .foot .hint { flex: 1 1 220px; }
kbd { font: inherit; font-size: 11px; padding: 1px 6px; border-radius: 6px; border: 1px solid var(--line-strong); color: var(--muted); }

.rail { display: grid; grid-template-columns: minmax(0, 1fr); gap: 18px; position: sticky; top: 0; }
.rail > *, .grid > * { min-width: 0; }
.sel-num { font-size: 40px; font-weight: 300; line-height: 1; }
.sel-sub { color: var(--muted); margin: 4px 0 16px; }
.stack { display: grid; gap: 10px; }
.stack .btn { width: 100%; justify-content: flex-start; height: 42px; }
.hatch {
  height: 12px; border-radius: 8px; background: rgba(8, 13, 38, 0.9); border: 1px solid var(--line); overflow: hidden;
}
.hatch i {
  display: block; height: 100%; width: 0; transition: width .3s;
  background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0 4px, rgba(255, 255, 255, 0.12) 4px 8px), #4262f0;
  background-blend-mode: overlay;
}
.hatch.done i { background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0 4px, rgba(255, 255, 255, 0.12) 4px 8px), #2fbf86; }
.dl { display: grid; gap: 4px; padding: 10px 0; border-top: 1px solid var(--line); }
.dl:first-of-type { border-top: 0; }
.dl .t { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.dl .t span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl .t span:last-child { color: var(--muted); flex: none; }
.dl .err { color: #ffb3bd; font-size: 12px; }
.folder { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 14px; background: rgba(8, 13, 38, 0.7); border: 1px solid var(--line); color: var(--muted); font-size: 12.5px; margin-bottom: 12px; }
.folder span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.folder button { background: none; border: 0; color: #a9b9ff; padding: 0; text-decoration: underline; text-underline-offset: 3px; flex: none; }
.chart { width: 100%; height: 130px; display: block; }
.chart text { fill: var(--faint); font-size: 10.5px; font-family: var(--font); }
.chart .bar { fill: url(#hatch); }
.chart .bar.top { fill: #fff; }
.note { color: var(--muted); font-size: 13px; }

.veil { position: fixed; inset: 0; background: rgba(3, 6, 20, 0.66); backdrop-filter: blur(4px); z-index: 80; display: none; place-items: center; padding: 20px; }
.veil.on { display: grid; }
.modal { width: 520px; max-width: 100%; background: #101946; border: 1px solid var(--line-strong); border-radius: 26px; padding: 26px; box-shadow: 0 30px 90px rgba(0, 0, 0, 0.6); }
.modal h2 { margin: 0 0 6px; font-weight: 400; font-size: 24px; }
.modal p { color: var(--muted); margin: 0 0 18px; }
.modal .field { margin-bottom: 12px; flex: none; width: 100%; }
.modal .err { color: #ffb3bd; min-height: 20px; margin: 4px 2px 12px; font-size: 13px; }
.modal .foot { display: flex; gap: 10px; justify-content: flex-end; }

.toast { position: fixed; left: 50%; bottom: 34px; transform: translateX(-50%) translateY(20px); opacity: 0; transition: all .25s; z-index: 90; background: #1b2a73; border: 1px solid rgba(130, 156, 255, 0.5); padding: 11px 20px; border-radius: 30px; box-shadow: 0 14px 40px rgba(0, 0, 0, 0.5); pointer-events: none; }
.toast.on { opacity: 1; transform: translateX(-50%); }

.view { display: none; }
.view.on { display: block; }

@media (max-width: 1240px) { .grid { grid-template-columns: minmax(0, 1fr); } .rail { position: static; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); } }
@media (max-width: 980px) {
  .shell { grid-template-columns: 74px minmax(0, 1fr); }
  .nav button span:not(.ic), .brand b, .side .foot { display: none; }
  .nav .count { display: none; }
  .clip { grid-template-columns: auto 140px minmax(0, 1fr); }
  .thumb { width: 140px; }
  .acts { grid-column: 2 / -1; }
}

:root {
  --glass: linear-gradient(155deg, rgba(64, 84, 178, 0.24) 0%, rgba(26, 38, 96, 0.30) 42%, rgba(12, 18, 54, 0.40) 100%);
  --glass-soft: linear-gradient(150deg, rgba(52, 70, 160, 0.20), rgba(14, 22, 62, 0.34));
  --glass-edge: rgba(170, 190, 255, 0.16);
  --glass-blur: blur(22px) saturate(150%);
  --spring: cubic-bezier(.34, 1.56, .64, 1);
  --ease: cubic-bezier(.22, 1, .36, 1);
}
body { background: #050817; }
#stars { width: 100%; height: 100%; }
#hills { height: 30vh; }

.shell { background: rgba(8, 12, 36, 0.16); box-shadow: inset 0 0 0 1px rgba(150, 172, 255, 0.16); }
.side {
  background: linear-gradient(180deg, rgba(10, 16, 46, 0.46), rgba(6, 10, 30, 0.52));
  backdrop-filter: blur(26px) saturate(140%); -webkit-backdrop-filter: blur(26px) saturate(140%);
  border-right: 1px solid rgba(150, 172, 255, 0.12);
  box-shadow: inset -1px 0 0 rgba(255, 255, 255, 0.03);
}
.main { padding-top: 34px; position: relative; }

.dragbar { height: 34px; }
h1 { margin-top: 4px; }
.scroller { padding-top: 6px; }

.side-status { padding: 0 2px 14px; }
.side-status .pill { width: 100%; justify-content: flex-start; flex-wrap: wrap; font-size: 12.5px; line-height: 1.3; padding: 9px 13px;
  background: rgba(8, 13, 40, 0.45); backdrop-filter: blur(12px); cursor: default; }
.side-status .pill .dot { flex: none; }
.side-status .pill.bad { cursor: pointer; border-color: rgba(255, 107, 126, 0.4); }

.card {
  position: relative;
  background: var(--glass);
  border: 1px solid var(--glass-edge);
  backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.07), 0 20px 50px rgba(2, 5, 22, 0.32);
}
.card::before, .clip::before, .modal::before {
  content: ""; position: absolute; inset: 0; border-radius: inherit; padding: 1px; pointer-events: none;
  background: linear-gradient(150deg, rgba(200, 214, 255, 0.34), rgba(150, 172, 255, 0.06) 38%, rgba(150, 172, 255, 0) 60%, rgba(122, 99, 255, 0.18));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude;
}
.field { background: rgba(6, 10, 32, 0.42); border-color: rgba(150, 172, 255, 0.14); box-shadow: inset 0 1px 6px rgba(0, 0, 0, 0.22); }
.chip, .acts button, .round-btn, .xbtn, .btn:not(.primary) { background: rgba(10, 16, 46, 0.42); }
.chip { border-color: rgba(150, 172, 255, 0.16); }

.clip {
  position: relative; background: var(--glass-soft); border-color: rgba(150, 172, 255, 0.13);
  backdrop-filter: blur(16px) saturate(140%); -webkit-backdrop-filter: blur(16px) saturate(140%);
  transition: transform .35s var(--spring), border-color .2s, background .3s, box-shadow .3s;
}
.clip:hover { transform: translateY(-2px); border-color: rgba(150, 172, 255, 0.3); box-shadow: 0 14px 34px rgba(2, 5, 22, 0.35), 0 0 0 1px rgba(110, 140, 255, 0.12); }
.clip.sel {
  background: linear-gradient(105deg, rgba(48, 74, 200, 0.62), rgba(26, 42, 122, 0.55));
  border-color: rgba(140, 166, 255, 0.5);
  box-shadow: inset 0 -40px 60px -34px rgba(110, 140, 255, 0.75), 0 0 34px rgba(80, 110, 255, 0.18);
}
.clip .thumb { transition: transform .35s var(--spring); }
.clip:hover .thumb { transform: scale(1.02); }

.veil { background: rgba(3, 6, 22, 0.5); backdrop-filter: blur(12px) saturate(120%); -webkit-backdrop-filter: blur(12px) saturate(120%); }
.modal { position: relative; background: linear-gradient(160deg, rgba(40, 56, 140, 0.55), rgba(14, 22, 66, 0.72)); border-color: var(--glass-edge); }
.pop, .sug { background: rgba(14, 22, 64, 0.82); backdrop-filter: blur(24px) saturate(150%); -webkit-backdrop-filter: blur(24px) saturate(150%); }
.toast { background: rgba(30, 44, 120, 0.7); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); }
.searching { background: var(--glass-soft); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); border-color: var(--glass-edge); }

#selCard { overflow: hidden; }
#selCard::after {
  content: ""; position: absolute; left: -10%; right: -10%; top: -60%; height: 120%; pointer-events: none; opacity: 0;
  background: radial-gradient(closest-side, rgba(120, 146, 255, 0.55), rgba(90, 110, 255, 0.18) 55%, transparent);
  transition: opacity .5s var(--ease), transform .6s var(--ease); transform: translateY(-20px);
}
#selCard.has::after { opacity: 1; transform: none; }
#selCard > * { position: relative; z-index: 1; }

.btn, .chip, .acts button, .nav button, .round-btn, .xbtn, .tick, .chip-l button, .switch, .linkbtn {
  transition: transform .28s var(--spring), background .2s, border-color .2s, box-shadow .25s, color .2s, filter .2s;
}
.btn, .chip, .acts button, .nav button, .xbtn, .round-btn { position: relative; overflow: hidden; }
.btn:hover:not(:disabled), .acts button:hover, .xbtn:hover { transform: translateY(-1px); }
.btn:active:not(:disabled), .chip:active, .acts button:active, .xbtn:active, .round-btn:active, .nav button:active { transform: scale(.95); transition-duration: .08s; }
.btn.primary::after {
  content: ""; position: absolute; top: 0; bottom: 0; left: -60%; width: 40%; pointer-events: none;
  background: linear-gradient(100deg, transparent, rgba(255, 255, 255, 0.28), transparent);
  transform: skewX(-18deg); transition: left .7s var(--ease);
}
.btn.primary:hover:not(:disabled)::after { left: 120%; }
.rip { position: absolute; border-radius: 50%; pointer-events: none; transform: scale(0); opacity: .55;
  background: radial-gradient(circle, rgba(220, 230, 255, 0.55), rgba(160, 180, 255, 0.15) 45%, transparent 70%);
  animation: rip .6s var(--ease) forwards; }
@keyframes rip { to { transform: scale(1); opacity: 0; } }

.seg, .sorts { position: relative; }
.glide {
  position: absolute; left: 0; top: 0; width: 0; height: 0; border-radius: 30px; pointer-events: none; z-index: 0; opacity: 0;
  background: linear-gradient(180deg, #5a7bff, #3f5ee8); box-shadow: 0 0 22px rgba(80, 110, 255, 0.45);
  transition: left .5s var(--spring), top .35s var(--ease), width .5s var(--spring), height .35s var(--ease), opacity .25s;
}
.seg .chip, .sorts .chip, .seg .lbl { position: relative; z-index: 1; }
.seg .chip.on, .sorts .chip.on { background: transparent; box-shadow: none; border-color: transparent; }
body.noanim .glide, body.noanim .navind { transition: none !important; }

.nav { position: relative; }
.navind {
  position: absolute; left: 6px; top: 0; width: 38px; height: 38px; border-radius: 50%; pointer-events: none; z-index: 0;
  background: radial-gradient(circle at 35% 30%, #8aa2ff, #4262f0 70%); box-shadow: var(--glow);
  transition: top .55s var(--spring), opacity .25s;
}
.nav button { z-index: 1; }
.nav button .ic { position: relative; z-index: 1; transition: background .25s, border-color .25s, transform .3s var(--spring); }
.nav button.on .ic { background: transparent; border-color: transparent; box-shadow: none; }
.nav button:hover .ic { transform: scale(1.06); }

@keyframes riseT { from { transform: translateY(16px); } }
@keyframes fadeIn { from { opacity: 0; } }
.view.on > * { animation: riseT .55s var(--ease) both; }
.view.on > h1, .view.on > .lede { animation: riseT .55s var(--ease) both, fadeIn .45s ease both; }
.view.on > .card { animation: riseT .55s var(--ease) both, fadeIn .45s ease both; }
.view.on > :nth-child(2) { animation-delay: .04s; }
.view.on > :nth-child(3) { animation-delay: .08s; }
.view.on > :nth-child(4) { animation-delay: .12s; }
.view.on > :nth-child(5) { animation-delay: .16s; }
.view.on .rail > .card, .view.on .dl-grid > .card { animation: riseT .6s var(--ease) both, fadeIn .5s ease both; }
.view.on .rail > .card:nth-child(2), .view.on .dl-grid > .card:nth-child(2) { animation-delay: .08s; }
.view.on .rail > .card:nth-child(3) { animation-delay: .16s; }

.clip.enter { animation: clipIn .5s var(--ease) both; animation-delay: var(--d, 0s); }
@keyframes clipIn { from { opacity: 0; transform: translateY(18px) scale(.985); } }
.clip.sel .tick { animation: pop .38s var(--spring); }
@keyframes pop { from { transform: scale(.4); } }
.chip-l.new { animation: chipIn .38s var(--spring) both; }
@keyframes chipIn { from { opacity: 0; transform: scale(.6); } }
.sel-num.bump { animation: bump .42s var(--spring); }
@keyframes bump { 40% { transform: scale(1.22); color: #b9c7ff; } }
.sel-num { display: inline-block; transform-origin: left 70%; }
.res-head:not([hidden]) { animation: riseT .45s var(--ease) both, fadeIn .35s ease both; }
.alert { animation: riseT .4s var(--ease) both, fadeIn .3s ease both; }
.empty .big { animation: floaty 4s ease-in-out infinite; }
@keyframes floaty { 50% { transform: translateY(-6px); } }

.pop:not([hidden]) { animation: popIn .32s var(--spring) both; transform-origin: 85% 0; }
.sug:not([hidden]) { animation: popIn .28s var(--spring) both; transform-origin: 30% 0; }
@keyframes popIn { from { opacity: 0; transform: translateY(-8px) scale(.96); } }
.veil.on { animation: fadeIn .25s ease both; }
.veil.on .modal { animation: modalIn .48s var(--spring) both; }
.veil.closing { animation: fadeOut .2s ease both; }
.veil.closing .modal { animation: modalOut .2s ease both; }
@keyframes modalIn { from { opacity: 0; transform: translateY(22px) scale(.94); } }
@keyframes modalOut { to { opacity: 0; transform: translateY(10px) scale(.97); } }
@keyframes fadeOut { to { opacity: 0; } }
.toast { transition: opacity .3s ease, transform .45s var(--spring); }
.switch .tr::after { transition: left .35s var(--spring), background .2s, width .2s; }
.switch:active .tr::after { width: 20px; }
.field { transition: border-color .2s, box-shadow .3s, background .2s; }
.field:focus-within { background: rgba(8, 14, 44, 0.6); }
.days button { transition: background .15s, transform .2s var(--spring); }
.days button:active:not(:disabled) { transform: scale(.9); }

.hatch i { transition: width .45s var(--ease), background-color .3s; }
.hatch.run i { animation: stripes .9s linear infinite; }
@keyframes stripes { to { background-position: 11.31px 0; } }
.dl.enter { animation: riseT .45s var(--ease) both, fadeIn .35s ease both; }
.dl .ok-ic { color: var(--ok); }

.folder-hero { display: grid; grid-template-columns: auto minmax(0, 1fr) minmax(0, 1.1fr); gap: 26px; align-items: center; padding: 26px 28px; overflow: hidden; }
.folder-hero::after {
  content: ""; position: absolute; left: -80px; bottom: -140px; width: 420px; height: 300px; pointer-events: none;
  background: radial-gradient(closest-side, rgba(100, 128, 255, 0.4), transparent); filter: blur(10px);
}
.folder-hero > * { position: relative; z-index: 1; }
.fh-ic {
  width: 84px; height: 84px; border-radius: 26px; display: grid; place-items: center; font-size: 38px; color: #dfe6ff;
  background: radial-gradient(circle at 30% 25%, #8aa2ff, #4262f0 72%); box-shadow: 0 0 40px rgba(80, 110, 255, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.4);
  animation: floaty 5s ease-in-out infinite;
}
.fh-label { color: var(--muted); font-size: 13px; }
.fh-path { font-size: 17px; font-weight: 500; margin: 4px 0 14px; overflow-wrap: anywhere; line-height: 1.35; }
.fh-btns { display: flex; gap: 10px; flex-wrap: wrap; }
.fh-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.stat { padding: 14px 16px; border-radius: 18px; background: rgba(6, 10, 32, 0.38); border: 1px solid rgba(150, 172, 255, 0.12); min-width: 0; }
.stat b { display: block; font-size: 24px; font-weight: 400; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat span { color: var(--muted); font-size: 12.5px; }
.disk { grid-column: 1 / -1; display: grid; gap: 7px; }
.disk span { color: var(--faint); font-size: 12px; }
.dl-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 22px; margin-top: 22px; align-items: start; }
.dl-grid > * { min-width: 0; }
.qempty { text-align: center; padding: 30px 10px 18px; color: var(--muted); }
.qempty .big { width: 64px; height: 64px; margin: 0 auto 14px; border-radius: 20px; display: grid; place-items: center; font-size: 28px; color: #9fb2ff;
  background: rgba(10, 16, 46, 0.5); border: 1px solid var(--glass-edge); animation: floaty 4s ease-in-out infinite; }
.qempty p { margin: 0 auto 16px; max-width: 40ch; }
.rf { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 10px 6px; border-top: 1px solid rgba(150, 172, 255, 0.1); }
.rf:first-child { border-top: 0; }
.rf .fi { width: 38px; height: 38px; border-radius: 12px; display: grid; place-items: center; color: #a9b9ff; background: rgba(91, 124, 255, 0.14); border: 1px solid rgba(130, 156, 255, 0.25); }
.rf .nm { font-size: 13.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rf .sb { color: var(--faint); font-size: 12px; font-variant-numeric: tabular-nums; }
.rf .acts button { width: 34px; height: 34px; font-size: 15px; }
.rf.enter { animation: riseT .45s var(--ease) both, fadeIn .35s ease both; animation-delay: var(--d, 0s); }
.linkbtn { background: none; border: 0; color: #a9b9ff; padding: 0; font-size: 13px; }
.linkbtn:hover { color: #fff; }
@media (max-width: 1240px) { .folder-hero { grid-template-columns: auto minmax(0, 1fr); } .fh-stats { grid-column: 1 / -1; } .dl-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 980px) { .side-status .pill span:not(.dot) { display: none; } .side-status .pill { width: auto; } }

html.glass-os, body.glass-os { background: transparent; }
body.glass-os { --muted: #aab4e2; --faint: #8590c4; }
body.glass-os .clip h4, body.glass-os h1, body.glass-os .card h3 { text-shadow: 0 1px 12px rgba(4, 8, 30, 0.45); }
body.glass-os #stars, body.glass-os #hills { display: none; }
body.glass-os .shell { background: linear-gradient(160deg, rgba(18, 28, 84, 0.22), rgba(8, 12, 40, 0.30)); box-shadow: inset 0 0 0 1px rgba(170, 190, 255, 0.14); }
body.glass-os .side { background: linear-gradient(180deg, rgba(8, 12, 36, 0.30), rgba(5, 8, 26, 0.38)); backdrop-filter: none; -webkit-backdrop-filter: none; }
body.glass-os .card { background: linear-gradient(155deg, rgba(80, 102, 210, 0.20), rgba(26, 38, 100, 0.22) 45%, rgba(10, 16, 50, 0.28)); }
body.glass-os .clip { background: linear-gradient(150deg, rgba(60, 82, 180, 0.16), rgba(12, 18, 56, 0.26)); }
body.glass-os .clip.sel { background: linear-gradient(105deg, rgba(56, 84, 214, 0.6), rgba(28, 44, 130, 0.52)); }

.res-head h2 { flex: none; white-space: nowrap; }
.res-head { row-gap: 8px; }
.res-head .status { flex: 1 1 100%; order: 2; margin-top: -4px; line-height: 1.45; }
.res-head .sorts { order: 3; margin-top: 4px; }

.sorts.locked .chip { cursor: not-allowed; opacity: .55; }
.sorts.locked .glide { opacity: .5 !important; }
.chip.shake { animation: shake .42s cubic-bezier(.36, .07, .19, .97) both; }
@keyframes shake {
  10%, 90% { transform: translateX(-1px); }
  20%, 80% { transform: translateX(3px); }
  30%, 50%, 70% { transform: translateX(-5px); }
  40%, 60% { transform: translateX(5px); }
}

.rail .card { overflow: visible; }
.chart .bar, .chart .ghost { transform-box: fill-box; transform-origin: 50% 100%; }
.chart .ghost { fill: url(#hatch); opacity: .32; animation: breathe 2.8s ease-in-out infinite; animation-delay: calc(var(--i) * -0.41s); }
.chart.busy .ghost { opacity: .55; animation-duration: 1.3s; }
@keyframes breathe { 0%, 100% { transform: scaleY(.32); } 50% { transform: scaleY(1); } }
.chart .axis { stroke: rgba(150, 172, 255, 0.18); stroke-width: 1; }
.chart .real { animation: grow .8s var(--spring) both; animation-delay: calc(var(--i) * 45ms); transition: filter .15s; }
@keyframes grow { from { transform: scaleY(0); } }
.chart .real.hov { filter: brightness(1.45) drop-shadow(0 0 4px rgba(140, 165, 255, 0.9)); }
.chart .real.top.hov { filter: drop-shadow(0 0 6px rgba(255, 255, 255, 0.9)); }
.chart .hit { fill: transparent; cursor: pointer; }
.chart .badge { transform-box: fill-box; transform-origin: 50% 100%; animation: popIn .45s var(--spring) both; animation-delay: .55s; }
.chart .axl { animation: fadeIn .5s ease both; animation-delay: .4s; }
#chartNote.fresh { animation: riseT .5s var(--ease) both, fadeIn .45s ease both; }
.ctip {
  position: absolute; z-index: 5; pointer-events: none; transform: translate(-50%, calc(-100% + 6px)) scale(.92); opacity: 0;
  padding: 7px 12px; border-radius: 12px; white-space: nowrap; text-align: center;
  background: rgba(22, 32, 92, 0.92); border: 1px solid rgba(150, 172, 255, 0.4); box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
  transition: opacity .15s, transform .25s var(--spring), left .18s var(--ease), top .18s var(--ease);
}
.ctip.on { opacity: 1; transform: translate(-50%, -100%) scale(1); }
.ctip b { display: block; font-weight: 600; font-size: 13.5px; }
.ctip span { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
body.jsdrag .dragbar, body.jsdrag .brand { -webkit-app-region: no-drag; }
body.dragging, body.dragging * { cursor: default !important; user-select: none !important; }

.thumb .pv { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .4s ease; pointer-events: none; will-change: opacity; }
.thumb.pvon .pv { opacity: 1; }
.clip .thumb { transition: transform .5s var(--spring), box-shadow .45s ease; will-change: transform; }
.clip:hover .thumb.pvon, .clip .thumb.pvon {
  transform: perspective(700px) rotateX(var(--rx, 0deg)) rotateY(var(--ry, 0deg)) scale(1.22); z-index: 4;
  box-shadow: calc(var(--sx, 0) * -14px) calc(18px + var(--sy, 0) * -10px) 44px rgba(2, 5, 22, 0.65), 0 0 0 1px rgba(160, 180, 255, 0.4), 0 0 34px rgba(91, 124, 255, 0.35); }
.clip .thumb.pvon.tilting { transition: transform .14s ease-out, box-shadow .14s ease-out; }
.thumb .glare { position: absolute; inset: 0; z-index: 2; pointer-events: none; opacity: 0; transition: opacity .35s ease;
  background: radial-gradient(circle at var(--gx, 50%) var(--gy, 30%), rgba(255, 255, 255, 0.26), rgba(255, 255, 255, 0.06) 35%, transparent 62%); }
.thumb.pvon .glare { opacity: 1; }
.thumb .dur { z-index: 2; }
.thumb .pvbar { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; z-index: 3; background: linear-gradient(90deg, #7f9bff, #a38bff); box-shadow: 0 0 8px rgba(127, 155, 255, 0.9); transform-origin: left; transform: scaleX(0); opacity: 0; transition: opacity .3s; pointer-events: none; }
.thumb.pvon .pvbar { opacity: 1; }
.thumb.pvload::after { content: ""; position: absolute; inset: 0; z-index: 1; pointer-events: none; background: linear-gradient(100deg, transparent 30%, rgba(200, 214, 255, 0.22) 50%, transparent 70%); background-size: 220% 100%; animation: pvsh 1.1s linear infinite; }
@keyframes pvsh { from { background-position: 120% 0; } to { background-position: -120% 0; } }
.thumb.pvon .playov { background: transparent; }
.thumb.pvon .playov span { transform: scale(.78); opacity: .38; background: rgba(91, 124, 255, 0.55); box-shadow: none; }
.thumb.pvon .playov:hover span { opacity: .8; }
.thumb .playov span { transition: transform .3s var(--spring), opacity .3s; }

.freq { padding: 10px; }
.freq .fh { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; padding: 2px 4px 9px; }
.freq .fh svg { color: #a9b9ff; }
.freq .fl { display: flex; flex-wrap: wrap; gap: 8px; }
.freq .fq { width: auto; display: inline-flex; align-items: center; gap: 8px; height: 34px; padding: 0 6px 0 12px; border-radius: 20px; border: 1px solid rgba(130, 156, 255, 0.35);
  background: rgba(70, 98, 235, 0.18); color: var(--text); font-size: 13.5px; animation: chipIn .35s var(--spring) both; animation-delay: calc(var(--k) * 30ms);
  transition: background .2s, border-color .2s, transform .25s var(--spring); }
.freq .fq:hover { background: rgba(91, 124, 255, 0.36); border-color: rgba(160, 180, 255, 0.7); transform: translateY(-1px); }
.freq .fq:active { transform: scale(.95); }
.freq .fq img { width: 20px; height: 27px; border-radius: 4px; object-fit: cover; margin-left: -6px; }
.freq .fq .cnt { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.freq .fq .fx { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; color: var(--muted); background: rgba(255, 255, 255, 0.06); transition: background .15s, color .15s; }
.freq .fq .fx:hover { background: #e5384f; color: #fff; }
.freq .fq .fx svg { width: 11px; height: 11px; }

.upd-pill { display: inline-flex; align-items: center; gap: 7px; margin-top: 8px; padding: 6px 12px; border-radius: 20px; border: 1px solid rgba(61, 220, 151, 0.5); background: rgba(61, 220, 151, 0.12);
  color: #bff5dc; font-size: 12.5px; animation: chipIn .45s var(--spring) both, glowp 2.4s ease-in-out infinite; }
.upd-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }
@keyframes glowp { 50% { box-shadow: 0 0 18px rgba(61, 220, 151, 0.35); } }
.side .foot .linkbtn { font-size: 12px; color: var(--faint); margin-top: 4px; display: block; }
.side .foot .linkbtn:hover { color: #c9d3ff; }
.upd .ubig { width: 64px; height: 64px; border-radius: 20px; display: grid; place-items: center; font-size: 30px; color: #fff; margin-bottom: 16px;
  background: radial-gradient(circle at 30% 25%, #8aa2ff, #4262f0 72%); box-shadow: 0 0 36px rgba(80, 110, 255, 0.55); animation: floaty 4s ease-in-out infinite; }
.upd .ubig svg { animation: dlarrow 1.6s ease-in-out infinite; }
@keyframes dlarrow { 50% { transform: translateY(3px); } }
.upd .vers { display: flex; align-items: center; gap: 10px; margin: 2px 0 16px; color: var(--muted); }
.upd .vers b { color: var(--text); font-weight: 500; }
.upd .vers .arrow { color: #a9b9ff; }
.upd ul { margin: 0 0 18px; padding: 0; list-style: none; display: grid; gap: 8px; max-height: 230px; overflow: auto; }
.upd li { position: relative; padding-left: 20px; color: #d3daf7; animation: riseT .45s var(--ease) both, fadeIn .35s ease both; animation-delay: calc(var(--k) * 60ms + .15s); }
.upd li::before { content: ""; position: absolute; left: 3px; top: .55em; width: 7px; height: 7px; border-radius: 50%; background: linear-gradient(180deg, #8aa2ff, #5b7cff); box-shadow: 0 0 8px rgba(91, 124, 255, 0.8); }
.upd .uprog { margin: -4px 0 16px; }
.upd .uprog .note { margin-top: 7px; }

.rsz { position: fixed; z-index: 75; display: none; }
body.jsresize:not(.maxed) .rsz { display: block; }
.rsz.t { top: 0; left: 10px; right: 160px; height: 4px; cursor: ns-resize; }
.rsz.tl { top: 0; left: 0; width: 10px; height: 10px; cursor: nwse-resize; }

body.glass-os .card, body.glass-os .clip, body.glass-os .side, body.glass-os .searching, body.glass-os .pop, body.glass-os .sug,
body.glass-os .toast, body.glass-os .side-status .pill { backdrop-filter: none; -webkit-backdrop-filter: none; }
body.glass-os .pop, body.glass-os .sug { background: rgba(16, 24, 70, 0.94); }
body.glass-os .toast { background: rgba(30, 44, 120, 0.94); }

body { user-select: none; -webkit-user-select: none; }
input, textarea, .errmsg { user-select: text; -webkit-user-select: text; }
.cmenu { position: fixed; z-index: 95; min-width: 270px; max-width: 360px; padding: 6px; border-radius: 16px;
  background: rgba(18, 26, 76, 0.95); border: 1px solid rgba(160, 180, 255, 0.28);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(0, 0, 0, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.08);
  transform-origin: var(--ox, 0) var(--oy, 0); animation: cmIn .28s var(--spring) both; }
.cmenu.out { animation: cmOut .14s ease both; pointer-events: none; }
@keyframes cmIn { from { opacity: 0; transform: scale(.88); } }
@keyframes cmOut { to { opacity: 0; transform: scale(.96); } }
.cm-i { display: flex; align-items: center; gap: 11px; width: 100%; min-height: 36px; padding: 0 10px; border: 0; background: none; border-radius: 10px;
  color: var(--text); font-size: 13.5px; text-align: left; animation: cmItem .32s var(--ease) both; animation-delay: calc(var(--k, 0) * 16ms);
  transition: background .12s; }
@keyframes cmItem { from { opacity: 0; transform: translateX(-6px); } }
.cm-i > svg.i { font-size: 16px; color: #a9b9ff; transition: transform .25s var(--spring); }
.cm-i:hover, .cm-i.act { background: rgba(91, 124, 255, 0.24); }
.cm-i:hover > svg.i, .cm-i.act > svg.i { transform: scale(1.15); }
.cm-i:active { background: rgba(91, 124, 255, 0.36); }
.cm-i .lbl2 { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cm-i kbd { margin-left: auto; flex: none; }
.cm-i.tall { padding: 8px 10px; align-items: flex-start; }
.cm-i.tall .lbl2 { white-space: normal; }
.cm-i .sub2 { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
.cm-i .chk { margin-left: auto; color: var(--ok); opacity: 0; transform: scale(.4); transition: opacity .2s, transform .3s var(--spring); flex: none; align-self: center; }
.cm-i.on .chk { opacity: 1; transform: none; }
.cm-sep { height: 1px; margin: 5px 8px; background: rgba(150, 172, 255, 0.14); }
.cm-h { padding: 7px 10px 6px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 340px; }
.cm-q { display: flex; gap: 6px; align-items: center; padding: 2px 10px 6px 37px; color: var(--faint); font-size: 12px; animation: cmItem .32s var(--ease) both; animation-delay: calc(var(--k, 0) * 16ms); }
.cm-q button { height: 26px; padding: 0 10px; border-radius: 14px; border: 1px solid rgba(150, 172, 255, 0.25); background: rgba(8, 13, 38, 0.6); color: #c9d3ff; font-size: 12px; transition: background .15s, transform .2s var(--spring); }
.cm-q button:hover { background: rgba(91, 124, 255, 0.35); transform: translateY(-1px); }
.rec { display: inline-block; margin-left: 8px; padding: 1px 7px; border-radius: 10px; font-size: 10.5px; font-weight: 500; vertical-align: 1px;
  background: rgba(61, 220, 151, 0.16); color: #9ff0c9; border: 1px solid rgba(61, 220, 151, 0.4); }
.qbtn .qtext { flex: 1; text-align: left; }
.qbtn .chev { transition: transform .3s var(--spring); }
.qbtn.open .chev { transform: rotate(180deg); }
.clip.kfocus { box-shadow: 0 0 0 2px rgba(127, 155, 255, 0.9), 0 0 28px rgba(91, 124, 255, 0.35); border-color: transparent; }
.clip.kpulse { animation: kpulse .45s var(--spring); }
@keyframes kpulse { 0% { transform: scale(.985); } 60% { transform: scale(1.006); } }
.kbtn { width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; border: 1px solid rgba(150, 172, 255, 0.16); background: rgba(10, 16, 46, 0.42); color: var(--muted); font-size: 16px; margin-left: 4px; }
.kbtn:hover { color: #fff; border-color: #8aa0ff; }
.keys .kl { display: grid; grid-template-columns: auto 1fr; gap: 10px 16px; align-items: center; margin: 4px 0 20px; }
.keys .kl > div { display: flex; gap: 5px; justify-content: flex-end; }
.keys .kl span { color: #d3daf7; }
.keys .kl > * { animation: riseT .4s var(--ease) both, fadeIn .3s ease both; animation-delay: calc(var(--k) * 35ms + .1s); }
.keys kbd { font-size: 12px; padding: 3px 8px; border-radius: 8px; background: rgba(8, 13, 38, 0.6); color: #e1e7ff; border-color: rgba(160, 180, 255, 0.35); box-shadow: inset 0 -2px 0 rgba(0, 0, 0, 0.35); }

.dlpair { display: inline-flex; align-items: center; }
.dlpair .btn { isolation: isolate; transition: transform .28s var(--spring), border-color .3s, box-shadow .4s, color .3s, max-width .55s var(--spring), padding .45s var(--spring), margin .45s var(--spring), opacity .3s; }
.dlpair .btn.primary { background: rgba(10, 16, 46, 0.42); }
.dlpair .btn::before { content: ""; position: absolute; inset: 0; z-index: -1; border-radius: inherit; background: linear-gradient(180deg, #5f80ff, #3b59e6); opacity: 0; transition: opacity .4s ease; }
.dlpair .btn.primary::before { opacity: 1; }
.dlpair .btn.primary { border-color: transparent; box-shadow: 0 0 28px rgba(80, 110, 255, 0.45); }
#pCut { max-width: 220px; margin-left: 10px; white-space: nowrap; }
.dlpair:not(.cut) #pCut { max-width: 0; padding-left: 0; padding-right: 0; margin-left: 0; opacity: 0; transform: translateX(-26px) scale(.85); border-width: 0; pointer-events: none; }
.rsbtn { width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; border: 1px solid rgba(150, 172, 255, 0.18); background: rgba(10, 16, 46, 0.42);
  color: var(--muted); font-size: 15px; flex: none; transition: color .2s, border-color .2s, background .2s, transform .35s var(--spring); }
.rsbtn:hover { color: #fff; border-color: rgba(255, 107, 126, 0.6); background: rgba(229, 56, 79, 0.25); transform: rotate(90deg); }
.rsbtn:active { transform: rotate(90deg) scale(.9); }
.list.leaving > * { animation: leaveOut .28s ease both; }
@keyframes leaveOut { to { opacity: 0; transform: translateY(10px) scale(.985); } }
.res-head.leaving { animation: fadeOut .25s ease both; }

.dl .t { display: flex; align-items: center; gap: 10px; }
.dl .t .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl .t .st { color: var(--muted); flex: none; font-variant-numeric: tabular-nums; transition: color .3s; }
.dl[data-state="done"] .t .st { color: var(--ok); }
.dl[data-state="fail"] .t .st { color: #ffb3bd; }
.dl[data-state="stop"] .t .st { color: var(--faint); }
.dl[data-state="stop"] .t .nm { color: var(--muted); }
.dacts { display: flex; gap: 6px; flex: none; }
.dacts.swap button { animation: chipIn .35s var(--spring) both; }
.dacts.swap button:nth-child(2) { animation-delay: .05s; }
.dacts button { width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center; font-size: 13px; padding: 0;
  background: rgba(10, 16, 46, 0.5); border: 1px solid rgba(150, 172, 255, 0.2); color: var(--muted);
  transition: color .2s, background .2s, border-color .2s, transform .3s var(--spring); }
.dacts button:hover { color: #fff; border-color: #8aa0ff; background: rgba(60, 90, 230, 0.4); transform: translateY(-1px); }
.dacts button:active { transform: scale(.9); }
.dacts button.dx:hover { background: rgba(229, 56, 79, 0.45); border-color: rgba(255, 107, 126, 0.7); }
.dacts button.dr:hover svg { animation: spin .7s var(--ease); }
@keyframes spin { to { transform: rotate(360deg); } }
.hatch.stopped i { background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.25) 0 4px, rgba(255, 255, 255, 0.06) 4px 8px), #5a3a52; }
.dl.gone { animation: leaveOut .25s ease both; }
#railDl .dacts button { width: 24px; height: 24px; font-size: 12px; }

@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<canvas id="stars"></canvas>
<svg id="hills" viewBox="0 0 1440 260" preserveAspectRatio="none" aria-hidden="true">
  <defs>
    <linearGradient id="hg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#26306a"/><stop offset="1" stop-color="#0c1130"/></linearGradient>
    <linearGradient id="hg2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#151c46"/><stop offset="1" stop-color="#080b22"/></linearGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ffb07a" stop-opacity=".55"/><stop offset=".45" stop-color="#9fb2ff" stop-opacity=".25"/><stop offset="1" stop-color="#9fb2ff" stop-opacity=".1"/></linearGradient>
  </defs>
  <path d="M0 120 L70 96 L120 108 L190 62 L240 88 L300 70 L370 104 L450 78 L520 112 L600 84 L680 116 L760 72 L820 92 L900 58 L960 86 L1040 100 L1120 66 L1190 94 L1260 80 L1340 106 L1440 90 L1440 260 L0 260Z" fill="url(#hg)" opacity=".75"/>
  <path d="M0 120 L70 96 L120 108 L190 62 L240 88 L300 70 L370 104 L450 78 L520 112 L600 84 L680 116 L760 72 L820 92 L900 58 L960 86 L1040 100 L1120 66 L1190 94 L1260 80 L1340 106 L1440 90" fill="none" stroke="url(#rim)" stroke-width="1.4"/>
  <path d="M0 176 L90 146 L170 166 L260 128 L350 160 L430 138 L540 176 L650 134 L760 168 L860 142 L980 174 L1090 130 L1190 164 L1290 140 L1440 170 L1440 260 L0 260Z" fill="url(#hg2)"/>
  <path d="M0 222 L120 200 L240 218 L380 194 L520 216 L680 198 L820 220 L980 200 L1120 218 L1280 198 L1440 214 L1440 260 L0 260Z" fill="#05071a"/>
</svg>

<div class="dragbar" id="dragbar"></div>
<div class="rsz t" data-edge="top"></div><div class="rsz tl" data-edge="topleft"></div>
<div class="wc" id="wc" hidden>
  <button data-w="min" aria-label="Свернуть"><svg class="i" viewBox="0 0 24 24"><path d="M5 12h14"/></svg></button>
  <button data-w="max" aria-label="Развернуть"><svg class="i" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="1.5"/></svg></button>
  <button data-w="close" class="x" aria-label="Закрыть"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
</div>
<div class="shell">
  <aside class="side">
    <div class="brand"><img src="/icon.png" alt=""><b>Clip<span>Search</span></b></div>
    <nav class="nav" aria-label="Разделы" id="mainNav"><span class="navind" id="navInd"></span>
      <button class="on" data-view="search"><span class="ic"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span><span>Поиск</span></button>
      <button data-view="downloads"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg></span><span>Загрузки</span><span class="count" id="dlCount" hidden>0</span></button>
    </nav>
    <div class="spacer"></div>
    <div class="side-status"><div class="pill" id="keyPill"><span class="dot"></span><span id="keyText">Проверяю связь…</span></div></div>
    <nav class="nav" aria-label="Программа">
      <button id="navQuit"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/></svg></span><span>Выйти</span></button>
    </nav>
    <div class="foot">Версия __VERSION__<button class="linkbtn" id="updCheck" type="button">Проверить обновления</button><button class="upd-pill" id="updPill" type="button" hidden><span class="dot"></span><span id="updPillText">Доступно обновление</span></button></div>
  </aside>

  <section class="main">

    <div class="scroller" id="scroller">

      <div class="view on" id="v-search">
        <h1>Найдите нужный <b>клип</b></h1>
        <p class="lede">Укажите стримера, игру, слова из названия и точный период. Можно заполнить только часть полей.</p>

        <div class="card filters">
          <div class="row">
            <label class="field" id="fLogin" for="login"><svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>
              <span id="loginChips" style="display:contents"></span><div class="sug freq" id="freqL" hidden></div><input id="login" placeholder="Стример, например shroud. Можно несколько через запятую" autocomplete="off" spellcheck="false"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <div class="field" id="fCat"><svg class="i" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 10v5M4.5 12.5h5M16 11h.01M18.5 14h.01"/></svg>
              <span id="catChips" style="display:contents"></span><input id="cat" placeholder="Категория, например Just Chatting. Можно несколько" autocomplete="off" spellcheck="false"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
              <div class="sug" id="sug" hidden></div><div class="sug freq" id="freqC" hidden></div></div>
          </div>
          <div class="row">
            <label class="field wide" id="fWords" for="words"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
              <input id="words" placeholder="Слова из названия. «-слово» исключает клипы с ним" autocomplete="off"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <label class="field small" id="fCreator" for="creator"><svg class="i" viewBox="0 0 24 24"><path d="M5 20 19 6M14 4l6 6M4 14l6 6"/></svg>
              <span id="crChips" style="display:contents"></span><input id="creator" placeholder="Автор клипа" autocomplete="off" spellcheck="false"><button class="x" type="button" aria-label="Очистить"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></label>
            <label class="field small" id="fViews" for="minViews"><svg class="i" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>
              <input id="minViews" inputmode="numeric" placeholder="Просмотров от" autocomplete="off"></label>
          </div>
          <div class="seg" id="periodSeg" style="position:relative">
            <span class="glide"></span><span class="lbl">Период</span>
            <button class="chip" data-p="1h">1 час</button>
            <button class="chip" data-p="24h">24 часа</button>
            <button class="chip" data-p="7d">7 дней</button>
            <button class="chip" data-p="30d">30 дней</button>
            <button class="chip" data-p="all">Всё время</button>
            <button class="chip" id="rangeBtn" aria-haspopup="dialog"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/></svg><span id="rangeText">Выбрать даты</span></button>
            <div class="pop" id="pop" role="dialog" aria-label="Выбор периода" hidden>
              <div class="presets" id="presets"></div>
              <div class="cal">
                <div class="head">
                  <button type="button" id="prevM" aria-label="Предыдущий месяц"><svg class="i" viewBox="0 0 24 24"><path d="m15 5-7 7 7 7"/></svg></button>
                  <span class="note" id="calHint">Нажмите на первую и последнюю дату</span>
                  <button type="button" id="nextM" aria-label="Следующий месяц"><svg class="i" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg></button>
                </div>
                <div class="months" id="months"></div>
                <div class="inputs">
                  <label for="dFrom">С</label><input id="dFrom" placeholder="дд.мм.гггг" maxlength="10" inputmode="numeric" autocomplete="off">
                  <label for="dTo">по</label><input id="dTo" placeholder="дд.мм.гггг" maxlength="10" inputmode="numeric" autocomplete="off">
                </div>
                <div class="last">Или последние <input id="lastN" value="2" inputmode="numeric" maxlength="4"><select id="lastU"><option value="m">минут</option><option value="h" selected>часов</option><option value="d">дней</option><option value="w">недель</option><option value="mo">месяцев</option></select><button class="btn slim" type="button" id="lastGo">Применить</button></div>
                <div class="foot"><button class="btn slim" type="button" id="popClose">Закрыть</button><button class="btn slim primary" type="button" id="popOk">Готово</button></div>
              </div>
            </div>
          </div>
          <div class="go">
            <button class="btn primary" id="go"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>Искать</button>
            <button class="btn danger" id="stop" hidden>Остановить</button>
            <button class="btn ghostbtn" id="reset" title="Очистить все поля и вернуть период «7 дней»">Сбросить</button>
            <button class="switch" id="deep" role="switch" aria-checked="false" title="Период режется на недельные куски: находит больше старых и непопулярных клипов, но поиск идёт дольше"><span class="tr"></span>Глубокий поиск</button>
            <span class="hint" id="goHint"></span>
          </div>
        </div>

        <div class="grid">
          <div>
            <div id="alert"></div>
            <div class="res-head" id="resHead" hidden>
              <h2 id="resTitle">Найдено</h2>
              <button class="rsbtn" id="resReset" type="button" title="Сбросить результаты (Esc)"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
              <span class="status" id="resStatus"></span>
              <div class="sorts" id="sorts"><span class="glide"></span>
                <button class="chip on" data-s="views">Просмотры</button>
                <button class="chip" data-s="date">Дата</button>
                <button class="chip" data-s="dur">Длительность</button>
                <button class="chip" data-s="title">Название</button>
                <button class="kbtn" id="keysBtn" type="button" title="Горячие клавиши (?)"><svg class="i" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="3"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg></button>
              </div>
            </div>
            <div class="list" id="list"><div class="empty" id="firstEmpty"><div class="big"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></div><h3>Здесь появятся клипы</h3><p>Впишите стримера или категорию, выберите период и нажмите «Искать». Дату можно задать точно: с 01.02.2024 по 03.02.2026.</p></div></div>
            <button class="btn more" id="more" hidden>Показать ещё</button>
          </div>

          <aside class="rail">
            <div class="card" id="selCard">
              <h3>Выбранные клипы</h3>
              <div class="sel-num" id="selNum">0</div>
              <div class="sel-sub" id="selSub">Нажмите на карточки, чтобы выбрать</div>
              <div class="stack">
                <button class="btn qbtn" data-qbtn type="button" title="Качество скачивания"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15V9l2.5 3L12 9v6M15 9h2a2 2 0 0 1 0 6h-2z"/></svg><span class="qtext">Качество: 1080p</span><svg class="i chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>
                <button class="btn" id="bDl" disabled><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>Скачать в 1080p</button>
                <button class="btn" id="bCopy" disabled><svg class="i" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>Скопировать ссылки</button>
                <button class="btn" id="bAll"><svg class="i" viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>Выбрать все найденные</button>
                <button class="btn" id="bCsv" disabled><svg class="i" viewBox="0 0 24 24"><path d="M7 3h8l4 4v14H7zM15 3v5h4M10 13h6M10 17h6"/></svg>Сохранить список в CSV</button>
              </div>
            </div>
            <div class="card">
              <h3>Загрузки <span class="grow"></span><span class="note" id="dlSum"></span></h3>
              <div class="folder"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span id="dirText">…</span><button id="dirOpen">Открыть</button><button id="dirPick">Сменить</button></div>
              <div id="railDl"><div class="note">Скачанные клипы появятся здесь.</div></div>
            </div>
            <div class="card">
              <h3>Когда сняты клипы</h3>
              <svg class="chart" id="chart" viewBox="0 0 300 130" preserveAspectRatio="none" role="img" aria-label="График: сколько найденных клипов приходится на каждый отрезок периода"></svg>
              <div class="ctip" id="chartTip"></div>
              <div class="note" id="chartNote">После поиска здесь появится график.</div>
            </div>
          </aside>
        </div>
      </div>

      <div class="view" id="v-downloads">
        <h1>Загрузки</h1>
        <p class="lede">Клипы, скачанные в этой сессии, и всё, что уже лежит в папке загрузок.</p>
        <div class="card folder-hero">
          <div class="fh-ic"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></div>
          <div class="fh-main">
            <div class="fh-label">Папка загрузок</div>
            <div class="fh-path" id="dirText2">…</div>
            <div class="fh-btns">
              <button class="btn slim" id="dirOpen2"><svg class="i" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>Открыть в проводнике</button>
              <button class="btn slim qbtn" data-qbtn type="button" title="Качество скачивания"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15V9l2.5 3L12 9v6M15 9h2a2 2 0 0 1 0 6h-2z"/></svg><span class="qtext">Качество: 1080p</span><svg class="i chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>
              <button class="btn slim primary" id="dirPick2"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 10v6M9 13h6"/></svg>Сменить папку</button>
            </div>
          </div>
          <div class="fh-stats">
            <div class="stat"><b id="stCount">—</b><span>клипов в папке</span></div>
            <div class="stat"><b id="stSize">—</b><span>занимают клипы</span></div>
            <div class="stat"><b id="stFree">—</b><span>свободно на диске</span></div>
            <div class="disk"><div class="hatch"><i id="stDisk" style="width:0"></i></div><span id="stDiskTxt">Считаю место на диске…</span></div>
          </div>
        </div>
        <div class="dl-grid">
          <div class="card">
            <h3>Очередь этой сессии <span class="grow"></span><span class="note" id="dlSum2"></span><button class="linkbtn" id="dlClear" type="button" hidden>Очистить готовые</button></h3>
            <div id="dlAll"></div>
          </div>
          <div class="card">
            <h3>Последние файлы в папке <span class="grow"></span><button class="linkbtn" id="recentRefresh">Обновить</button></h3>
            <div id="recent"><div class="note">Загружаю список…</div></div>
          </div>
        </div>
      </div>
    </div>
  </section>
</div>

<div class="veil" id="playVeil">
  <div class="modal player" role="dialog" aria-labelledby="pTitle">
    <div class="ph"><div class="grow"><h2 id="pTitle"></h2><div class="meta" id="pMeta"></div></div><button class="xbtn" id="pClose" aria-label="Закрыть"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div>
    <div class="vbox"><video id="pv" controls playsinline preload="auto"></video><div class="vmsg" id="pMsg">Загружаю видео…</div></div>
    <div class="trim">
      <div class="tl" id="tl"><div class="rng" id="tRng"></div><button class="hd" id="hA" aria-label="Начало фрагмента"></button><button class="hd" id="hB" aria-label="Конец фрагмента"></button><div class="ph2" id="tPh"></div></div>
      <div class="tinfo"><span>Начало: <b id="tA">0:00.0</b></span><span>Конец: <b id="tB">0:00.0</b></span><span>Длина фрагмента: <b id="tLen">0 с</b></span></div>
      <div class="tbtns">
        <button class="btn slim" id="bSetA" title="Клавиша I">Начало здесь <kbd>I</kbd></button>
        <button class="btn slim" id="bSetB" title="Клавиша O">Конец здесь <kbd>O</kbd></button>
        <button class="btn slim" id="bPrev">Посмотреть фрагмент</button>
        <button class="btn slim" id="bTrimReset">Весь клип</button>
      </div>
    </div>
    <div class="foot">
      <span class="hint" id="pHint">Тяните ползунки или ставьте метки по ходу видео. Пробел — пауза.</span>
      <button class="btn slim" id="pTwitch">Открыть на Twitch</button>
      <span class="dlpair" id="dlPair"><button class="btn slim primary" id="pFull">Скачать весь клип</button><button class="btn slim" id="pCut">Скачать фрагмент</button></span>
    </div>
  </div>
</div>
<div class="veil" id="updVeil">
  <div class="modal upd" role="dialog" aria-labelledby="uTitle">
    <div class="ubig"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg></div>
    <h2 id="uTitle">Доступна новая версия</h2>
    <div class="vers"><span>У вас <b id="uCur">—</b></span><span class="arrow">→</span><span>новая <b id="uNew">—</b></span></div>
    <ul id="uNotes"></ul>
    <div class="uprog" id="uProg" hidden><div class="hatch run"><i id="uBar" style="width:0"></i></div><div class="note" id="uProgText">Скачиваю установщик…</div></div>
    <div class="foot"><button class="btn" id="uLater">Позже</button><button class="btn primary" id="uGo"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>Обновить</button></div>
  </div>
</div>
<div class="veil" id="keysHelp">
  <div class="modal keys" role="dialog" aria-labelledby="khTitle">
    <h2 id="khTitle">Горячие клавиши</h2>
    <p>Работают в списке найденных клипов, когда курсор не стоит в поле ввода.</p>
    <div class="kl" id="kList"></div>
    <div class="foot"><button class="btn primary" id="khClose">Понятно</button></div>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
(function () {
"use strict";
const T = "__TOKEN__";
if (new URLSearchParams(location.search).get("glass") === "1") { document.documentElement.classList.add("glass-os"); document.body.classList.add("glass-os"); }
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body) {
  const opt = { headers: { "X-Token": T, "Content-Type": "application/json" } };
  if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.json();
}
setInterval(() => { fetch("/api/ping", { headers: { "X-Token": T } }).catch(() => {}); }, 3000);
fetch("/api/ping", { headers: { "X-Token": T } }).catch(() => {});
window.addEventListener("pagehide", () => {
  fetch("/api/bye", { method: "POST", headers: { "X-Token": T }, body: "{}", keepalive: true }).catch(() => {});
});

function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 2600);
}

(function sky() {
  const c = $("stars"), g = c.getContext("2d");
  function draw() {
    const dpr = Math.min(2, devicePixelRatio || 1), W = innerWidth, H = innerHeight;
    c.width = W * dpr; c.height = H * dpr; g.setTransform(dpr, 0, 0, dpr, 0, 0);
    let seed = 11; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    const gauss = () => (rnd() + rnd() + rnd() - 1.5) / 1.5;
    let gr = g.createLinearGradient(0, 0, 0, H);
    gr.addColorStop(0, "#050817"); gr.addColorStop(0.45, "#0a1236"); gr.addColorStop(0.78, "#141e52"); gr.addColorStop(1, "#1d1f4c");
    g.fillStyle = gr; g.fillRect(0, 0, W, H);
    const blob = (x, y, r, col) => { const q = g.createRadialGradient(x, y, 0, x, y, r); q.addColorStop(0, col); q.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = q; g.fillRect(x - r, y - r, r * 2, r * 2); };
    blob(W * 0.86, -H * 0.05, Math.max(W, H) * 0.62, "rgba(90,120,255,0.30)");
    blob(W * 0.12, H * 1.04, Math.max(W, H) * 0.55, "rgba(255,140,86,0.30)");
    blob(W * 0.55, H * 1.1, Math.max(W, H) * 0.5, "rgba(122,99,255,0.18)");

    const ax = W * 0.02, ay = H * 0.92, bx = W * 0.98, by = H * 0.02, nx = -(by - ay), ny = bx - ax, nl = Math.hypot(nx, ny);
    for (let i = 0; i < 46; i++) {
      const t = rnd(), o = gauss() * 0.09 * nl, x = ax + (bx - ax) * t + nx / nl * o, y = ay + (by - ay) * t + ny / nl * o;
      blob(x, y, 60 + rnd() * 170, i % 3 ? "rgba(120,140,255,0.055)" : "rgba(190,150,255,0.05)");
    }
    for (let i = 0; i < 1400; i++) {
      const t = rnd(), o = gauss() * 0.07 * nl, x = ax + (bx - ax) * t + nx / nl * o, y = ay + (by - ay) * t + ny / nl * o;
      g.fillStyle = `rgba(215,225,255,${0.08 + rnd() * 0.35})`; g.fillRect(x, y, rnd() < 0.85 ? 0.8 : 1.4, rnd() < 0.85 ? 0.8 : 1.4);
    }
    for (let i = 0; i < 240; i++) {
      const x = rnd() * W, y = rnd() * H * 0.85, r = rnd() * 1.1 + 0.25;
      g.fillStyle = `rgba(215,225,255,${0.2 + rnd() * 0.6})`; g.beginPath(); g.arc(x, y, r, 0, 6.3); g.fill();
      if (r > 1.2) { blob(x, y, 7, "rgba(170,190,255,0.35)"); }
    }
  }
  if (document.body.classList.contains("glass-os")) return;
  draw(); let rt = 0; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(draw, 120); });
})();

function moveGlide(box) {
  if (!box) return;
  const gl = box.querySelector(".glide"), on = box.querySelector(".chip.on");
  if (!gl) return;
  if (!on || !box.offsetParent) { gl.style.opacity = 0; return; }
  gl.style.left = on.offsetLeft + "px"; gl.style.top = on.offsetTop + "px";
  gl.style.width = on.offsetWidth + "px"; gl.style.height = on.offsetHeight + "px"; gl.style.opacity = 1;
}
function moveNav() {
  const ind = $("navInd"), on = document.querySelector("#mainNav button.on");
  if (!ind || !on) return;
  const ic = on.querySelector(".ic");
  ind.style.top = (on.offsetTop + ic.offsetTop) + "px"; ind.style.left = (on.offsetLeft + ic.offsetLeft) + "px";
}
function reglide() { moveGlide($("periodSeg")); moveGlide($("sorts")); moveNav(); }
addEventListener("resize", reglide);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(reglide);
document.addEventListener("pointerdown", (e) => {
  const el = e.target.closest(".btn, .chip, .acts button, .nav button, .xbtn, .round-btn");
  if (!el || el.disabled || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const r = el.getBoundingClientRect(), size = Math.max(r.width, r.height) * 2.2, sp = document.createElement("span");
  sp.className = "rip"; sp.style.width = sp.style.height = size + "px";
  sp.style.left = (e.clientX - r.left - size / 2) + "px"; sp.style.top = (e.clientY - r.top - size / 2) + "px";
  el.appendChild(sp); sp.addEventListener("animationend", () => sp.remove());
});
function showVeil(v) { v.classList.remove("closing"); v.classList.add("on"); }
function hideVeil(v) {
  if (!v.classList.contains("on") || v.classList.contains("closing")) return;
  v.classList.add("closing"); clearTimeout(v._t);
  v._t = setTimeout(() => v.classList.remove("on", "closing"), 190);
}
const isOpen = (v) => v.classList.contains("on") && !v.classList.contains("closing");

const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const day0 = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const endOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59);
const fmtD = (d) => String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear();
function parseD(s) {
  const m = /^(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{4})$/.exec(s.trim());
  if (!m) return null;
  const d = new Date(+m[3], +m[2] - 1, +m[1]);
  return d.getFullYear() === +m[3] && d.getMonth() === +m[2] - 1 && d.getDate() === +m[1] ? d : null;
}
const sameDay = (a, b) => a && b && a.getTime() === b.getTime();
const TODAY = day0(new Date());
const UNIT = { m: 1, h: 60, d: 1440, w: 10080, mo: 43200 };
const UNIT_NAME = { m: ["минуту", "минуты", "минут"], h: ["час", "часа", "часов"], d: ["день", "дня", "дней"], w: ["неделю", "недели", "недель"], mo: ["месяц", "месяца", "месяцев"] };
function plural(n, f) { const a = n % 100, b = a % 10; return a > 10 && a < 20 ? f[2] : b > 1 && b < 5 ? f[1] : b === 1 ? f[0] : f[2]; }

const S = {
  period: { kind: "preset", key: "7d" },
  rows: [], shown: 0, sort: { key: "views", desc: true },
  selected: new Set(), catId: "", catName: "",
  job: null, downloads: new Map(), dlJob: null, link: null, dir: "",
};
const PRESETS = { "1h": 60, "24h": 1440, "7d": 10080, "30d": 43200 };

function periodRange() {
  const p = S.period, now = new Date();
  if (p.kind === "preset") {
    if (p.key === "all") return { start: null, end: null };
    return { start: new Date(now - PRESETS[p.key] * 60000), end: now };
  }
  if (p.kind === "range") return { start: day0(p.from), end: endOfDay(p.to) };
  return { start: new Date(now - p.n * UNIT[p.u] * 60000), end: now };
}
function periodText() {
  const p = S.period;
  if (p.kind === "preset") return { all: "за всё время" }[p.key] || "за " + { "1h": "1 час", "24h": "24 часа", "7d": "7 дней", "30d": "30 дней" }[p.key];
  if (p.kind === "range") return `с ${fmtD(p.from)} по ${fmtD(p.to)}`;
  return `за ${p.n} ${plural(p.n, UNIT_NAME[p.u])}`;
}
function paintPeriod() {
  document.querySelectorAll("#periodSeg [data-p]").forEach((b) => b.classList.toggle("on", S.period.kind === "preset" && S.period.key === b.dataset.p));
  const rb = $("rangeBtn"), isCustom = S.period.kind !== "preset";
  rb.classList.toggle("on", isCustom);
  $("rangeText").textContent = isCustom ? periodText().replace(/^за /, "Последние ") : "Выбрать даты";
  moveGlide($("periodSeg"));
}
document.querySelectorAll("#periodSeg [data-p]").forEach((b) => b.addEventListener("click", () => { S.period = { kind: "preset", key: b.dataset.p }; paintPeriod(); closePop(); }));

const cal = { y: TODAY.getFullYear(), m: TODAY.getMonth() - 1, from: null, to: null, hover: null };
function openPop() {
  const p = S.period;
  if (p.kind === "range") { cal.from = day0(p.from); cal.to = day0(p.to); cal.y = cal.to.getFullYear(); cal.m = cal.to.getMonth() - 1; }
  else { cal.from = cal.to = null; cal.y = TODAY.getFullYear(); cal.m = TODAY.getMonth() - 1; }
  fixMonth(); $("pop").hidden = false; placePop(); drawCal(); syncInputs();
}
function placePop() {
  const pop = $("pop"), seg = $("periodSeg"), btn = $("rangeBtn");
  const segR = seg.getBoundingClientRect(), bR = btn.getBoundingClientRect();
  let left = bR.right - segR.left - pop.offsetWidth;
  left = Math.max(-8, Math.min(left, segR.width - pop.offsetWidth + 8));
  pop.style.left = left + "px"; pop.style.top = (bR.bottom - segR.top + 10) + "px";
}
function closePop() { $("pop").hidden = true; }
function fixMonth() { while (cal.m < 0) { cal.m += 12; cal.y--; } while (cal.m > 11) { cal.m -= 12; cal.y++; } }
function drawCal() {
  fixMonth();
  const box = $("months"); box.innerHTML = "";
  for (let k = 0; k < 2; k++) {
    let y = cal.y, m = cal.m + k; if (m > 11) { m -= 12; y++; }
    const mm = document.createElement("div"); mm.className = "month";
    mm.innerHTML = `<div class="name">${MONTHS[m]} ${y}</div><div class="dow">${["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((d) => `<span>${d}</span>`).join("")}</div>`;
    const days = document.createElement("div"); days.className = "days";
    const first = (new Date(y, m, 1).getDay() + 6) % 7, count = new Date(y, m + 1, 0).getDate();
    for (let i = 0; i < first; i++) days.appendChild(document.createElement("i"));
    for (let d = 1; d <= count; d++) {
      const dt = new Date(y, m, d), b = document.createElement("button");
      b.type = "button"; b.textContent = d; b.dataset.t = dt.getTime();
      if (dt > TODAY) b.disabled = true;
      if (sameDay(dt, TODAY)) b.classList.add("today");
      const a = cal.from, z = cal.to || (a && cal.hover && cal.hover >= a ? cal.hover : null);
      if (sameDay(dt, a) || sameDay(dt, cal.to)) b.classList.add("sel");
      else if (a && z && dt > a && dt < z) b.classList.add("in");
      days.appendChild(b);
    }
    mm.appendChild(days); box.appendChild(mm);
  }
  $("calHint").textContent = cal.from && !cal.to ? "Теперь выберите последнюю дату" : "Нажмите на первую и последнюю дату";
}
$("months").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-t]"); if (!b) return;
  const d = new Date(+b.dataset.t);
  if (!cal.from || cal.to) { cal.from = d; cal.to = null; }
  else if (d < cal.from) { cal.to = cal.from; cal.from = d; }
  else cal.to = d;
  cal.hover = null; drawCal(); syncInputs();
});
$("months").addEventListener("mouseover", (e) => {
  const b = e.target.closest("button[data-t]");
  if (b && cal.from && !cal.to && !sameDay(cal.hover, new Date(+b.dataset.t))) { cal.hover = new Date(+b.dataset.t); drawCal(); }
});
function syncInputs() { $("dFrom").value = cal.from ? fmtD(cal.from) : ""; $("dTo").value = cal.to ? fmtD(cal.to) : cal.from ? fmtD(cal.from) : ""; $("dFrom").classList.remove("bad"); $("dTo").classList.remove("bad"); }
function typed() {
  const a = parseD($("dFrom").value), b = parseD($("dTo").value || $("dFrom").value);
  $("dFrom").classList.toggle("bad", !a && !!$("dFrom").value);
  $("dTo").classList.toggle("bad", !b && !!$("dTo").value);
  if (a && b) {
    cal.from = a <= b ? a : b; cal.to = a <= b ? b : a;
    if (cal.to > TODAY) cal.to = TODAY;
    cal.y = cal.from.getFullYear(); cal.m = cal.from.getMonth(); if (cal.to.getFullYear() * 12 + cal.to.getMonth() > cal.y * 12 + cal.m + 1) cal.m = cal.to.getMonth() - 1;
    drawCal();
  }
}
["dFrom", "dTo"].forEach((id) => {
  $(id).addEventListener("input", (e) => {
    let v = e.target.value.replace(/[^\d.]/g, "");
    if (e.inputType !== "deleteContentBackward") v = v.replace(/^(\d{2})(\d)/, "$1.$2").replace(/^(\d{2}\.\d{2})(\d)/, "$1.$2");
    e.target.value = v.slice(0, 10); typed();
  });
});
$("prevM").addEventListener("click", () => { cal.m--; drawCal(); });
$("nextM").addEventListener("click", () => { cal.m++; drawCal(); });
$("rangeBtn").addEventListener("click", () => ($("pop").hidden ? openPop() : closePop()));
$("popClose").addEventListener("click", closePop);
function applyRange() {
  if (!cal.from) { toast("Выберите даты в календаре"); return; }
  S.period = { kind: "range", from: cal.from, to: cal.to || cal.from }; paintPeriod(); closePop();
}
$("popOk").addEventListener("click", applyRange);
$("lastGo").addEventListener("click", () => {
  const n = parseInt($("lastN").value, 10);
  if (!(n > 0)) { toast("Впишите число больше нуля"); return; }
  S.period = { kind: "last", n, u: $("lastU").value }; paintPeriod(); closePop();
});
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const PRE = [
  ["Сегодня", () => [TODAY, TODAY]],
  ["Вчера", () => [addDays(TODAY, -1), addDays(TODAY, -1)]],
  ["Последние 7 дней", () => [addDays(TODAY, -6), TODAY]],
  ["Последние 30 дней", () => [addDays(TODAY, -29), TODAY]],
  ["Последние 90 дней", () => [addDays(TODAY, -89), TODAY]],
  ["Этот год", () => [new Date(TODAY.getFullYear(), 0, 1), TODAY]],
  ["Прошлый год", () => [new Date(TODAY.getFullYear() - 1, 0, 1), new Date(TODAY.getFullYear() - 1, 11, 31)]],
];
PRE.forEach(([name, fn]) => {
  const b = document.createElement("button"); b.type = "button"; b.textContent = name;
  b.addEventListener("click", () => { const [a, z] = fn(); cal.from = a; cal.to = z; cal.y = z.getFullYear(); cal.m = z.getMonth() - 1; drawCal(); syncInputs(); });
  $("presets").appendChild(b);
});
document.addEventListener("click", (e) => {
  const path = e.composedPath();
  if (!$("pop").hidden && !path.includes($("pop")) && !path.includes($("rangeBtn"))) closePop();
  if (!path.includes($("fCat"))) $("sug").hidden = true;
});
addEventListener("resize", () => { if (!$("pop").hidden) placePop(); });
addEventListener("keydown", (e) => {
  if (e.key === "Escape" && isOpen($("playVeil"))) { closePlayer(); return; }
  if (e.key === "Escape") { closePop(); $("sug").hidden = true; }
});

S.logins = [];
function cleanNick(t) { return t.trim().toLowerCase().replace(/^(https?:\/\/)?(www\.)?twitch\.tv\//, "").split(/[/?]/)[0].replace(/^@/, ""); }
function addLogins(text) {
  text.split(/[,;\s]+/).map(cleanNick).filter(Boolean).forEach((n) => { if (!S.logins.includes(n) && S.logins.length < 10) S.logins.push(n); });
}
let paintedChips = new Set();
function paintChips() {
  $("loginChips").innerHTML = S.logins.map((n, i) => `<span class="chip-l${paintedChips.has(n) ? "" : " new"}">${esc(n)}<button type="button" data-rm="${i}" aria-label="Убрать ${esc(n)}"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></span>`).join("");
  $("login").placeholder = S.logins.length ? (S.logins.length < 10 ? "Ещё стример…" : "") : "Стример, например shroud. Можно несколько через запятую";
  $("fLogin").classList.toggle("has", !!(S.logins.length || $("login").value));
  paintedChips = new Set(S.logins);
}
function commitLogin() { const v = $("login").value; if (v.trim()) { addLogins(v); $("login").value = ""; paintChips(); } }
function setLogins(text) { S.logins = []; addLogins(String(text || "")); $("login").value = ""; paintChips(); }
$("loginChips").addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (!b) return; e.preventDefault(); S.logins.splice(+b.dataset.rm, 1); paintChips(); $("login").focus(); });
$("login").addEventListener("input", () => {
  const v = $("login").value;
  if (/[,;\s]/.test(v)) { const parts = v.split(/[,;\s]+/); const rest = parts.pop(); addLogins(parts.join(",")); $("login").value = rest; }
  paintChips();
});
$("login").addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && !$("login").value && S.logins.length) { S.logins.pop(); paintChips(); }
});
$("login").addEventListener("blur", commitLogin);
$("login").addEventListener("keydown", (e) => { if (e.key === "Enter") startSearch(); });
$("fLogin").querySelector(".x").addEventListener("click", () => setLogins(""));

const P = { row: null, dur: 0, a: 0, b: 0, preview: false, drag: null };
const fmtT = (t) => { t = Math.max(0, t); const m = Math.floor(t / 60), s = t - m * 60; return m + ":" + s.toFixed(1).padStart(4, "0"); };
async function openPlayer(r) {
  stopPreview();
  const v = $("pv");
  P.row = r; P.dur = r.duration || 0; P.a = 0; P.b = P.dur; P.preview = false;
  $("pTitle").textContent = r.title;
  $("pMeta").innerHTML = (r.broadcaster ? `<span>${IC.tv}${esc(r.broadcaster)}</span>` : "") + `<span>${IC.user}${esc(r.creator || "неизвестно")}</span>` + (r.game ? `<span>${IC.game}${esc(r.game)}</span>` : "") + `<span>${IC.eye}${r.views.toLocaleString("ru-RU")}</span>`;
  $("pMsg").hidden = false; $("pMsg").textContent = "Загружаю видео…";
  v.removeAttribute("src"); v.load();
  showVeil($("playVeil")); paintTrim(); S.dlFresh = null; paintPlayerDl();
  const res = await api("/api/clipurl", { id: r.id });
  if (P.row !== r) return;
  if (res.error) { $("pMsg").textContent = res.error; return; }
  v.src = res.url; v.play().catch(() => {});
}
function closePlayer() { const v = $("pv"); v.pause(); v.removeAttribute("src"); v.load(); hideVeil($("playVeil")); P.row = null; }
$("pClose").addEventListener("click", closePlayer);
$("playVeil").addEventListener("mousedown", (e) => { if (e.target === $("playVeil")) closePlayer(); });
$("pv").addEventListener("loadedmetadata", () => {
  const d = $("pv").duration; $("pMsg").hidden = true;
  if (isFinite(d) && d > 0) { const full = P.b >= P.dur - 0.05; P.dur = d; if (full || P.b > d) P.b = d; if (P.a > d) P.a = 0; }
  paintTrim();
});
$("pv").addEventListener("error", () => { if (P.row && $("pv").getAttribute("src")) { $("pMsg").hidden = false; $("pMsg").textContent = "Не удалось воспроизвести видео. Попробуйте открыть клип на Twitch."; } });
$("pv").addEventListener("timeupdate", () => {
  const v = $("pv");
  if (P.preview && v.currentTime >= P.b) { v.pause(); P.preview = false; }
  paintHead();
});
function paintHead() { const d = P.dur || 1; $("tPh").style.left = (Math.min($("pv").currentTime || 0, d) / d * 100) + "%"; }
function paintTrim() {
  const d = P.dur || 1, a = P.a / d * 100, b = P.b / d * 100;
  $("tRng").style.left = a + "%"; $("tRng").style.width = Math.max(0, b - a) + "%";
  $("hA").style.left = a + "%"; $("hB").style.left = b + "%";
  $("tA").textContent = fmtT(P.a); $("tB").textContent = fmtT(P.b);
  const len = Math.max(0, P.b - P.a); $("tLen").textContent = len.toFixed(1).replace(".", ",") + " с";
  const cut = P.dur > 0 && (P.a > 0.05 || P.b < P.dur - 0.05);
  $("pCut").disabled = !cut || len < 0.5; $("bTrimReset").disabled = !cut;

  $("dlPair").classList.toggle("cut", cut); $("pFull").classList.toggle("primary", !cut); $("pCut").classList.toggle("primary", cut);
  paintHead();
}
const MINLEN = 0.5;
function setA(t) { P.a = Math.max(0, Math.min(t, P.b - MINLEN)); paintTrim(); }
function setB(t) { P.b = Math.min(P.dur, Math.max(t, P.a + MINLEN)); paintTrim(); }
function tlTime(e) { const r = $("tl").getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * P.dur; }
["hA", "hB"].forEach((id) => {
  const h = $(id);
  h.addEventListener("pointerdown", (e) => { e.preventDefault(); e.stopPropagation(); P.drag = id; h.setPointerCapture(e.pointerId); $("pv").pause(); });
  h.addEventListener("pointermove", (e) => {
    if (P.drag !== id) return;
    const t = tlTime(e); if (id === "hA") setA(t); else setB(t);
    $("pv").currentTime = id === "hA" ? P.a : P.b; paintHead();
  });
  h.addEventListener("pointerup", () => { P.drag = null; });
  h.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 1 : 0.1, k = { ArrowLeft: -step, ArrowRight: step }[e.key];
    if (k == null) return; e.preventDefault();
    if (id === "hA") setA(P.a + k); else setB(P.b + k);
    $("pv").currentTime = id === "hA" ? P.a : P.b;
  });
});
$("tl").addEventListener("pointerdown", (e) => { if (e.target.closest(".hd") || !P.dur) return; $("pv").currentTime = tlTime(e); paintHead(); });
$("bSetA").addEventListener("click", () => setA($("pv").currentTime || 0));
$("bSetB").addEventListener("click", () => setB($("pv").currentTime || 0));
$("bTrimReset").addEventListener("click", () => { P.a = 0; P.b = P.dur; paintTrim(); });
$("bPrev").addEventListener("click", () => { const v = $("pv"); v.currentTime = P.a; P.preview = true; v.play().catch(() => {}); });
$("pTwitch").addEventListener("click", () => P.row && api("/api/openurl", { url: P.row.url }));
$("pFull").addEventListener("click", () => P.row && startDownload([P.row]));
$("pCut").addEventListener("click", () => { if (P.row) startDownload([{ ...P.row, start: +P.a.toFixed(2), end: +P.b.toFixed(2) }]); });
addEventListener("keydown", (e) => {
  if (!isOpen($("playVeil")) || e.target.tagName === "INPUT") return;
  const v = $("pv");
  if (e.key === "i" || e.key === "I" || e.key === "ш" || e.key === "Ш") { setA(v.currentTime || 0); e.preventDefault(); }
  else if (e.key === "o" || e.key === "O" || e.key === "щ" || e.key === "Щ") { setB(v.currentTime || 0); e.preventDefault(); }
  else if (e.key === " " && e.target !== v) { e.preventDefault(); if (v.paused) v.play().catch(() => {}); else v.pause(); }
});

["fWords"].forEach((id) => {
  const f = $(id), inp = f.querySelector("input"), x = f.querySelector(".x");
  const upd = () => f.classList.toggle("has", !!inp.value);
  inp.addEventListener("input", upd); upd();
  x.addEventListener("click", () => { inp.value = ""; upd(); inp.dispatchEvent(new Event("input")); inp.focus(); });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") startSearch(); });
});

const MAXC = 10;
const XB = (i, name) => `<button type="button" data-rm="${i}" aria-label="Убрать ${esc(name)}"><svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>`;
S.cats = []; S.creators = [];
let paintedCats = new Set(), paintedCr = new Set();
function paintCats() {
  $("catChips").innerHTML = S.cats.map((c, i) => `<span class="chip-l${paintedCats.has(c.name) ? "" : " new"}">${c.art ? `<img src="${esc(c.art)}" alt="">` : ""}${esc(c.name)}${XB(i, c.name)}</span>`).join("");
  $("cat").placeholder = S.cats.length ? (S.cats.length < MAXC ? "Ещё категория…" : "") : "Категория, например Just Chatting. Можно несколько";
  $("fCat").classList.toggle("has", !!(S.cats.length || $("cat").value));
  paintedCats = new Set(S.cats.map((c) => c.name));
}
function addCat(c) {
  const name = String((c && c.name) || "").trim();
  if (!name || S.cats.length >= MAXC || S.cats.some((x) => x.name.toLowerCase() === name.toLowerCase())) return;
  S.cats.push({ id: c.id || "", name, art: c.art || "" });
}
function setCats(list) { S.cats = []; (list || []).forEach(addCat); $("cat").value = ""; paintCats(); }
function commitCat() { const v = $("cat").value; if (v.trim()) { v.split(",").forEach((n) => addCat({ name: n })); $("cat").value = ""; paintCats(); } }
$("catChips").addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (!b) return; e.preventDefault(); e.stopPropagation(); S.cats.splice(+b.dataset.rm, 1); paintCats(); $("cat").focus(); paintFreqC(); });
$("cat").addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && !$("cat").value && S.cats.length) { S.cats.pop(); paintCats(); paintFreqC(); }
  if (e.key === "Enter" && !(!$("sug").hidden && sugIdx >= 0)) startSearch();
});
$("fCat").querySelector(".x").addEventListener("click", () => { setCats([]); $("sug").hidden = true; $("cat").focus(); paintFreqC(); });

function paintCreators() {
  $("crChips").innerHTML = S.creators.map((n, i) => `<span class="chip-l${paintedCr.has(n) ? "" : " new"}">${esc(n)}${XB(i, n)}</span>`).join("");
  $("creator").placeholder = S.creators.length ? (S.creators.length < MAXC ? "Ещё автор…" : "") : "Автор клипа";
  $("fCreator").classList.toggle("has", !!(S.creators.length || $("creator").value));
  paintedCr = new Set(S.creators);
}
function addCreators(text) {
  String(text || "").split(/[,;\s]+/).map((t) => t.trim().replace(/^@/, "")).filter(Boolean)
    .forEach((n) => { if (S.creators.length < MAXC && !S.creators.some((x) => x.toLowerCase() === n.toLowerCase())) S.creators.push(n); });
}
function setCreators(list) { S.creators = []; (Array.isArray(list) ? list : [list]).forEach(addCreators); $("creator").value = ""; paintCreators(); }
function commitCreator() { const v = $("creator").value; if (v.trim()) { addCreators(v); $("creator").value = ""; paintCreators(); } }
$("crChips").addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (!b) return; e.preventDefault(); S.creators.splice(+b.dataset.rm, 1); paintCreators(); $("creator").focus(); });
$("creator").addEventListener("input", () => {
  const v = $("creator").value;
  if (/[,;\s]/.test(v)) { const parts = v.split(/[,;\s]+/); const rest = parts.pop(); addCreators(parts.join(",")); $("creator").value = rest; }
  paintCreators();
});
$("creator").addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && !$("creator").value && S.creators.length) { S.creators.pop(); paintCreators(); }
  if (e.key === "Enter") startSearch();
});
$("creator").addEventListener("blur", commitCreator);
$("fCreator").querySelector(".x").addEventListener("click", () => { setCreators([]); $("creator").focus(); });
$("minViews").addEventListener("keydown", (e) => { if (e.key === "Enter") startSearch(); });
$("deep").addEventListener("click", () => { const b = $("deep"); b.setAttribute("aria-checked", b.getAttribute("aria-checked") === "true" ? "false" : "true"); });

let catTimer = 0, sugIdx = -1, sugItems = [];
$("cat").addEventListener("input", () => {
  const raw = $("cat").value;
  if (raw.includes(",")) { const parts = raw.split(","); const rest = parts.pop(); parts.forEach((n) => addCat({ name: n })); $("cat").value = rest.trimStart(); }
  paintCats(); paintFreqC();
  clearTimeout(catTimer);
  const q = $("cat").value.trim();
  if (q.length < 2) { $("sug").hidden = true; return; }
  catTimer = setTimeout(async () => {
    const r = await api("/api/categories", { query: q });
    if ($("cat").value.trim() !== q) return;
    sugItems = r.items || []; sugIdx = -1; drawSug(r.error);
  }, 320);
});
function drawSug(err) {
  const box = $("sug"); box.innerHTML = "";
  if (err) box.innerHTML = `<div class="none">${esc(err)}</div>`;
  else if (!sugItems.length) box.innerHTML = '<div class="none">Такой категории не нашлось</div>';
  sugItems.forEach((c, i) => {
    const b = document.createElement("button"); b.type = "button";
    b.innerHTML = `<img src="${esc(c.art)}" alt="" loading="lazy"><span>${esc(c.name)}</span>`;
    b.addEventListener("click", () => pickCat(c));
    if (i === sugIdx) b.classList.add("act");
    box.appendChild(b);
  });
  box.hidden = false;
}
function pickCat(c) { addCat(c); $("cat").value = ""; paintCats(); $("sug").hidden = true; sugItems = []; sugIdx = -1; paintFreqC(); }
$("sug").addEventListener("mousedown", (e) => e.preventDefault());
$("cat").addEventListener("keydown", (e) => {
  if ($("sug").hidden || !sugItems.length) return;
  if (e.key === "Enter" && sugIdx < 0) return;
  if (e.key === "ArrowDown") { sugIdx = (sugIdx + 1) % sugItems.length; drawSug(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sugIdx = (sugIdx - 1 + sugItems.length) % sugItems.length; drawSug(); e.preventDefault(); }
  else if (e.key === "Enter" && sugIdx >= 0) { pickCat(sugItems[sugIdx]); e.preventDefault(); }
});

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function showAlert(t) { $("alert").innerHTML = t ? `<div class="alert">${esc(t)}</div>` : ""; }
function setBusy(b) { $("go").disabled = b; $("stop").hidden = !b; $("sorts").classList.toggle("locked", b); $("resReset").hidden = b; }

async function startSearch() {
  if ($("go").disabled) return;
  showAlert("");
  commitLogin(); commitCreator(); commitCat(); $("sug").hidden = true;
  const logins = S.logins.slice(), login = logins.join(", "), cats = S.cats.map((c) => ({ name: c.name, id: c.id })), creators = S.creators.slice();
  if (!logins.length && !cats.length) { showAlert("Укажите стримера или категорию. Можно и то и другое."); $("login").focus(); return; }
  const { start, end } = periodRange();
  const body = {
    login, categories: cats, creators,
    words: $("words").value, min_views: parseInt($("minViews").value, 10) || 0,
    pages: 10, deep: $("deep").getAttribute("aria-checked") === "true",
    start: start ? start.toISOString() : null, end: end ? end.toISOString() : null,
  };
  api("/api/settings", { last: { login, cats: S.cats.slice(), creators, words: $("words").value, period: serPeriod() } });
  setBusy(true); S.selected.clear(); S.rows = []; S.multi = logins.length > 1; paintSel(); stopPreview();
  S.lastSearch = { logins, cats: S.cats.slice() };
  $("resHead").hidden = false; moveGlide($("sorts")); $("resTitle").textContent = "Ищу…"; $("resStatus").textContent = "Подключаюсь к Twitch";
  $("more").hidden = true; showSearching(); const t0 = Date.now();
  drawChart();
  const r = await api("/api/search", body);
  if (r.error) { fail(r.error); return; }
  S.job = r.job;
  for (;;) {
    const j = await api("/api/job?id=" + r.job);
    if (j.state === "running") { updSearching(j.progress, t0); await sleep(350); continue; }
    setBusy(false);
    if (j.state === "error") { fail(j.error); return; }
    if (j.state === "cancelled") { $("resTitle").textContent = "Остановлено"; $("resStatus").textContent = ""; $("list").innerHTML = ""; drawChart(); return; }
    done(j.result); return;
  }
}
const LENS = '<svg class="lens" viewBox="0 0 76 76" fill="none"><circle cx="31" cy="31" r="21" fill="rgba(95,123,255,.16)" stroke="#9db0ff" stroke-width="5"/><path d="M18 24a16 16 0 0 1 12-9" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".7"/><path d="m47 47 22 22" stroke="#7a8cff" stroke-width="8" stroke-linecap="round"/></svg>';
function showSearching() {
  $("resStatus").textContent = "";
  $("list").innerHTML = '<div class="searching"><div class="lens-scene"><div class="tile"><i></i></div><div class="tile"><i></i></div><div class="tile"><i></i></div>' + LENS + '</div>' +
    '<h3>Ищем клипы<span class="dots"><span>.</span><span>.</span><span>.</span></span></h3><div class="numbers" id="sNum">Подключаюсь к Twitch</div><div class="eta" id="sEta"></div><div class="hatch"><i id="sBar" style="width:4%"></i></div></div>';
}
function fmtDur(sec) {
  sec = Math.max(1, Math.round(sec));
  if (sec < 60) return sec + " с";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + " мин" + (s ? " " + s + " с" : "");
}
function updSearching(p, t0) {
  if (!$("sNum")) return;
  if (!p || typeof p !== "object") { $("sNum").textContent = p || "Подключаюсь к Twitch"; return; }
  $("sNum").textContent = `Просмотрено клипов: ${p.scanned.toLocaleString("ru-RU")} · найдено: ${p.found.toLocaleString("ru-RU")}`;
  const frac = Math.min(0.99, p.frac || 0), el = (Date.now() - t0) / 1000;
  $("sBar").style.width = Math.max(4, Math.round(frac * 100)) + "%";
  $("sEta").textContent = frac >= 0.06 && el >= 2 ? "Осталось примерно: " + fmtDur(el * (1 - frac) / frac) + (p.steps > 1 ? ` (период ${p.step} из ${p.steps})` : "") : "Считаю, сколько осталось…";
  if (p.tn > 1) $("sNum").textContent += ` · стример ${p.ti} из ${p.tn}: ${p.who}`;
}
function fail(msg) { if (/Нет связи|Сервер ClipSearch/.test(String(msg))) checkLink(); setBusy(false); showAlert(msg); $("resHead").hidden = true; $("list").innerHTML = ""; drawChart(); }
function done(res) {
  S.rows = res.rows.map((r) => ({ ...r, ts: new Date(r.created) }));
  const ls = S.lastSearch || { logins: [] };
  const known = {}; (ls.cats || []).forEach((c) => { if (c.art) known[c.name.toLowerCase()] = c.art; });
  const rcats = (res.categories || []).map((c) => ({ id: c.id, name: c.name, art: c.art || known[String(c.name).toLowerCase()] || "" }));
  api("/api/history", { logins: ls.logins, categories: rcats })
    .then((r) => { if (r && r.history) S.history = r.history; }).catch(() => {});
  if (rcats.length) { setCats(rcats); paintedCats = new Set(rcats.map((c) => c.name)); paintCats(); }
  const n = S.rows.length;
  $("resTitle").textContent = n ? `Найдено ${n.toLocaleString("ru-RU")} ${plural(n, ["клип", "клипа", "клипов"])}` : "Ничего не найдено";
  let st = `${periodText()}. Просмотрено клипов: ${res.info.scanned.toLocaleString("ru-RU")}.`;
  if (res.info.truncated) st += " Часть клипов не просмотрена: сузьте период или включите глубокий поиск.";
  $("resStatus").textContent = st;
  sortRows(); S.shown = 0; $("list").innerHTML = ""; renderMore(); drawChart(); moveGlide($("sorts"));
  if (!n) $("list").innerHTML = `<div class="empty"><div class="big"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8.5 11h5"/></svg></div><h3>Ничего не нашлось</h3><p>Расширьте период, уберите часть слов или включите глубокий поиск.</p></div>`;
}
$("go").addEventListener("click", startSearch);
$("reset").addEventListener("click", () => {
  if ($("go").disabled) return;
  ["login", "cat", "words", "creator", "minViews"].forEach((id) => { $(id).value = ""; $(id).dispatchEvent(new Event("input")); });
  $("sug").hidden = true; setLogins(""); setCats([]); setCreators([]);
  $("deep").setAttribute("aria-checked", "false");
  S.period = { kind: "preset", key: "7d" }; paintPeriod(); closePop(); showAlert("");
  api("/api/settings", { last: { login: "", cats: [], creators: [], words: "", period: serPeriod() } });
  $("login").focus();
});
$("stop").addEventListener("click", () => { if (S.job) api("/api/cancel", { id: S.job }); $("resStatus").textContent = "Останавливаю…"; });

function serPeriod() { const p = S.period; return p.kind === "range" ? { kind: "range", from: p.from.getTime(), to: p.to.getTime() } : p; }
function loadPeriod(p) {
  if (!p) return;
  if (p.kind === "range") S.period = { kind: "range", from: new Date(p.from), to: new Date(p.to) };
  else if (p.kind === "preset" && (p.key === "all" || PRESETS[p.key])) S.period = p;
  else if (p.kind === "last") S.period = p;
}

function sortRows() {
  const { key, desc } = S.sort, k = { views: (r) => r.views, date: (r) => r.ts, dur: (r) => r.duration, title: (r) => r.title.toLowerCase() }[key];
  S.rows.sort((a, b) => (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0) * (desc ? -1 : 1));
}
document.querySelectorAll("#sorts .chip").forEach((b) => b.addEventListener("click", () => {
  if ($("sorts").classList.contains("locked")) {
    b.classList.remove("shake"); void b.offsetWidth; b.classList.add("shake");
    b.addEventListener("animationend", () => b.classList.remove("shake"), { once: true });
    toast("Дождитесь окончания поиска"); return;
  }
  const key = b.dataset.s;
  S.sort = { key, desc: S.sort.key === key ? !S.sort.desc : key !== "title" };
  document.querySelectorAll("#sorts .chip").forEach((x) => { x.classList.toggle("on", x === b); x.textContent = x.textContent.replace(/ [↑↓]$/, ""); });
  b.textContent += S.sort.desc ? " ↓" : " ↑";
  moveGlide($("sorts"));
  const before = new Map();
  document.querySelectorAll("#list .clip").forEach((el) => before.set(el.dataset.id, el.getBoundingClientRect().top));
  sortRows(); S.shown = 0; $("list").innerHTML = ""; renderMore(false);
  const vh = innerHeight;
  document.querySelectorAll("#list .clip").forEach((el, i) => {
    const top = el.getBoundingClientRect().top; if (top > vh + 200) return;
    const t0 = before.get(el.dataset.id), dy = t0 == null ? null : t0 - top;
    if (dy == null || Math.abs(dy) > vh * 1.5) { el.style.setProperty("--d", Math.min(i, 10) * 0.03 + "s"); el.classList.add("enter"); return; }
    if (!dy) return;
    el.style.transition = "none"; el.style.transform = `translateY(${dy}px)`;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transition = "transform .6s cubic-bezier(.22,1,.36,1)"; el.style.transform = "";
      el.addEventListener("transitionend", () => { el.style.transition = ""; }, { once: true });
    }));
  });
}));
function ago(d) {
  const m = Math.max(0, Math.round((Date.now() - d) / 60000));
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60); if (h < 48) return `${h} ч назад`;
  const dd = Math.round(h / 24); if (dd < 60) return `${dd} дн назад`;
  return `${Math.round(dd / 30)} мес назад`;
}
const mmss = (s) => { s = Math.round(s); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const IC = {
  user: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  game: '<svg class="i" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 10v5M4.5 12.5h5"/></svg>',
  clock: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  eye: '<svg class="i" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
  play: '<svg class="i" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  copy: '<svg class="i" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="3"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>',
  tv: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="3"/><path d="m9 3 3 3 3-3"/></svg>',
  ok: '<svg class="i" viewBox="0 0 24 24" style="stroke-width:3"><path d="m5 12 5 5 9-10"/></svg>',
};
function clipEl(r, i) {
  const el = document.createElement("div"); el.className = "clip" + (S.selected.has(r.id) ? " sel" : ""); el.dataset.id = r.id; el.dataset.i = i;
  const thumb = (r.thumb ? `<img src="${esc(r.thumb)}" alt="" loading="lazy">` : "") + `<button class="playov" data-a="watch" aria-label="Смотреть"><span>${IC.play}</span></button>`;
  el.innerHTML =
    `<button class="tick" aria-label="Выбрать клип">${IC.ok}</button>` +
    `<div class="thumb">${thumb}<span class="dur">${mmss(r.duration)}</span></div>` +
    `<div><h4>${esc(r.title)}</h4><div class="meta">` +
    (S.multi && r.broadcaster ? `<span title="Стример">${IC.tv}${esc(r.broadcaster)}</span>` : "") +
    `<span title="Автор клипа">${IC.user}${esc(r.creator || "неизвестно")}</span>` + (r.game ? `<span>${IC.game}${esc(r.game)}</span>` : "") +
    `<span>${IC.clock}${r.ts.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })} <span class="rel">(${ago(r.ts)})</span></span>` +
    `<span>${IC.eye}${r.views.toLocaleString("ru-RU")}</span></div></div>` +
    `<div class="acts"><button data-a="watch" title="Смотреть и обрезать">${IC.play}</button><button data-a="copy" title="Скопировать ссылку">${IC.copy}</button><button data-a="dl" title="Скачать в ${S.quality || "1080"}p">${IC.dl}</button></div>`;
  return el;
}
function renderMore(anim) {
  const list = $("list"), start = S.shown, end = Math.min(S.rows.length, S.shown + 40);
  const frag = document.createDocumentFragment();
  for (let i = S.shown; i < end; i++) {
    const el = clipEl(S.rows[i], i);
    if (anim !== false) { el.classList.add("enter"); el.style.setProperty("--d", Math.min(i - start, 12) * 0.035 + "s"); el.addEventListener("animationend", () => el.classList.remove("enter"), { once: true }); }
    frag.appendChild(el);
  }
  list.appendChild(frag); S.shown = end; $("more").hidden = S.shown >= S.rows.length;
  $("more").textContent = `Показать ещё (${S.rows.length - S.shown})`;
}
$("more").addEventListener("click", () => renderMore());
$("scroller").addEventListener("scroll", () => { const s = $("scroller"); if (!$("more").hidden && s.scrollHeight - s.scrollTop - s.clientHeight < 400) renderMore(); });
$("list").addEventListener("click", (e) => {
  const el = e.target.closest(".clip"); if (!el) return;
  const r = S.rows[+el.dataset.i], a = e.target.closest("[data-a]");
  if (a) {
    e.stopPropagation();
    if (a.dataset.a === "watch") openPlayer(r);
    if (a.dataset.a === "copy") copyText(r.url, "Ссылка скопирована");
    if (a.dataset.a === "dl") startDownload([r]);
    return;
  }
  toggleSel(+el.dataset.i); setKFocus(+el.dataset.i, false);
});
$("list").addEventListener("dblclick", (e) => { const el = e.target.closest(".clip"); if (el && !e.target.closest("[data-a]")) openPlayer(S.rows[+el.dataset.i]); });
function selRows() { return S.rows.filter((r) => S.selected.has(r.id)); }
function paintSel() {
  const n = S.selected.size, num = $("selNum");
  if (num.textContent !== String(n)) { num.textContent = n; num.classList.remove("bump"); void num.offsetWidth; num.classList.add("bump"); }
  $("selCard").classList.toggle("has", n > 0);
  $("selSub").textContent = n ? `${plural(n, ["клип выбран", "клипа выбрано", "клипов выбрано"])}` : "Нажмите на карточки, чтобы выбрать";
  ["bDl", "bCopy"].forEach((id) => ($(id).disabled = !n));
  $("bCsv").disabled = !S.rows.length;
  $("bAll").lastChild.textContent = n && n === S.rows.length ? "Снять выбор" : "Выбрать все найденные";
}
$("bAll").addEventListener("click", () => {
  if (!S.rows.length) return;
  if (S.selected.size === S.rows.length) S.selected.clear(); else S.rows.forEach((r) => S.selected.add(r.id));
  document.querySelectorAll(".clip").forEach((el) => el.classList.toggle("sel", S.selected.has(el.dataset.id))); paintSel();
});
async function copyText(t, msg) {
  try { await navigator.clipboard.writeText(t); toast(msg); }
  catch (e) { const ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); toast(msg); } catch (_) { toast("Не удалось скопировать"); } ta.remove(); }
}
$("bCopy").addEventListener("click", () => { const rows = selRows(); copyText(rows.map((r) => r.url).join("\n"), `Скопировано ссылок: ${rows.length}`); });
$("bCsv").addEventListener("click", async () => {
  const r = await api("/api/csv", { rows: S.rows });
  if (r.error) { showAlert(r.error); return; }
  toast("Список сохранён в папку загрузок"); api("/api/reveal", { path: r.path });
});

async function startDownload(rows, quality) {
  if (!rows.length) return;
  const r = await api("/api/download", { quality: quality || S.quality || "1080", items: rows.map((x) => ({ id: x.id, title: x.title, broadcaster: x.broadcaster, start: x.start ?? null, end: x.end ?? null })) });
  if (r.error) { showAlert(r.error); return; }
  if (r.keys && r.keys.length) S.dlFresh = r.keys[r.keys.length - 1];
  const busy = [...S.downloads.values()].some((x) => x.state === "wait" || x.state === "load");
  toast(busy ? `Добавлено в очередь: ${rows.length}` : (rows.length > 1 ? `Скачиваю клипов: ${rows.length}` : "Скачиваю клип"));
  pollDownloads();
}
let dlPolling = false;
function applyDownloads(items) {
  const before = new Map(S.downloads);
  S.downloads = new Map(items.map((it) => [it.key, it]));
  let finished = 0;
  items.forEach((it) => { const o = before.get(it.key); if (it.state === "done" && o && o.state !== "done") finished++; });
  paintDl();
  if (finished) loadFolder();
  return finished;
}
async function pollDownloads() {
  if (dlPolling) return; dlPolling = true;
  try {
    for (;;) {
      const r = await api("/api/downloads", {}).catch(() => null);
      if (!r || !r.items) { await sleep(1000); continue; }
      applyDownloads(r.items);
      const active = r.items.some((x) => x.state === "wait" || x.state === "load");
      if (!active) {
        const done = r.items.filter((x) => x.state === "done").length, fail = r.items.filter((x) => x.state === "fail").length;
        if (S.dlNotify !== r.items.length + ":" + done + ":" + fail) { S.dlNotify = r.items.length + ":" + done + ":" + fail; if (fail) toast(`Не удалось скачать: ${fail}`); else if (done) toast("Загрузки завершены"); }
        break;
      }
      await sleep(450);
    }
  } finally { dlPolling = false; }
}
async function dlAction(kind, key) {
  const r = await api("/api/download/" + kind, { key }).catch(() => null);
  if (r && r.items) applyDownloads(r.items);
  if (kind === "retry") { toast("Пробую ещё раз"); pollDownloads(); }
  if (kind === "cancel") toast("Загрузка отменена");
}
$("bDl").addEventListener("click", () => startDownload(selRows()));
const DL_LABEL = { wait: "в очереди", done: "готово", fail: "ошибка", stop: "отменено" };
const DIC = {
  x: '<svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg>',
  retry: '<svg class="i" viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
  play: '<svg class="i" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  dir: '<svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
};
function dlActs(it) {
  if (it.state === "wait" || it.state === "load") return `<button data-dl="cancel" class="dx" title="Отменить загрузку">${DIC.x}</button>`;
  if (it.state === "done") return `<button data-dl="open" title="Открыть файл">${DIC.play}</button><button data-dl="show" title="Показать в папке">${DIC.dir}</button>`;
  return `<button data-dl="retry" class="dr" title="Повторить">${DIC.retry}</button>`;
}
function dlFill(node, it) {
  const pct = it.state === "done" ? 100 : (it.pct || 0);
  node.dataset.state = it.state;
  node.querySelector(".t .nm").textContent = it.title || it.id;
  node.querySelector(".t .nm").title = it.title || "";
  node.querySelector(".t .st").textContent = it.state === "load" ? pct + "%" : (DL_LABEL[it.state] || "");
  const acts = node.querySelector(".dacts");
  if (acts.dataset.s !== it.state) { acts.dataset.s = it.state; acts.innerHTML = dlActs(it); acts.classList.remove("swap"); void acts.offsetWidth; acts.classList.add("swap"); }
  const h = node.querySelector(".hatch");
  h.classList.toggle("done", it.state === "done"); h.classList.toggle("run", it.state === "load" || it.state === "wait");
  h.classList.toggle("stopped", it.state === "stop" || it.state === "fail");
  h.querySelector("i").style.width = (it.state === "stop" ? 0 : pct) + "%";
  const er = node.querySelector(".err"); er.textContent = it.error || ""; er.hidden = !it.error || it.state !== "fail";
}
function dlList(box, items, emptyHtml) {
  if (!items.length) { if (!box.querySelector(".dlempty")) box.innerHTML = `<div class="dlempty">${emptyHtml}</div>`; return; }
  box.querySelector(".dlempty")?.remove();
  const keep = new Set(items.map((it) => it.key));
  box.querySelectorAll(".dl").forEach((n) => { if (!keep.has(n.dataset.k)) { n.classList.add("gone"); setTimeout(() => n.remove(), 260); n.dataset.k = ""; } });
  items.forEach((it) => {
    let n = [...box.children].find((x) => x.dataset.k === it.key);
    if (!n) {
      n = document.createElement("div"); n.className = "dl enter"; n.dataset.k = it.key;
      n.innerHTML = '<div class="t"><span class="nm"></span><span class="st"></span><span class="dacts"></span></div><div class="hatch"><i style="width:0"></i></div><div class="err" hidden></div>';
      n.addEventListener("animationend", () => n.classList.remove("enter"), { once: true });
    }
    dlFill(n, it); box.appendChild(n);
  });
}
["railDl", "dlAll"].forEach((id) => $(id).addEventListener("click", (e) => {
  const b = e.target.closest("[data-dl]"); if (!b) return;
  const key = b.closest(".dl").dataset.k, it = S.downloads.get(key); if (!it) return;
  const a = b.dataset.dl;
  if (a === "cancel" || a === "retry") dlAction(a, key);
  else if (a === "open") api("/api/openfile", { path: it.path }).then((r) => r && r.error && toast(r.error));
  else if (a === "show") api("/api/reveal", { path: it.path });
}));
const P_HINT = "Тяните ползунки или ставьте метки по ходу видео. Пробел — пауза.";
function paintPlayerDl() {
  if (!P.row) return;
  const it = [...S.downloads.values()].reverse().find((x) => x.id === P.row.id && x.state !== "done" && x.state !== "fail" && x.state !== "stop");
  const last = [...S.downloads.values()].reverse().find((x) => x.id === P.row.id);
  if (it) $("pHint").textContent = (it.key && it.key.includes("@") ? "Скачиваю и обрезаю фрагмент… " : "Скачиваю клип… ") + (it.state === "wait" ? "в очереди" : (it.pct || 0) + "%");
  else if (last && last.state === "done" && S.dlFresh === last.key) $("pHint").textContent = "Готово: файл в папке загрузок.";
  else if (last && last.state === "fail" && S.dlFresh === last.key) $("pHint").textContent = "Ошибка: " + (last.error || "не удалось скачать");
  else $("pHint").textContent = P_HINT;
}
function paintDl() {
  try { paintPlayerDl(); } catch (e) {}
  const items = [...S.downloads.values()].reverse();
  const active = items.filter((x) => x.state === "wait" || x.state === "load").length;
  $("dlCount").hidden = !active; $("dlCount").textContent = active;
  $("dlClear").hidden = !items.some((x) => x.state === "done" || x.state === "fail" || x.state === "stop");
  $("dlSum").textContent = items.length ? `всего ${items.length}` : "";
  $("dlSum2").textContent = items.length ? `всего ${items.length}` : "";
  dlList($("railDl"), items.slice(0, 4), '<div class="note">Скачанные клипы появятся здесь.</div>');
  dlList($("dlAll"), items, '<div class="qempty"><div class="big"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg></div><p>Пока ничего не скачано. Выберите клипы на вкладке «Поиск» и нажмите «Скачать в 1080p» или откройте клип и скачайте фрагмент.</p><button class="btn slim" type="button" data-goto="search">Перейти к поиску</button></div>');
}
function shortPath(p) { return p.length > 30 ? "…" + p.slice(-29) : p; }
function paintDir() { $("dirText").textContent = shortPath(S.dir); $("dirText2").textContent = S.dir; $("dirText").title = S.dir; }
async function pickDir() { const r = window.cs ? { path: await window.cs.pickFolder(S.dir) } : await api("/api/pickfolder", {}); if (r.path) { const s = await api("/api/settings", { download_dir: r.path }); S.dir = s.downloadDir; paintDir(); loadFolder(); toast("Папка загрузок изменена"); } else toast("Папка не выбрана"); }
function fmtBytes(b) {
  if (b == null) return "—";
  const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"]; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i >= 2 ? b.toFixed(b < 10 ? 1 : 0) : Math.round(b)).toString().replace(".", ",") + " " + u[i];
}
let folderBusy = false;
async function loadFolder() {
  if (folderBusy) return; folderBusy = true;
  try {
    const f = await api("/api/folder", {});
    if (f.error) return;
    $("stCount").textContent = f.count.toLocaleString("ru-RU");
    $("stSize").textContent = fmtBytes(f.size);
    $("stFree").textContent = fmtBytes(f.free);
    if (f.disk) {
      const used = (f.disk - f.free) / f.disk;
      $("stDisk").style.width = Math.round(used * 100) + "%";
      $("stDiskTxt").textContent = `Диск занят на ${Math.round(used * 100)}%: ${fmtBytes(f.disk - f.free)} из ${fmtBytes(f.disk)}`;
    } else $("stDiskTxt").textContent = "";
    const box = $("recent");
    if (!f.exists) { box.innerHTML = '<div class="note">Папки ещё нет: она появится после первой загрузки.</div>'; return; }
    if (!f.recent.length) { box.innerHTML = '<div class="note">В папке пока нет клипов.</div>'; return; }
    const sig = f.recent.map((r) => r.path + r.mtime).join("|");
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.innerHTML = f.recent.map((r, i) => `<div class="rf enter" style="--d:${i * 0.04}s"><span class="fi"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9 5 3-5 3z"/></svg></span>` +
      `<div style="min-width:0"><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div><div class="sb">${fmtBytes(r.size)} · ${new Date(r.mtime * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</div></div>` +
      `<div class="acts"><button data-open="${i}" title="Открыть">${IC.play}</button><button data-show="${i}" title="Показать в папке"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></button></div></div>`).join("");
    box._rows = f.recent;
  } finally { folderBusy = false; }
}
$("recent").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-open],[data-show]"); if (!b) return; const rows = $("recent")._rows || [];
  if (b.dataset.open != null) { const r = await api("/api/openfile", { path: rows[+b.dataset.open].path }); if (r.error) { toast(r.error); loadFolder(); } }
  else api("/api/reveal", { path: rows[+b.dataset.show].path });
});
$("recentRefresh").addEventListener("click", () => { $("recent").dataset.sig = ""; loadFolder(); });
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) document.querySelector(`.nav [data-view="${g.dataset.goto}"]`).click(); });
["dirPick", "dirPick2"].forEach((id) => $(id).addEventListener("click", pickDir));
["dirOpen", "dirOpen2"].forEach((id) => $(id).addEventListener("click", () => api("/api/reveal", { path: S.dir })));

const CH_DEFS = '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#3a56d6"/><rect width="3" height="6" fill="#8ea4ff" opacity=".75"/></pattern></defs>';
const CH = { W: 300, H: 130, base: 112, n: 0, cnt: [], lo: 0, span: 1 };
function restartNote(text) { const n = $("chartNote"); n.textContent = text; n.classList.remove("fresh"); void n.offsetWidth; n.classList.add("fresh"); }
function chartIdle() {
  const svg = $("chart"), busy = $("go").disabled, H = [0.35, 0.62, 0.46, 0.82, 0.55, 0.96, 0.7, 0.5, 0.86, 0.4, 0.66, 0.9, 0.52, 0.3];
  const bw = CH.W / H.length;
  svg.classList.add("idle"); svg.classList.toggle("busy", busy); hideTip(); CH.n = 0;
  svg.innerHTML = CH_DEFS + H.map((h, i) => `<rect class="bar ghost" style="--i:${i}" x="${i * bw + 3}" y="${CH.base - h * 86}" width="${bw - 6}" height="${h * 86}" rx="3"/>`).join("") +
    `<line x1="0" x2="${CH.W}" y1="${CH.base + 0.5}" y2="${CH.base + 0.5}" class="axis"/>`;
  if ($("chartNote").textContent !== "После поиска здесь появится график.") restartNote("После поиска здесь появится график.");
}
function chartFmt(t, withTime) {
  const d = new Date(t);
  return withTime ? d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
                  : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
}
function drawChart() {
  const svg = $("chart"), rows = S.rows;
  if (!rows.length) { chartIdle(); return; }
  svg.classList.remove("idle", "busy");
  const ts = rows.map((r) => +r.ts), lo = Math.min(...ts), hi = Math.max(...ts), N = Math.min(14, Math.max(2, rows.length));
  const span = Math.max(hi - lo, 1), cnt = new Array(N).fill(0);
  ts.forEach((t) => cnt[Math.min(N - 1, Math.floor(((t - lo) / span) * N))]++);
  Object.assign(CH, { n: N, cnt, lo, span });
  const max = Math.max(...cnt), { W, H, base } = CH, bw = W / N, top = cnt.indexOf(max);
  let out = CH_DEFS;
  cnt.forEach((c, i) => {
    const h = Math.max(c ? 4 : 0, (c / max) * (base - 26)), x = i * bw + 3;
    out += `<rect class="bar real ${i === top ? "top" : ""}" data-i="${i}" style="--i:${i}" x="${x}" y="${base - h}" width="${bw - 6}" height="${h}" rx="3"/>`;
  });
  cnt.forEach((c, i) => { out += `<rect class="hit" data-i="${i}" x="${i * bw}" y="0" width="${bw}" height="${base + 4}"/>`; });
  const tx = top * bw + bw / 2;
  out += `<g class="badge"><rect x="${Math.min(Math.max(tx - 22, 0), W - 44)}" y="2" width="44" height="18" rx="9" fill="#4262f0"/><text x="${Math.min(Math.max(tx, 22), W - 22)}" y="14.5" text-anchor="middle" style="fill:#fff">${max}</text></g>`;
  const withTime = span < 2 * 86400000;
  out += `<g class="axl"><text x="0" y="${H - 3}">${chartFmt(lo, withTime)}</text><text x="${W}" y="${H - 3}" text-anchor="end">${chartFmt(hi, withTime)}</text></g>`;
  svg.innerHTML = out; hideTip();
  restartNote(`Больше всего клипов (${max}) пришлось на период около ${chartFmt(lo + span * ((top + 0.5) / N), withTime)}. Наведите на столбик, чтобы увидеть число.`);
}
function hideTip() { $("chartTip").classList.remove("on"); document.querySelectorAll("#chart .bar.hov").forEach((b) => b.classList.remove("hov")); }
$("chart").addEventListener("mousemove", (e) => {
  const hit = e.target.closest(".hit"); if (!hit || !CH.n) { hideTip(); return; }
  const i = +hit.dataset.i, bar = $("chart").querySelector(`.bar.real[data-i="${i}"]`);
  document.querySelectorAll("#chart .bar.hov").forEach((b) => b !== bar && b.classList.remove("hov"));
  bar.classList.add("hov");
  const withTime = CH.span < 2 * 86400000, a = CH.lo + CH.span * i / CH.n, z = CH.lo + CH.span * (i + 1) / CH.n, c = CH.cnt[i];
  const tip = $("chartTip");
  tip.innerHTML = `<b>${c.toLocaleString("ru-RU")} ${plural(c, ["клип", "клипа", "клипов"])}</b><span>${chartFmt(a, withTime)} – ${chartFmt(z, withTime)}</span>`;
  const card = tip.parentElement.getBoundingClientRect(), br = bar.getBoundingClientRect(), hr = hit.getBoundingClientRect();
  const top = (c ? br.top : hr.bottom - 8) - card.top - 10;
  let left = hr.left + hr.width / 2 - card.left;
  tip.classList.add("on");
  const tw = tip.offsetWidth; left = Math.max(tw / 2 + 6, Math.min(card.width - tw / 2 - 6, left));
  tip.style.left = left + "px"; tip.style.top = top + "px";
});
$("chart").addEventListener("mouseleave", hideTip);

let linkBusy = false;
async function checkLink() {
  if (linkBusy) return; linkBusy = true;
  if (S.link !== true) { S.link = null; paintLink(); }
  const r = await api("/api/link", {});
  linkBusy = false;
  S.link = !!r.ok; S.linkErr = r.error || ""; paintLink();
}
function paintLink() {
  const p = $("keyPill");
  p.className = "pill " + (S.link === true ? "ok" : S.link === false ? "bad" : "");
  p.title = S.link === false ? S.linkErr : "";
  $("keyText").innerHTML = S.link === true ? "Twitch подключён" : S.link === false ? 'Нет связи с Twitch · <button>проверить</button>' : "Проверяю связь…";
}
$("keyPill").addEventListener("click", () => { if (S.link === false) checkLink(); });
document.querySelectorAll(".nav [data-view]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".nav [data-view]").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === "v-" + b.dataset.view));
  $("scroller").scrollTop = 0; reglide();
  if (b.dataset.view === "downloads") loadFolder();
}));
if (window.cs && window.cs.drag) {
  document.body.classList.add("jsdrag");
  [$("dragbar"), document.querySelector(".brand")].forEach((z) => {
    let down = null, moving = false;
    const end = () => { if (moving) window.cs.drag("end"); down = null; moving = false; document.body.classList.remove("dragging"); };
    z.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      down = { x: e.screenX, y: e.screenY, ox: e.clientX, oy: e.clientY }; z.setPointerCapture(e.pointerId);
    });
    z.addEventListener("pointermove", (e) => {
      if (!down || moving) return;
      if (Math.hypot(e.screenX - down.x, e.screenY - down.y) < 4) return;
      moving = true; document.body.classList.add("dragging"); window.cs.drag("start", { ox: down.ox, oy: down.oy });
    });
    z.addEventListener("pointerup", end); z.addEventListener("pointercancel", end); z.addEventListener("lostpointercapture", end);
    z.addEventListener("dblclick", () => window.cs.win("max"));
  });
}
if (window.cs) {
  document.body.classList.add("has-wc"); $("wc").hidden = false;
  $("wc").addEventListener("click", (e) => { const b = e.target.closest("[data-w]"); if (b) window.cs.win(b.dataset.w); });
}
$("navQuit").addEventListener("click", async () => {
  if (window.cs) { window.cs.win("close"); return; }
  await api("/api/quit", {});
  document.body.innerHTML = '<div style="height:100vh;display:grid;place-items:center;color:#95a0d0;font:16px system-ui">Программа закрыта. Это окно можно закрыть.</div>';
  setTimeout(() => window.close(), 300);
});

S.history = {};
const XSVG = '<svg class="i" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg>';
const CLOCKSVG = '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
function freqList(kind, prefix, exclude) {
  const d = (S.history && S.history[kind]) || {}, now = Date.now() / 1000;
  return Object.entries(d).filter(([k, v]) => v.n >= 3 && !exclude.includes(k) && (!prefix || k.toLowerCase().startsWith(prefix)))
    .map(([k, v]) => ({ name: k, n: v.n, id: v.id, art: v.art, score: v.n * (0.5 + 0.5 * Math.exp(-(now - v.t) / 2592000)) }))
    .sort((a, b) => b.score - a.score).slice(0, 8);
}
function freqHtml(items, kind) {
  return `<div class="fh">${CLOCKSVG}Часто ищете</div><div class="fl">` + items.map((it, k) =>
    `<button type="button" class="fq" style="--k:${k}" data-k="${esc(it.name)}">${kind === "cats" && it.art ? `<img src="${esc(it.art)}" alt="">` : ""}${esc(it.name)}<span class="fx" data-x="1" title="Убрать из «Часто ищете»">${XSVG}</span></button>`).join("") + "</div>";
}
function paintFreqL() {
  const box = $("freqL"), focused = document.activeElement === $("login");
  const items = focused && S.logins.length < 10 ? freqList("logins", cleanNick($("login").value), S.logins) : [];
  if (!items.length) { box.hidden = true; return; }
  const sig = items.map((i) => i.name + i.n).join("|");
  if (box.hidden || box.dataset.sig !== sig) { box.innerHTML = freqHtml(items, "logins"); box.dataset.sig = sig; }
  box.hidden = false;
}
function paintFreqC() {
  const box = $("freqC"), focused = document.activeElement === $("cat");
  const items = focused && !$("cat").value.trim() && S.cats.length < MAXC ? freqList("cats", "", S.cats.map((c) => c.name)) : [];
  if (!items.length) { box.hidden = true; return; }
  const sig = items.map((i) => i.name + i.n).join("|");
  if (box.hidden || box.dataset.sig !== sig) { box.innerHTML = freqHtml(items, "cats"); box.dataset.sig = sig; }
  box.hidden = false; $("sug").hidden = true;
}
["freqL", "freqC"].forEach((id) => $(id).addEventListener("mousedown", (e) => e.preventDefault()));
$("freqL").addEventListener("click", (e) => {
  e.preventDefault(); e.stopPropagation();
  const b = e.target.closest(".fq"); if (!b) return; const name = b.dataset.k;
  if (e.target.closest("[data-x]")) { forget("login", name); return; }
  addLogins(name); $("login").value = ""; paintChips(); paintFreqL();
});
$("freqC").addEventListener("click", (e) => {
  e.preventDefault(); e.stopPropagation();
  const b = e.target.closest(".fq"); if (!b) return; const name = b.dataset.k;
  if (e.target.closest("[data-x]")) { forget("cat", name); return; }
  const it = (S.history.cats || {})[name] || {}; pickCat({ id: it.id || "", name, art: it.art || "" });
});
async function forget(kind, name) {
  const r = await api("/api/history", { forget: { kind, name } }).catch(() => null);
  if (r && r.history) S.history = r.history;
  if (kind === "login") paintFreqL(); else paintFreqC();
  toast("Убрано из «Часто ищете»");
}
$("login").addEventListener("focus", paintFreqL);
$("login").addEventListener("input", paintFreqL);
$("login").addEventListener("blur", () => setTimeout(paintFreqL, 120));
$("cat").addEventListener("focus", paintFreqC);
$("cat").addEventListener("blur", () => setTimeout(paintFreqC, 120));
addEventListener("keydown", (e) => { if (e.key === "Escape") { $("freqL").hidden = true; $("freqC").hidden = true; } });

let pvTimer = 0, pvThumb = null, pvVideo = null;
const pvCache = new Map();
function startPreview(th) {
  if (pvThumb === th) return;
  stopPreview(); pvThumb = th; clearTimeout(pvTimer);
  pvTimer = setTimeout(async () => {
    const el = th.closest(".clip"), r = el && S.rows[+el.dataset.i];
    if (!r || pvThumb !== th) return;
    th.classList.add("pvload");
    let url = pvCache.get(r.id);
    if (!url) {
      const res = await api("/api/clipurl", { id: r.id, preview: true }).catch(() => ({}));
      if (!res || res.error || !res.url) { th.classList.remove("pvload"); return; }
      url = res.url; pvCache.set(r.id, url);
    }
    if (pvThumb !== th) return;
    const v = document.createElement("video"), bar = document.createElement("i");
    v.className = "pv"; v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto"; v.src = url;
    bar.className = "pvbar";
    const glare = document.createElement("i"); glare.className = "glare";
    th.insertBefore(v, th.querySelector(".playov")); th.insertBefore(glare, th.querySelector(".playov")); th.appendChild(bar);
    v.addEventListener("playing", () => { th.classList.remove("pvload"); th.classList.add("pvon"); }, { once: true });
    v.addEventListener("timeupdate", () => { if (v.duration) bar.style.transform = `scaleX(${v.currentTime / v.duration})`; });
    v.addEventListener("error", () => th.classList.remove("pvload"));
    v.play().catch(() => {});
    pvVideo = v;
  }, 380);
}
function stopPreview() {
  clearTimeout(pvTimer);
  const th = pvThumb, v = pvVideo; pvThumb = null; pvVideo = null;
  if (!th) return;
  th.classList.remove("pvon", "pvload", "tilting");
  ["--rx", "--ry", "--sx", "--sy"].forEach((k) => th.style.removeProperty(k));
  const bar = th.querySelector(".pvbar"), glare = th.querySelector(".glare");
  setTimeout(() => { if (v) { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); } if (bar) bar.remove(); if (glare) glare.remove(); }, 420);
}
$("list").addEventListener("mouseover", (e) => { const th = e.target.closest(".thumb"); if (th) startPreview(th); });

$("list").addEventListener("mousemove", (e) => {
  const th = e.target.closest(".thumb");
  if (!th || th !== pvThumb || !th.classList.contains("pvon")) return;
  const r = th.getBoundingClientRect(), x = Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1)), y = Math.max(-1, Math.min(1, ((e.clientY - r.top) / r.height) * 2 - 1));
  th.style.setProperty("--ry", (x * 12).toFixed(2) + "deg"); th.style.setProperty("--rx", (-y * 10).toFixed(2) + "deg");
  th.style.setProperty("--sx", x.toFixed(3)); th.style.setProperty("--sy", y.toFixed(3));
  th.style.setProperty("--gx", ((x + 1) * 50).toFixed(1) + "%"); th.style.setProperty("--gy", ((y + 1) * 50).toFixed(1) + "%");
  if (!th.classList.contains("tilting")) setTimeout(() => th.classList.add("tilting"), 450);
});
$("list").addEventListener("mouseout", (e) => { const th = e.target.closest(".thumb"); if (th && !(e.relatedTarget && th.contains(e.relatedTarget))) stopPreview(); });
$("scroller").addEventListener("scroll", () => { if (pvThumb && !pvThumb.matches(":hover")) stopPreview(); }, { passive: true });

S.update = null;
function noteItems(text) {
  return String(text || "").split(/\r?\n/).map((l) => l.replace(/^\s*[-*•]\s*/, "").trim()).filter((l) => l && !/^#/.test(l)).slice(0, 8);
}
async function checkUpdate(manual) {
  const b = $("updCheck");
  if (manual) { b.textContent = "Проверяю…"; b.disabled = true; }
  let r;
  try { r = await api("/api/update/check", {}); } catch (e) { r = { error: "нет связи" }; }
  b.disabled = false; b.textContent = "Проверить обновления";
  if (r.error) { if (manual) toast(r.error); return; }
  if (!r.newer) { if (manual) toast(`У вас последняя версия ${r.current}`); return; }
  S.update = r;
  $("updPillText").textContent = `Доступна версия ${r.latest}`; $("updPill").hidden = false; b.hidden = true;
  if (manual || S.updateSeen !== r.latest) openUpdate();
}
function openUpdate() {
  const u = S.update; if (!u) return;
  $("uTitle").textContent = `Доступна новая версия ${u.latest}`;
  $("uCur").textContent = u.current; $("uNew").textContent = u.latest;
  const items = noteItems(u.notes);
  $("uNotes").innerHTML = (items.length ? items : ["Исправления и улучшения."]).map((t, k) => `<li style="--k:${k}">${esc(t)}</li>`).join("");
  $("uProg").hidden = true; $("uGo").disabled = false; $("uLater").disabled = false;
  showVeil($("updVeil"));
  if (S.updateSeen !== u.latest) { S.updateSeen = u.latest; api("/api/update/seen", { version: u.latest }).catch(() => {}); }
}
$("updCheck").addEventListener("click", () => checkUpdate(true));
$("updPill").addEventListener("click", openUpdate);
$("uLater").addEventListener("click", () => hideVeil($("updVeil")));
$("updVeil").addEventListener("mousedown", (e) => { if (e.target === $("updVeil") && !$("uGo").disabled) hideVeil($("updVeil")); });
$("uGo").addEventListener("click", async () => {
  $("uGo").disabled = true; $("uLater").disabled = true; $("uProg").hidden = false; $("uBar").style.width = "3%";
  $("uProgText").textContent = "Скачиваю установщик…";
  const r = await api("/api/update/download", {});
  if (r.error) { toast(r.error); $("uGo").disabled = false; $("uLater").disabled = false; $("uProg").hidden = true; return; }
  for (;;) {
    const j = await api("/api/job?id=" + r.job);
    const p = j.progress || {};
    if (p.total) { const pct = Math.round(p.done / p.total * 100); $("uBar").style.width = Math.max(3, pct) + "%"; $("uProgText").textContent = `Скачиваю установщик… ${pct}%`; }
    if (j.state === "running") { await sleep(250); continue; }
    if (j.state !== "done") { toast(j.error || "Не удалось скачать обновление"); $("uGo").disabled = false; $("uLater").disabled = false; $("uProg").hidden = true; return; }
    $("uBar").style.width = "100%"; $("uProgText").textContent = "Запускаю установку… Программа перезапустится сама.";
    await sleep(700);
    if (window.cs && window.cs.runSetup) window.cs.runSetup(j.result.path);
    else { toast("Установщик скачан. Запустите его, чтобы обновиться."); api("/api/reveal", { path: j.result.path }); }
    return;
  }
});

if (window.cs && window.cs.resize) {
  document.body.classList.add("jsresize");
  document.querySelectorAll(".rsz").forEach((z) => z.addEventListener("pointerdown", (e) => { if (e.button === 0) { e.preventDefault(); window.cs.resize(z.dataset.edge); } }));
  const mx = () => document.body.classList.toggle("maxed", outerWidth >= screen.availWidth - 2 && outerHeight >= screen.availHeight - 2);
  addEventListener("resize", mx); mx();
}

const MI = {
  play: '<svg class="i" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  dl: '<svg class="i" viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>',
  check: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>',
  link: '<svg class="i" viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/></svg>',
  text: '<svg class="i" viewBox="0 0 24 24"><path d="M5 6h14M5 12h14M5 18h9"/></svg>',
  ext: '<svg class="i" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  tv: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="3"/><path d="m9 3 3 3 3-3"/></svg>',
  user: '<svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  keys: '<svg class="i" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="3"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
  q: '<svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15V9l2.5 3L12 9v6M15 9h2a2 2 0 0 1 0 6h-2z"/></svg>',
  ok: '<svg class="i chk" viewBox="0 0 24 24" style="stroke-width:3"><path d="m5 12 5 5 9-10"/></svg>',
};
let menuEl = null, menuOnClose = null;
function closeMenu() {
  if (!menuEl) return;
  const m = menuEl; menuEl = null; m.classList.add("out"); setTimeout(() => m.remove(), 150);
  if (menuOnClose) { const f = menuOnClose; menuOnClose = null; f(); }
}
function openMenu(items, x, y, opts) {
  closeMenu();
  const m = document.createElement("div"); m.className = "cmenu"; m.setAttribute("role", "menu");
  let k = 0;
  items.forEach((it) => {
    if (!it) return;
    if (it.sep) { m.insertAdjacentHTML("beforeend", '<div class="cm-sep"></div>'); return; }
    if (it.head) { const h = document.createElement("div"); h.className = "cm-h"; h.textContent = it.head; m.appendChild(h); return; }
    if (it.chips) {
      const row = document.createElement("div"); row.className = "cm-q"; row.style.setProperty("--k", k++);
      row.innerHTML = `<span>${esc(it.label)}</span>` + it.chips.map((c, i) => `<button type="button" data-c="${i}">${esc(c.label)}</button>`).join("");
      row.addEventListener("click", (e) => { const b = e.target.closest("[data-c]"); if (b) { closeMenu(); it.chips[+b.dataset.c].run(); } });
      m.appendChild(row); return;
    }
    const b = document.createElement("button"); b.type = "button"; b.setAttribute("role", "menuitem");
    b.className = "cm-i" + (it.sub ? " tall" : "") + (it.checked ? " on" : ""); b.style.setProperty("--k", k++);
    b.innerHTML = (it.icon ? MI[it.icon] : "") + `<span class="lbl2">${esc(it.label)}${it.badge ? `<span class="rec">${esc(it.badge)}</span>` : ""}${it.sub ? `<span class="sub2">${esc(it.sub)}</span>` : ""}</span>` +
      (it.kbd ? `<kbd>${esc(it.kbd)}</kbd>` : "") + (it.checkable ? MI.ok : "");
    b.addEventListener("click", () => { closeMenu(); it.run(); });
    b.addEventListener("mouseenter", () => { m.querySelectorAll(".cm-i.act").forEach((x) => x.classList.remove("act")); b.classList.add("act"); });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  const w = m.offsetWidth, h = m.offsetHeight, W = innerWidth, H = innerHeight;
  let left = x, top = y;
  if (opts && opts.below) { left = x; top = y; }
  if (left + w > W - 8) left = Math.max(8, W - w - 8);
  if (top + h > H - 8) top = (opts && opts.anchorTop != null) ? Math.max(8, opts.anchorTop - h - 6) : Math.max(8, H - h - 8);
  m.style.left = left + "px"; m.style.top = top + "px";
  m.style.setProperty("--ox", (x - left) + "px"); m.style.setProperty("--oy", (y - top) + "px");
  m._t = performance.now(); menuEl = m; menuOnClose = (opts && opts.onClose) || null;
}
document.addEventListener("pointerdown", (e) => { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }, true);
addEventListener("blur", closeMenu); addEventListener("resize", closeMenu);
$("scroller").addEventListener("scroll", () => { if (menuEl && performance.now() - menuEl._t > 300) closeMenu(); }, { passive: true });
addEventListener("keydown", (e) => {
  if (!menuEl) return;
  const its = [...menuEl.querySelectorAll(".cm-i")], i = its.findIndex((x) => x.classList.contains("act"));
  if (e.key === "Escape") { closeMenu(); e.preventDefault(); e.stopImmediatePropagation(); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const n = e.key === "ArrowDown" ? (i + 1) % its.length : (i - 1 + its.length) % its.length;
    its.forEach((x, j) => x.classList.toggle("act", j === n)); e.preventDefault(); e.stopImmediatePropagation();
  } else if (e.key === "Enter" && i >= 0) { its[i].click(); e.preventDefault(); e.stopImmediatePropagation(); }
}, true);
addEventListener("contextmenu", (e) => { if (!e.target.closest("input, textarea")) e.preventDefault(); });

S.quality = "1080";
const QDESC = { "1080": ["1080p", "Наилучшее качество", "Рекомендуется"], "720": ["720p", "Файл примерно вдвое меньше"], "480": ["480p", "Минимальный размер — удобно переслать"] };
function paintQuality() {
  document.querySelectorAll(".qtext").forEach((t) => (t.textContent = `Качество: ${S.quality}p`));
  if ($("bDl")) $("bDl").lastChild.textContent = `Скачать в ${S.quality}p`;
  document.querySelectorAll('.acts [data-a="dl"]').forEach((b) => (b.title = `Скачать в ${S.quality}p`));
}
function setQuality(q) {
  if (S.quality === q) return;
  S.quality = q; paintQuality(); api("/api/settings", { quality: q }); toast(`Клипы будут скачиваться в ${q}p`);
}
document.querySelectorAll("[data-qbtn]").forEach((b) => b.addEventListener("click", () => {
  if (b.classList.contains("open")) { closeMenu(); return; }
  const r = b.getBoundingClientRect();
  b.classList.add("open");
  openMenu([{ head: "Качество скачивания" }].concat(["1080", "720", "480"].map((q) => ({
    icon: "q", label: QDESC[q][0], sub: QDESC[q][1], badge: QDESC[q][2], checkable: true, checked: S.quality === q, run: () => setQuality(q),
  }))), r.left, r.bottom + 6, { below: true, anchorTop: r.top, onClose: () => b.classList.remove("open") });
}));

S.kf = -1;
function toggleSel(i) {
  const r = S.rows[i]; if (!r) return;
  if (S.selected.has(r.id)) S.selected.delete(r.id); else S.selected.add(r.id);
  const el = document.querySelector(`#list .clip[data-i="${i}"]`); if (el) el.classList.toggle("sel", S.selected.has(r.id));
  paintSel();
}
function setKFocus(i, scroll) {
  document.querySelectorAll("#list .clip.kfocus").forEach((x) => x.classList.remove("kfocus"));
  S.kf = i; if (i < 0) return;
  while (i >= S.shown - 2 && S.shown < S.rows.length) renderMore(false);
  const el = document.querySelector(`#list .clip[data-i="${i}"]`); if (!el) return;
  if (scroll === false) return;
  el.classList.add("kfocus"); el.classList.remove("kpulse"); void el.offsetWidth; el.classList.add("kpulse");
  if (scroll !== "noscroll") el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function isTyping(e) { const t = e.target; return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable); }
function anyVeil() { return [...document.querySelectorAll(".veil")].some((v) => v.classList.contains("on")); }

function nickOf(name) { const n = String(name || "").trim().toLowerCase(); return /^[a-z0-9_]{3,25}$/.test(n) ? n : ""; }
function searchFor(fn) {
  if ($("go").disabled) { toast("Дождитесь окончания поиска"); return; }
  fn(); startSearch(); $("scroller").scrollTo({ top: 0, behavior: "smooth" });
}
function clipMenu(i, x, y) {
  const r = S.rows[i]; if (!r) return;
  const sel = S.selected.has(r.id), many = sel && S.selected.size > 1, nick = nickOf(r.broadcaster), other = ["1080", "720", "480"].filter((q) => q !== S.quality);
  openMenu([
    { head: r.title },
    { icon: "play", label: "Смотреть и обрезать", kbd: "Enter", run: () => openPlayer(r) },
    { icon: "dl", label: `Скачать в ${S.quality}p`, kbd: "D", run: () => startDownload([r]) },
    { chips: other.map((q) => ({ label: q + "p", run: () => startDownload([r], q) })), label: "или в" },
    many ? { icon: "dl", label: `Скачать выбранные (${S.selected.size})`, kbd: "Shift D", run: () => startDownload(selRows()) } : null,
    { icon: "check", label: sel ? "Снять выбор" : "Выбрать", kbd: "Пробел", run: () => toggleSel(i) },
    { sep: true },
    { icon: "link", label: "Скопировать ссылку", kbd: "C", run: () => copyText(r.url, "Ссылка скопирована") },
    many ? { icon: "link", label: `Скопировать ссылки выбранных (${S.selected.size})`, run: () => copyText(selRows().map((x) => x.url).join("\n"), `Скопировано ссылок: ${S.selected.size}`) } : null,
    { icon: "text", label: "Скопировать название", run: () => copyText(r.title, "Название скопировано") },
    { icon: "ext", label: "Открыть на Twitch", run: () => api("/api/openurl", { url: r.url }) },
    { sep: true },
    nick ? { icon: "tv", label: `Все клипы ${r.broadcaster}`, run: () => searchFor(() => setLogins(nick)) } : null,
    r.creator ? { icon: "user", label: `Клипы автора ${r.creator}`, run: () => searchFor(() => setCreators([r.creator])) } : null,
    { sep: true },
    { icon: "keys", label: "Горячие клавиши", kbd: "?", run: openKeysHelp },
  ], x, y);
}
$("list").addEventListener("contextmenu", (e) => {
  const el = e.target.closest(".clip"); if (!el) return;
  e.preventDefault(); stopPreview();
  const i = +el.dataset.i; setKFocus(i, "noscroll"); clipMenu(i, e.clientX, e.clientY);
});

const KEYS = [
  [["↑", "↓"], "Перейти к соседнему клипу"],
  [["Enter"], "Смотреть и обрезать клип"],
  [["Пробел"], "Выбрать клип или снять выбор"],
  [["D"], `Скачать клип`],
  [["Shift", "D"], "Скачать все выбранные"],
  [["C"], "Скопировать ссылку на клип"],
  [["Ctrl", "A"], "Выбрать все найденные"],
  [["Shift", "F10"], "Меню клипа (как правый клик)"],
  [["/"], "Перейти к полю «Стример»"],
  [["?"], "Показать эту подсказку"],
  [["Esc"], "Закрыть меню и окна"],
];
function openKeysHelp() {
  $("kList").innerHTML = KEYS.map(([ks, t], k) => `<div style="--k:${k}">${ks.map((x) => `<kbd>${esc(x)}</kbd>`).join("")}</div><span style="--k:${k}">${esc(t)}</span>`).join("");
  showVeil($("keysHelp"));
}
$("keysBtn").addEventListener("click", openKeysHelp);
$("khClose").addEventListener("click", () => hideVeil($("keysHelp")));
$("keysHelp").addEventListener("mousedown", (e) => { if (e.target === $("keysHelp")) hideVeil($("keysHelp")); });
addEventListener("keydown", (e) => {
  if (e.defaultPrevented || menuEl) return;
  if (e.key === "Escape" && isOpen($("keysHelp"))) { hideVeil($("keysHelp")); return; }
  if (isTyping(e) || anyVeil() || !$("v-search").classList.contains("on")) return;
  if (e.key === "?") { openKeysHelp(); e.preventDefault(); return; }
  if (e.key === "/") { $("login").focus(); e.preventDefault(); return; }
  if (!S.rows.length) return;
  const n = S.rows.length, cur = S.kf;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const next = cur < 0 ? 0 : Math.max(0, Math.min(n - 1, cur + (e.key === "ArrowDown" ? 1 : -1)));
    setKFocus(next); e.preventDefault(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.code === "KeyA") { S.rows.forEach((r) => S.selected.add(r.id)); document.querySelectorAll("#list .clip").forEach((el) => el.classList.add("sel")); paintSel(); toast(`Выбрано клипов: ${n}`); e.preventDefault(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.shiftKey && e.code === "KeyD") { const rows = selRows(); if (rows.length) startDownload(rows); else toast("Сначала выберите клипы"); e.preventDefault(); return; }
  if (cur < 0) return;
  const r = S.rows[cur];
  if (e.key === "Enter") { openPlayer(r); e.preventDefault(); }
  else if (e.key === " ") { toggleSel(cur); e.preventDefault(); }
  else if (e.code === "KeyD") { startDownload([r]); e.preventDefault(); }
  else if (e.code === "KeyC") { copyText(r.url, "Ссылка скопирована"); e.preventDefault(); }
  else if (e.shiftKey && e.key === "F10") { const el = document.querySelector(`#list .clip[data-i="${cur}"]`); if (el) { const b = el.getBoundingClientRect(); clipMenu(cur, b.left + 40, b.top + 30); } e.preventDefault(); }
});

const FIRST_EMPTY = $("firstEmpty") ? $("firstEmpty").outerHTML : "";
function clearSelection() {
  if (!S.selected.size) return false;
  S.selected.clear(); document.querySelectorAll("#list .clip.sel").forEach((el) => el.classList.remove("sel")); paintSel();
  return true;
}
function clearResults() {
  if ($("go").disabled) { toast("Дождитесь окончания поиска"); return; }
  if ($("resHead").hidden && !S.rows.length) return;
  stopPreview(); closeMenu();
  const list = $("list"), head = $("resHead");
  list.classList.add("leaving"); head.classList.add("leaving");
  setTimeout(() => {
    list.classList.remove("leaving"); head.classList.remove("leaving");
    S.rows = []; S.shown = 0; S.kf = -1; S.selected.clear(); paintSel();
    head.hidden = true; $("more").hidden = true; showAlert("");
    list.innerHTML = FIRST_EMPTY; drawChart();
  }, 260);
}
$("resReset").addEventListener("click", clearResults);
addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || menuEl || isTyping(e) || anyVeil()) return;
  if (!$("pop").hidden || !$("sug").hidden || !$("freqL").hidden || !$("freqC").hidden) return;
  if ($("v-downloads").classList.contains("on")) { document.querySelector('.nav [data-view="search"]').click(); return; }
  if (!$("v-search").classList.contains("on")) return;
  if (clearSelection()) { toast("Выбор снят"); setKFocus(-1); return; }
  if (S.rows.length || !$("resHead").hidden) clearResults();
}, true);

$("dlClear").addEventListener("click", async () => { const r = await api("/api/download/clear", {}).catch(() => null); if (r && r.items) applyDownloads(r.items); });

(async function init() {
  document.body.classList.add("noanim");
  paintPeriod(); paintSel(); paintDl(); moveGlide($("sorts")); moveNav(); drawChart();
  requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove("noanim")));
  const st = await api("/api/state");
  S.quality = st.quality || "1080"; paintQuality();
  S.dir = st.downloadDir; S.history = st.history || {}; S.updateSeen = st.updateSeen || ""; paintDir(); checkLink();
  const l = st.last || {};
  setLogins(l.login || ""); $("words").value = l.words || "";
  setCats(Array.isArray(l.cats) ? l.cats : l.category ? [{ name: l.category }] : []);
  setCreators(Array.isArray(l.creators) ? l.creators : l.creator || "");
  paintedCats = new Set(S.cats.map((c) => c.name)); paintedCr = new Set(S.creators); paintCats(); paintCreators();
  ["login", "words"].forEach((id) => $(id).dispatchEvent(new Event("input")));
  loadPeriod(l.period); paintPeriod(); loadFolder();
  $("goHint").textContent = "Enter в любом поле запускает поиск";
  setTimeout(() => checkUpdate(false), 2500);
  pollDownloads();
})();
})();
</script>
</body>
</html>
'''
ICON_PNG_B64 = "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"

HEARTBEAT_LIMIT = 150
BYE_DELAY = 6

def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "ClipSearch")
    os.makedirs(path, exist_ok=True)
    return path

def log(msg):
    try:
        with open(os.path.join(data_dir(), "log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass

def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", "ClipSearch")

def icon_ico_bytes():
    png = base64.b64decode(ICON_PNG_B64)
    header = b"\x00\x00\x01\x00\x01\x00"
    entry = bytes([0, 0, 0, 0]) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
    entry += len(png).to_bytes(4, "little") + (22).to_bytes(4, "little")
    return header + entry + png

class Job:
    def __init__(self, kind):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.state = "running"
        self.progress = ""
        self.result = None
        self.error = ""
        self.items = []
        self.cancel = threading.Event()

    def view(self):
        d = {"id": self.id, "kind": self.kind, "state": self.state, "progress": self.progress,
             "error": self.error, "items": self.items}
        if self.state == "done":
            d["result"] = self.result
        return d

class DownloadQueue:

    def __init__(self, ctx):
        self.ctx = ctx
        self.items = []
        self.cv = threading.Condition()
        self.seq = 0
        self.thread = None

    def view(self):
        with self.cv:
            return [{k: v for k, v in it.items() if not k.startswith("_")} for it in self.items]

    def add(self, items, quality):
        keys = []
        with self.cv:
            for i in items:
                self.seq += 1
                st, en = i.get("start"), i.get("end")
                cut = st is not None and en is not None
                f = lambda t: f"{int(float(t)) // 60}:{int(float(t)) % 60:02d}"
                tail = f" (фрагмент {f(st)}–{f(en)})" if cut else ""
                key = str(i["id"]) + (f"@{st}-{en}" if cut else "") + f"#{self.seq}"
                self.items.append({
                    "key": key, "id": str(i["id"]), "title": str(i.get("title", "")) + tail, "pct": 0, "state": "wait",
                    "path": "", "error": "", "quality": quality,
                    "_src": {"id": str(i["id"]), "title": str(i.get("title", "")), "broadcaster": str(i.get("broadcaster", "")),
                             "start": float(st) if cut else None, "end": float(en) if cut else None},
                })
                keys.append(key)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.work, daemon=True)
                self.thread.start()
            self.cv.notify_all()
        return keys

    def find(self, key):
        return next((it for it in self.items if it["key"] == key), None)

    def cancel(self, key):
        with self.cv:
            it = self.find(key)
            if not it:
                return
            if it["state"] == "wait":
                it["state"] = "stop"
            elif it["state"] == "load" and it.get("_ev"):
                it["_ev"].set()

    def retry(self, key):
        with self.cv:
            it = self.find(key)
            if it and it["state"] in ("fail", "stop"):
                it.update(state="wait", pct=0, error="", path="")
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self.work, daemon=True)
                    self.thread.start()
                self.cv.notify_all()

    def clear(self):
        with self.cv:
            self.items = [it for it in self.items if it["state"] in ("wait", "load")]

    def work(self):
        while True:
            with self.cv:
                it = next((x for x in self.items if x["state"] == "wait"), None)
                if it is None:
                    self.thread = None
                    return
                it["state"], it["pct"], it["_ev"] = "load", 0, threading.Event()
            src, ev = it["_src"], it["_ev"]
            try:
                folder = self.ctx.download_dir()
                os.makedirs(folder, exist_ok=True)

                def prog(p, it=it):
                    it["pct"] = p
                path = download_clip({"id": src["id"], "title": src["title"], "broadcaster": src["broadcaster"]}, folder,
                                     progress=prog, cancel=ev, start=src["start"], end=src["end"], quality=it["quality"])
                it.update(path=path, pct=100, state="done")
            except Cancelled:
                it.update(state="stop", pct=0)
            except Exception as e:
                log(f"download {src['id']}: {e!r}")
                it.update(state="fail", error=str(e))
            finally:
                it.pop("_ev", None)

class Ctx:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.cfg = load_config()
        if "client_id" in self.cfg or "client_secret" in self.cfg:
            self.cfg.pop("client_id", None)
            self.cfg.pop("client_secret", None)
            try:
                save_config(self.cfg)
            except Exception:
                pass
        self.jobs = {}
        self.last_ping = time.time()
        self.seen_ping = False
        self.bye_at = None
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self.dlq = DownloadQueue(self)

    def twitch(self):
        if getattr(self, "_tw", None) is None:
            self._tw = Twitch()
        return self._tw

    def download_dir(self):
        return self.cfg.get("download_dir") or default_download_dir()

    def start_job(self, kind, fn):
        job = Job(kind)
        self.jobs[job.id] = job

        def run():
            try:
                job.result = fn(job)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except (TwitchError, ValueError) as e:
                job.state, job.error = "error", str(e)
            except Exception as e:
                log(f"job {kind}: {e!r}")
                job.state, job.error = "error", f"Неожиданная ошибка: {e!r}"

        threading.Thread(target=run, daemon=True).start()
        return job

def row_json(r):
    return {**r, "created": r["created"].strftime("%Y-%m-%dT%H:%M:%SZ")}

def parse_iso(s):
    if not s:
        return None
    try:
        return parse_time(str(s)[:19] + "Z")
    except Exception:
        raise ValueError("Не удалось прочитать дату.")

def pick_folder(initial):
    if not sys.platform.startswith("win"):
        return ""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description='Куда сохранять клипы';"
        "if($env:CLIPS_INIT -and (Test-Path $env:CLIPS_INIT)){$f.SelectedPath=$env:CLIPS_INIT};"
        "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, timeout=600, env={**os.environ, "CLIPS_INIT": initial},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""

def reveal(path):
    try:
        if sys.platform.startswith("win"):
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)
    except Exception as e:
        log(f"reveal: {e!r}")

def vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)
    except Exception:
        return (0,)

def check_update():
    req = urllib.request.Request(f"{GITHUB_API}/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "ClipSearch", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except Exception as e:
        log(f"update check: {e!r}")
        raise TwitchError("Не удалось проверить обновления: нет связи с GitHub.")
    latest = str(d.get("tag_name", "")).lstrip("vV")
    setup = next((a for a in d.get("assets") or [] if a.get("name") == "ClipSearchSetup.exe"), None)
    return {
        "current": VERSION, "latest": latest, "newer": vtuple(latest) > vtuple(VERSION),
        "notes": str(d.get("body") or ""), "setupUrl": setup.get("browser_download_url") if setup else "",
        "size": int(setup.get("size") or 0) if setup else 0,
    }

def download_setup(info, job):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"ClipSearchSetup-{info['latest']}.exe")
    tmp = path + ".part"
    req = urllib.request.Request(info["setupUrl"], headers={"User-Agent": "ClipSearch"})
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        done = 0
        while True:
            if job.cancel.is_set():
                raise Cancelled()
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            job.progress = {"done": done, "total": total}
    os.replace(tmp, path)
    return path

HISTORY_MIN = 3

def update_history(ctx, b):
    h = ctx.cfg.setdefault("history", {})
    logins = h.setdefault("logins", {})
    cats = h.setdefault("cats", {})
    now = int(time.time())
    for name in b.get("logins") or []:
        name = str(name).strip().lower()
        if re.fullmatch(r"[a-z0-9_]{3,25}", name):
            e = logins.setdefault(name, {"n": 0, "t": 0})
            e["n"] += 1
            e["t"] = now
    clist = b.get("categories") if isinstance(b.get("categories"), list) else [b.get("category")]
    for c in clist[:10]:
        if not (isinstance(c, dict) and str(c.get("name", "")).strip()):
            continue
        name = str(c["name"]).strip()
        e = cats.setdefault(name, {"n": 0, "t": 0, "id": "", "art": ""})
        e["n"] += 1
        e["t"] = now
        if c.get("id"):
            e["id"] = str(c["id"])
        if c.get("art"):
            e["art"] = str(c["art"])
    rm = b.get("forget")
    if isinstance(rm, dict):
        (logins if rm.get("kind") == "login" else cats).pop(str(rm.get("name", "")), None)
    for d in (logins, cats):
        if len(d) > 60:
            for k in sorted(d, key=lambda k: d[k]["t"])[: len(d) - 60]:
                d.pop(k, None)
    save_config(ctx.cfg)
    return h

def folder_info(folder):
    files, total = [], 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                n = e.name.lower()
                if e.is_file() and n.endswith(".mp4") and ".part" not in n:
                    st = e.stat()
                    total += st.st_size
                    files.append((st.st_mtime, e.name, st.st_size, e.path))
    except OSError:
        pass
    free = disk = None
    probe = folder
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        import shutil
        du = shutil.disk_usage(probe or ".")
        free, disk = du.free, du.total
    except Exception:
        pass
    files.sort(reverse=True)
    return {
        "path": folder, "exists": os.path.isdir(folder), "count": len(files), "size": total,
        "free": free, "disk": disk,
        "recent": [{"name": n, "size": sz, "mtime": m, "path": p} for m, n, sz, p in files[:8]],
    }

def make_handler(ctx):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), ctx.token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                html = UI_HTML.replace("__TOKEN__", ctx.token).replace("__VERSION__", VERSION)
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/icon.png":
                return self._send(200, base64.b64decode(ICON_PNG_B64), "image/png")
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/ping":
                ctx.last_ping, ctx.seen_ping, ctx.bye_at = time.time(), True, None
                return self._send(200, {"ok": True})
            if u.path == "/api/state":
                cfg = ctx.cfg
                return self._send(200, {
                    "downloadDir": ctx.download_dir(),
                    "last": cfg.get("last", {}),
                    "version": VERSION,
                    "history": cfg.get("history", {}),
                    "updateSeen": cfg.get("update_seen", ""),
                    "quality": cfg.get("quality", "1080"),
                })
            if u.path == "/api/job":
                job = ctx.jobs.get(urllib.parse.parse_qs(u.query).get("id", [""])[0])
                if not job:
                    return self._send(404, {"error": "нет такой задачи"})
                return self._send(200, job.view())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = self.route(self.path, body)
                self._send(200, out)
            except (TwitchError, ValueError) as e:
                self._send(200, {"error": str(e)})
            except Exception as e:
                log(f"POST {self.path}: {e!r}")
                self._send(200, {"error": f"Неожиданная ошибка: {e!r}"})

        def route(self, path, b):
            if path == "/api/bye":
                ctx.bye_at = time.time() + BYE_DELAY
                return {"ok": True}
            if path == "/api/quit":
                ctx.quit.set()
                return {"ok": True}
            if path == "/api/link":
                return check_link()
            if path == "/api/settings":
                if "download_dir" in b:
                    ctx.cfg["download_dir"] = str(b["download_dir"])
                if "last" in b and isinstance(b["last"], dict):
                    ctx.cfg["last"] = b["last"]
                if str(b.get("quality", "")) in QUALITIES:
                    ctx.cfg["quality"] = str(b["quality"])
                save_config(ctx.cfg)
                return {"ok": True, "downloadDir": ctx.download_dir()}
            if path == "/api/categories":
                q = str(b.get("query", "")).strip()
                return {"items": ctx.twitch().categories(q) if q else []}
            if path == "/api/search":
                return self.search(b)
            if path == "/api/cancel":
                job = ctx.jobs.get(b.get("id"))
                if job:
                    job.cancel.set()
                return {"ok": True}
            if path == "/api/download":
                return self.download(b)
            if path == "/api/downloads":
                return {"items": ctx.dlq.view()}
            if path == "/api/download/cancel":
                ctx.dlq.cancel(str(b.get("key", "")))
                return {"items": ctx.dlq.view()}
            if path == "/api/download/retry":
                ctx.dlq.retry(str(b.get("key", "")))
                return {"items": ctx.dlq.view()}
            if path == "/api/download/clear":
                ctx.dlq.clear()
                return {"items": ctx.dlq.view()}
            if path == "/api/clipurl":
                cid = str(b.get("id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
                    raise ValueError("Неверный клип.")
                url, q = get_download_url(cid, preview=bool(b.get("preview")))
                return {"url": url, "quality": q}
            if path == "/api/csv":
                rows = b.get("rows") or []
                folder = ctx.download_dir()
                os.makedirs(folder, exist_ok=True)
                p = os.path.join(folder, f"clips_{datetime.now():%Y%m%d_%H%M%S}.csv")
                with open(p, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(["Название", "Автор клипа", "Стример", "Игра", "Дата (UTC)", "Просмотры", "Секунд", "Ссылка"])
                    for r in rows:
                        w.writerow([r.get("title"), r.get("creator"), r.get("broadcaster"), r.get("game"),
                                    r.get("created"), r.get("views"), round(float(r.get("duration") or 0)), r.get("url")])
                return {"path": p}
            if path == "/api/pickfolder":
                return {"path": pick_folder(ctx.download_dir())}
            if path == "/api/openurl":
                url = str(b.get("url", ""))
                host = urllib.parse.urlparse(url).hostname or ""
                if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
                    raise ValueError("Можно открывать только ссылки Twitch.")
                webbrowser.open(url)
                return {"ok": True}
            if path == "/api/history":
                return {"history": update_history(ctx, b)}
            if path == "/api/update/check":
                return check_update()
            if path == "/api/update/seen":
                ctx.cfg["update_seen"] = str(b.get("version", ""))
                save_config(ctx.cfg)
                return {"ok": True}
            if path == "/api/update/download":
                info = check_update()
                if not info.get("newer") or not info.get("setupUrl"):
                    raise ValueError("Обновлений нет.")

                def fn(job):
                    return {"path": download_setup(info, job)}

                return {"job": ctx.start_job("update", fn).id}
            if path == "/api/folder":
                return folder_info(ctx.download_dir())
            if path == "/api/openfile":
                p = os.path.realpath(str(b.get("path", "")))
                root = os.path.realpath(ctx.download_dir())
                if (not p.startswith(root + os.sep) or not os.path.isfile(p)
                        or os.path.splitext(p)[1].lower() not in (".mp4", ".csv")):
                    raise ValueError("Файл не найден: возможно, его переместили или удалили.")
                if sys.platform.startswith("win"):
                    os.startfile(p)
                return {"ok": True}
            if path == "/api/reveal":
                p = str(b.get("path", "")) or ctx.download_dir()
                os.makedirs(p if not os.path.splitext(p)[1] else os.path.dirname(p), exist_ok=True)
                reveal(p)
                return {"ok": True}
            raise ValueError("Неизвестная команда.")

        def search(self, b):
            tw = ctx.twitch()
            logins = []
            for part in re.split(r"[,;\s]+", str(b.get("login", "")).strip().lower()):
                part = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", part).split("/")[0].split("?")[0].lstrip("@")
                if not part:
                    continue
                if not re.fullmatch(r"[a-z0-9_]{3,25}", part):
                    raise ValueError(f"Ник стримера «{part}» выглядит неверно: только латинские буквы, цифры и «_».")
                if part not in logins:
                    logins.append(part)
            if len(logins) > 10:
                raise ValueError("Можно искать сразу не больше чем по 10 стримерам.")
            cats_in = b.get("categories")
            if not isinstance(cats_in, list):
                cats_in = [{"name": b.get("category", ""), "id": b.get("category_id", "")}]
            cats_req = []
            for c in cats_in:
                if not isinstance(c, dict):
                    continue
                name, cid = str(c.get("name", "")).strip(), str(c.get("id", "")).strip()
                if (name or cid) and not any((name and x[0].lower() == name.lower()) or (cid and x[1] == cid) for x in cats_req):
                    cats_req.append((name, cid))
            if len(cats_req) > 10:
                raise ValueError("Можно искать сразу не больше чем по 10 категориям.")
            cr_in = b.get("creators")
            if not isinstance(cr_in, list):
                cr_in = re.split(r"[,;\s]+", str(b.get("creator", "")))
            creators = [c for c in dict.fromkeys(str(c).strip().lstrip("@") for c in cr_in) if c]
            if len(creators) > 10:
                raise ValueError("Можно искать сразу не больше чем по 10 авторам.")
            login = logins
            start, end = parse_iso(b.get("start")), parse_iso(b.get("end"))
            args = dict(
                words=str(b.get("words", "")), start=start, end=end, creator=creators,
                min_views=max(0, int(b.get("min_views") or 0)),
                max_pages=min(50, max(1, int(b.get("pages") or 10))), deep=bool(b.get("deep")),
            )
            if not login and not cats_req:
                raise ValueError("Укажите стримера или категорию (можно и то и другое).")

            def fn(job):
                resolved = []
                for name, cid in cats_req:
                    if cid:
                        resolved.append({"id": cid, "name": name, "art": ""})
                        continue
                    found = tw.categories(name, job.cancel)
                    exact = [c for c in found if c["name"].lower() == name.lower()]
                    pick = exact[0] if exact else (found[0] if found else None)
                    if not pick:
                        raise ValueError(f"Категория «{name}» не найдена.")
                    if not any(r["id"] == pick["id"] for r in resolved):
                        resolved.append({"id": pick["id"], "name": pick["name"], "art": pick.get("art", "")})
                rows, info = search_clips(
                    tw, login=logins, game_id=[c["id"] for c in resolved],
                    game_names={c["id"]: c["name"] for c in resolved},
                    progress=lambda t: setattr(job, "progress", t), cancel=job.cancel, **args,
                )
                first = resolved[0] if resolved else {"id": "", "name": "", "art": ""}
                return {"rows": [row_json(r) for r in rows], "info": info, "categories": resolved,
                        "category": first["name"], "category_id": first["id"], "category_art": first["art"]}

            return {"job": ctx.start_job("search", fn).id}

        def download(self, b):
            items = [i for i in (b.get("items") or []) if re.fullmatch(r"[A-Za-z0-9_-]+", str(i.get("id", "")))]
            if not items:
                raise ValueError("Не выбрано ни одного клипа.")
            quality = str(b.get("quality") or ctx.cfg.get("quality") or "1080")
            if quality not in QUALITIES:
                quality = "1080"
            return {"keys": ctx.dlq.add(items, quality)}

    return Handler

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def find_browser():
    pf = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
    rel = [
        os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join("Google", "Chrome", "Application", "chrome.exe"),
        os.path.join("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join("Chromium", "Application", "chrome.exe"),
    ]
    for r in rel:
        for base in pf:
            if base and os.path.exists(os.path.join(base, r)):
                return os.path.join(base, r)
    return None

def launch_window(url):
    exe = os.environ.get("CLIPS_BROWSER") or find_browser()
    if exe:
        try:
            profile = os.path.join(data_dir(), "window")
            subprocess.Popen([
                exe, f"--app={url}", "--window-size=1400,900", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebar",
            ])
            return True
        except Exception as e:
            log(f"browser: {e!r}")
    webbrowser.open(url)
    return False

def message_box(text):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "Поиск клипов", 0x10)
    except Exception:
        try:
            print(text)
        except Exception:
            pass

def run_app(open_window=True):
    ctx = Ctx()
    srv = Server(("127.0.0.1", 0), make_handler(ctx))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log(f"start {VERSION} port={port}")
    if open_window:
        launch_window(url)
    started = time.time()
    while not ctx.quit.is_set():
        time.sleep(0.5)
        now = time.time()
        if ctx.bye_at and now > ctx.bye_at:
            break
        if ctx.seen_ping and now - ctx.last_ping > HEARTBEAT_LIMIT:
            break
        if not ctx.seen_ping and now - started > 120:
            break
    srv.shutdown()
    return ctx

HOST_B64 = "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"

def unpack_sources(b64, folder):
    import zlib
    files = json.loads(zlib.decompress(base64.b64decode(b64)).decode("utf-8"))
    os.makedirs(folder, exist_ok=True)
    for name, text in files.items():
        enc = "utf-8-sig" if name.endswith(".cs") else "utf-8"
        with open(os.path.join(folder, name), "w", encoding=enc, newline="\n") as f:
            f.write(text)
    with open(os.path.join(folder, "clip_search.ico"), "wb") as f:
        f.write(icon_ico_bytes())

def write_host(folder):
    unpack_sources(HOST_B64, folder)

INSTALLER_B64 = "eNrtvWuT3MaVKPhXQFqPKrMKXVVd/WA3H9MkmxJHbJKXTUmW2VwKVUB1w11VKAOofojqG5I1cz0TcljruRMxGxs7njsxEbMf5sOlZdGmJUuK8C/o/gvzS+55ZCYSQAKFIunZ2djVeNgFIB8nT54878x8cv5eGOyGzsjuR+fXrPPTyB/vWtvHUeyN1nfG+qN9PRgOvX7sB+PIfssbe6Hfzxa54Tu74yCK/X6U+xQ6h/CYfX3rbv4N9DSahF4UQVfZr7f98U+z7+54cfbVfW8gYM19mY5jf+RRH/7QC7e98MDve1FRuVvj2AuDSVGxB95RbHoHEOxOh064eSRHkq+6F3qOa8CJ+mA/cKL9XL33vZ693Q/9SWwDVL4z9D9yTCN93x+7wWFk3wzCUdLIlt8PgygYxPh9sQPvd8ZjZ+RFE6fvWdeHPgzVCft72148neyMn+yMLfgviqGLvtUfOlFkCZLhL6IA/vdw+8EGw/4oeSlq+uPY2nL8cS2KQwDj4SPLCXejelJOawf/25hMbgQjqGBfn4ahN47F00YUeaPe8Pi+FwXDA8+6cNkSPzdHPc91PXc93VAcHqdfZDrC/0IYazi2YMprBFWmiZP0Y9+J+3tWbfOo700Q75Z3VJ/Zxe1g134/9GOvtnN+4MTOcM3aOW9dwLrr+dL+wKqd2wBQ7LediGBqQPFmMwKKHcc75+v1fB38bwtIzdn1rgVH9vZecAh9nf7L2adnPzt9evrt6XenX5797enz068teHhmnX16+kd491vr9Pfw+fuzz6jg87Ofnf0Cfn2xtgOUwRDaolmEISGQnfMNrb9r0zhGxnD3Hf3trX4wtjfDMAhNoxRobxej+wSpM0NJkgKy814Lej+BJW9F3tj1wob6fAAYQ1RaXjG1MVFauA6sy9bYO1S93IFXNc/GP3X6FzCyc952h8Od8xm4eX3VtmNYASMrgoZkI8Au480jrz+NoYR8Wavj6y1n7A+8KEZop2Hf49o1hKQ+m6qQTqAjAHk6HNYlQvHBgG4B34ETWqNIDHPLGwXhsejURFVPzIQWAf+cHD8IaqPINLPa7Coc3A4cF4rbD4KNMHRg+KaKJ9VI4eGWF+8F7q3RZFhLft6dsHC6E9waD/0xjLZuZkS40CvyIcH37wVQD+YKyDoErtufwlI+Bj4YB/1gaH182cq+e3A88ewHw6jdyYxSZwQAUhgzH2Bw7L+EfuCDhYuLAKuv6+MWDM16Yt3wQyB2mDuAJpYsUr6r3XPiPaSuB95ogr8R2daJ4FxP0riUdDQnuxHTu01FFNtMl7s1hhEOh7fGg8Da80JcWyRWEDTxDdZtlg56AaDUH0e+64EUgDpU9RwTufXGGxJXmz+dOsNIjVWNnpYs1Bz6fZKLNi89pzf0sGzdfhD6o82xW3tzB/57s96gDkB3AaaxTU2jbuCEfgR174YgiJ3hLVBrQu+6E3lZaBF1JtxNgf5ohIA+6+OPkwHVcQgm5j4Ig1ETBPckj/AnWcxqTCseIY4ICwB3zx97hulPs+5mhLPQJMJ7a+q7oEEd4l9gSQ8CRgEQ4R2Aw96e9rifWqthrdaZ+3lHHnwzLN+bQA3EGUrw30CI4Z9w6pmaEKNyYEwz0Xo1/QpX0pplqjRxndjTatAzF985j+8vZGfAABksbVAAI3sbV20Nuad4Qy+QyGs0MqcOS+zdyNve84ZDHjtS/gCI1bNOSgRhKy8I0y/uAD4PPFzxoucb925tHDqhl1tDKfSPEfXv+REsl+34eOhFpcWRnyD9xz7UQlX2PglUmJQb3sCZDuMajSS/2klpQeX0XujB8vGQ4XthrYIYQ5EUAopy6otRsIBS8/en35x9YYH28jWoMk8tobuklBx4ffrt2Wenvzt9dvqtBc/fgbrzPfz7Lb44+1miCFub7q5nAeDv+d5hx7ZOf336m7PPz36OrWEf31rcjHX6DLs5+4V1+qUllGur3bJOn1vtdsPi4r8//YqKQaFnp7/FN6hrIZRnf3P6zel3+MlG1WpnfPprgPnrs0+gL2yU2v6EBvH87L+dfcZ62W/h+2fw62voRYJ4FaRDEWpm6WcfeNGdIKeiIfGGI5r/InU0RDUD7LthsAu6CpABNlXPLIqd83txPInWFhZ2A3ukLI1+MFoYHIJI3l+YLFwFE27/lnu50+50V1qLZkYiVkRn1ooQ3GIUuLjEqvAL7UUVZiGfiFOomiaJtUu20WW5SK8747fwTW6tIbEjtqUGBlYNWmg1HEODW2mY4Iq84SAGTbE59VFAlKxgVG+wh/wSvT70nPF0UpslfTKNi9nANoGZ+8AfXG+9XEtHNUvoBhU1rSqGGuIuEIgT2l5llRXku9I/bvpH8MhoeM8ZTj0NEa4f0uRfFWUF18Py9XrBurvhRftxgHLYJNjHQTPaC8IYBEGETRfIfyjmcjtQqKCjbdlOta4KWrnv7fpR7IXFjYSiRHEbt4O+M/yxP5HLLovGj/yCYZwYljtjWqiENeBPqOo1wLaJG9agYbmgLjSsCLWGo7p1+QqqsA0LVlgfJCzR/INg3xuD4j826hQpnZuI0pIqw913hCEeoAI4n3B+pU6BDFwDB57dUieBAGxxPvP5IPBdxQeIcSFjueENvThjIM+/PAU37o9coIqd8wt9a4LP7c6K3YL/a1vNsdW1rsDkWm9Y4QhWmrUQWQs/tUAPP08jRfUBtdb7QRCTUsZfCmSSBjl1egF7haZdb2gtDFINF+uj5d1UUPp2zkPfrBM3EAxU/66DQR17dwJWEwAbqO42irTChlAnSDtDZZ670F7ab/uu643z6qORDJ9kiCBFCylXHi7dnB9vMu0BrmRBohFkEAkfb+gOExyuIEXH3hgf145whaZttKMGFa1kX6GlWgSL6JZ5TRE8hRIG5JdVQ8HkA45b6/DnEoB82xvvAhU0rTa8uXChXkBqqfE4D/1H8wxJIeihD+TWfmSWrxm/TenEAevIzZsogA7YYDw8toRDDDj1vpCZ/CalkhgRjHYcrY0n1q4XJxM8l5XJTB30RVTxrJPiWSWWxHxQ2rRgdpRqCgDSEIdVw8HV2e6EJQ6WysZwiEZLTQ6hYd2AlfgAXfl3gkPdvj2G/5pbW03Xtd5+e200WosiaQpazHcRDPizOT7ww2A8Av6MljLorzDtm+N+QD76dx/cXE3TbMWFyBOZUmNMi5AQArIpP2e0MhPVA1lMQSFdbSgppukF5lICGin7140j0r0+TwzjyLx6zwsp0JNt5raD2m6uBVU8/frdcKheDQMMgvgfeVqbIBK8XTT6idjuexNAiKQ2ABed224A1T1rEDr9BrcBy8gTP+MAxqTKRBPPcxXXIWpdNy5TEUUxzy1YXjBA0QiCRFKzDwsoYjeNM5ks6AbdenkT6GqN77APO+XuOcRIT/NouWuTVjarmckE5FOmDZZvM2q+K20rWV+9mKP6O94x1P2LnfPbYDqic2NnR5nqOzvC7t7ZET5PQQ47O6or+GRGWbqvjcwAeabOp0wmI2/U7YFCFmXilzobAbZ5Mxi6XkiMU/+yPfH6YGXzV5tWmqa7AC9ziM2KGFyEOkfa6q8XWWbG0ZAas+WNp2iVl7L7FwFfQpmRDPZwvD9LKChsE3v7s0An2laO41Ioi+DUuV3arf1Slq4I1jBHDo9xUaAQl88yKgoKZWjfBbm3Pe1BmZpaQnPEclC92a8aRNLUfJcM6n1ENytkO+fF6JFqkVpRnDqRKL9e3LtQrm5Fd6DTu+HmaBIf16B5cpyfI+G+eQTjFs5+OecueuyZW9XrVQCXJUATSgsp9g3cQTcMdtuQUgad0KkB3vCjydA5llxHHx+5DMjaOJkrsKVrC/oQZhlyYhYIaPF7UkxzKkaAiwOap6UxKXD0TKRSfMVatK7CYyZiYq1Zk7m4jPC3SDD9/r7nzozGTuSUiPKGwEshaUwMhDExee1Qh0dr1RxTwvYpnDSp54PfLxYr4g7feMOqnUtieGIIEyZ47f14OvJCUFsQEE7r2BxDtx6WJWOrZHwmNjl5QWkhlLGbHtAp/669vCcv9H6KC+ztOJ6A3X/f++kU2q0nP222ojWvsjPx7V0/3pv2yKccgs4ULZCqTuoTqu7wcugB7qOFIcFZ5F/+qY28c2MXnS6XQR0oKLTRR4cO6QpOIoYXDsauAOTCTyJkBAXV0e4Ipli/vdRqtYrjXdhKecw+9KJJCl3RBBQar47dAKnKZ2MUX2smCoUtyEH/+x4FabBxvRWZEZC1cwryThB6XEShje09CJBXmNCOALii/790DhxOY5JZTGB4PLG2nKO/hNYEA7qMjmQb3hEDtk5QZsvSl274lOHlhMeXGIsNYeBeuVJDiErCi6DxQ9surItx7IDIRNm5cx7ePsb1yc5/+HbghXFiMroP9SKP6iJ6WDBOB7XxyNALf+A+sEn5/AhliSHX7pbgAmh4rBWJN2QtokcRKS+YqgHwJgddlcI7EACGBawFNQrUBjVOGGKAoBdPx3pxCwS3VD6Q++Ww7gCKFMbPXU6snDpq9LE/ThmqJSKf+VZx0ZJx4n+JSgA0QBKRfYJvHrzZsN58780iR7n8DyxUqGscXi8Ek8YLH4NlMx4Gjvt4Gg5xuDNaRBM31eStcbzcpRYj+DSjhZMCrJ2U+6mZq1qCy1p7MPHjgN0lyczM7yuHla/3ogka5QOXaWh5Z0tukqG1ZLo4lsDo1wKUiRgpFyALclYW0oNsSPy3UqgsEaLkepBBDuH3sYKGhJm7awjnhBXSH0OYw+rHM3UnN1FojYENqWI5Ics/cxKNm69YqK5zU6CNxHtAzTQXKGAp+Q/zEP/x9NnpN2e/OP392ReWiNZ/ydmG1un3Z59gJiJFv/94+keMfcOrsy8wQRGD7Ri9h48Yt4fSX0G1TzHAb1unf3f2+elvTp/BR2gKA+3fYy7j6ddnn9l5wY+w53QuMjNAHaukdGFhVrsoiFfRMskvqAIc/Z2CH4byp38jesOZRLL80zcqDeEbwMTvTp+e/QxwQekBvz19jgmdfwV1/wBI/tyIGE6SgH9/qWHJgprfnD7X35x9KiaEExZO/5BJr6BUij9omcIGTGdz1FiRS/LTBLEUeE3BinWm8V4QwvpyUQGLIsUrkFEw9u4E9/3dPTGD64WpAR9RiDKwMz7LlBGAYW8OZVI4xrxWyEuI4lxMiA3TlxP+JtXXpBmS+3HnvGQtaE60tP8BLfwTYPkrwPU3Z788+zlM9i85qeRr6y0/fnva+/dP/tWs2RYuUEDEnE4BweqVZ0A+p82AAjHCeJ8vLe2FE9I42FwASTK7Gad2KoIvZkIM2gZx0cAxNBQf7scVElaJiGPM4A8Obw101i1sGpHvWEIT/WEQUSixZa9kaYL45Je4rM++OPtlQyQXyfztv0UOiXlPvKLTTPX0aSHJXMce70/HY1JymeeXAOgdxaHTj80g/g8g1U+Jm3xNuVifA7hAvIV9bzsDj0O4NVpgpjKzuImoaJyLl2Io2dY2eeQ1ogsRNSgnD8E7Qgxnw7rxxy5IBQxuF4+Wkq2ovFx3hdKCxSCyB5h3ZM5/tODxk7O/Bq7+JaedPQMpkfbig3AA3gJlvkNp0sBGfkPilVPm4B/mMFSbMua+ofS1L6x//+Tvc5sFOC3uV/ye5cN3tnmqX3JdDPyxH+0x1V1cSVPdPxCNo34Ay0IThHnV4vNCShRTFQxdncnDo9HC0+gWihTNo1HbqFtbwYH3Puif9z0gbVYWChpJGsA6RBcNyzUnqlRZSnmwCxHhUQynUK0pXKfVcoDz7erBo7rMDy5Z02XmQx+6t1SmnxdWsCHklAW2CpZWl5ZmIl1dSlPpPwMlYn7oU6TRL4AkgUIxsbOQJPWML+kMSwVsGjhLhQSRDElEOuq51rTwyuy2PExgzrpdtRZIKVY90JaJCEzSIN0LL4EH4THTIbaVaqSakOVxyYD1HBtXmLgPKMgtNRxtb8E5o+4klAJhU9bRc5N+Za1Z6PkCihcvaiV4lFBDaSZ9gKbisA2E1k4T2X8HXv4z5sN5osq0OEB39fC40jYjoUVlZw61yhfgRTNiG8naVkI5MWxnRl1NticaS9+TFPiShRwLMjQlOaE7Z26VWpdYAwQnIPnsVyh12WD6K7KmnimjivqRJhpJZ2m7PiPd7SnVe46i6+wT68YaRleEkqdJLLvES697FpQKK1A1RRVWGfrIdF/KszCPxx66zoeWZnjc0+7yxVbeXY4lcJURl0+KLhcU3RgOg8ONaRzc91wSo2YLQPjG+7HiJrWaSDJlgUZt9XDZpTdRWScVtjbkDMIS3vSKnP3mvYhFHv7yBsgFwo0MSIFF2c5zjCRVn99lnGTTSKDQOw40wR7/BiXdoFetASw2iq8dA5uldL1yL3SESZbbIEMOcXo4XROMyELzVIGCnWzN6kFP+6kATG86EEGOHoD/sG1dumS1Vx6VucFhTYxLvh/uAeatWo2iLLQIatAJ8Xz4K8K1IJeuWK36C3u4qyjoZfUHQv+SkI1nVaCpvnC5dORqosaURxsd2ptDZxJ57pY/HPqR1w/GbjSjPsVdoXpTzvaVy1a7U4apCtjS6AL1TFytNJ5mQrd164dWG5iT3bIWrC3UeLecoxoIbB2YWUgiNi8Ij/9eQp/zVe51Tbz7IdriSyC66C0+dZYqNKyoHyBaTy04HMt6ZQxg0hyyV1rXVwg89A0AIISTBfFlrXTplLrHOC2PM/JEMp7IwiMjeUazJyVxkPLgVDIk0A5pMJe4/0K7/B9S27ae4hYyNrC/hA/foH8ENQ7UDUjjYKohQpF4Yq/W62ipax6fX7AO8hx3r5FSwta9UDTAmP+jUFP+9G/kPfwS9UDWXc5+AfpMsY1R7JMk/w4jXdGvSoUco51WYRJOqrnN2KwDaVeyiaJkXb4o95KbD8E4lLIRo5kZyVtiXoW6/RCi6Imn0XXakXaZmkne2HeC+GYwHbslrKcwfpI4Zp6d/S26XMT8/zbR+NFZn7hznmP+GMeylP+/DkT1DzkXTlIJVNve1B+6TTT/Ucn9RDiBfk9FiZ6Q4ihk8J1wr/wGaP0PsiF2Aj0tIbfiCBGGOL6iVUIbJTUHV0GgyGDYv/ChArouLT19SRBBqdLuKwvSCa9gLi0Le8jtUpfbVHaKzp0A42wj7O/5B571Ecbk4ZnUNcwOJIXBEAAwLKWUfqbLrI8cW4TB7O3piHd5HCnNI9HbTK4w0G76QPY4Wq2Z6/iqIXZjmJMo5lHr0upcs23Ot5lPP1PZEgl2EfpjC4W+NpY/P5vyL1woT8cUh5d4NtIRhm+BnU2GTt+rvbmAGQpMRuVtAHoxxdZAk2l/HS0BPqOkhDGe4+Z41iJ0eAoHZqUtMwVc4n+X/m1mQF8Sg/odWNm/Ov22hOfI1DuYMJdBkaupXi8NLvAYwPJTySZFGkNxG+boumi5COK8Ccdr+SVMNtnj3Ebbfwrz5P9d5kXrFZgXKe18dvGXtd/KonwgfsCkWMko9BmFvDQEKES1zxusYAX/nl+wYKCXMnCD1cq11RfT6U9eVjdQ6cx6HC/JDiiW+KXJyiKSmEtYpvfruT2MLJCkNoJyKOE8GpPBfBWGqmBLY3H2igEWt+qWRbM+lY54CdjxyIFkE1VQfeOmaaPmk9lxYwqhcafQ27oYQ1mM6dbddJTpcA80uxruG+10UWwgGn3rdauLqv5iNgo1gv6wk/DYFH9KRd0RKvIu8vl59vbQ8ya1FWAiLxjW/s8IbhJzr6wtlZgKHJoGMwbD32AI4aF17NWnSLDILUtiwWuYRvaU7IkvmmjKWGRX8IkuSYhcWVffJaezUKoZWk1fk/3xB5lRlctaezlTvCIvMiQVpianSqREhgVCb+Af6Vtfis2MLOchfdOnaMvDR0BK2e0RvJPxcc/pgznk4raYeol+Lba8WxNsUm66f0sddORF11hpMh2MV0Y/Jvf7DOkruZNPe/FLthMlPEaUndh4mstW4E6Bs8q9I5mD18qUCGpGD0biC1195vmqqEBXyI7GThlo3lf5tjN2GYRb4/heHNo/9sIAIZnYRGJJSc6AnNjvO358MwjxVJhaFyyu6v2em+CxI1jRoyMTJvY7oHJhjMXQ6vocIr1qjgAOiBndWJ7rWDFPQIudEuC4KyzAaMz6DBWjRKfIhI+rLOI5juSYHAhTQLSP+95oX1vyWJLBivs43Wlfli46sNScF3xAlNLfc0L4csPfhSmdHDxsPdI2Lh3Y25MhvH/zwpt1+DL3ERfzIuMnxsSWnfOhOIRT7Kp1JhP+MQEeBsRg81afwgNbdYVq8pNkfAWbRLZoICPe1Ykkh8+slRExiIMMoKWGOKHkQG52ZhYd/XCN/+CL2sP/Df88Ar2IzjIphHJkb09JV5gFn0yVAABH9lthMJ1ED9uP+KCf4uNoDnQL227xNpeDdNrmQXLoRgd30Rysz7M7ksCt5MXTEy2SBdXQkkCitDaSoRkyDHsFEUjm/PIrqihK06TlRLniqVzyCDDyQ9o5SEKSdwBgmFpT2evkXBugc03ZzSUS5IVytebZW8yeg1m7iw1rbR8PEsxsm71D23jKtiCW1Vabbhty5uSO25kt3EOVKdrD86zm7j23p7kwR8oENB6vh1VwhlDCNFoVulSpcCznGWh1lNGM1Dl1npGVPv1uZqf3byPdbvSCacw9lm2VqYq2G3R6XvkhLFtbrms4dS7X5J0AVCt/cCyyniS90ud3QCuxb7wfhG6FZmAAjh++ZDObUeyPYFTuNu21apCFWmePuT+u6fsWG4JNLFjtFlhilfucwwJQJMBneoUeWnN4WkQmVFHM7PQd8SKgXXpMrylhvbWUz8slG4pdQJqZ1aDTNDlNCo/Y1M0tYz4k2a7jJFmvnjZ68JPc6FQAJ+OEAW1nAP0XDjtl0ocrAYLaTaErRYJVuBuoUEsBmwq1fylbxMvWo8K8W1k65QxisZNAUUEAzbBXTIdoUddCWhUdolVgcKWzCamhh/6jMi8+OjA6S+jBaNWLJpe8leis9DOJFzqksA799JtyaihyYKbarJbGqaVH6oRbKYVcVajP5TL5v5WvQ/eyiqsAZNT1ucxPWLMonPx7uiLgdxjTbczwkZg8JOIE3BeJFZ8ULuNUinV2Ff/m9Dn6YzIJ1gRCJt8Ten9OEfBn6C8qX+na9mRTenMmJTuZoAx1p8rNOs4im1ddCZJUonUCxxzp1sxLjKogN8Cq4IPQ8xJ1sGGJI6EV6yi1vENvl87jir0K9jZlWChpNpt7akvrpQ8IMh28lNIdZ6Q+//mOfyqF4mTmyqmaMz5L60ilec92WJhkRzez+TbZJ/OCXn4BDynGszaRPEk7sBdNnqYqhmZ6eUkH4qwTDLNLfkChaHoDeuZGDM30pqA31gYNepu8sdFf6wwBWCothjzgs6qqnz2oBATY4MmCVSfuJfJDHrONxnrPibzaqM4Z0OZD7wRnmnGwKHuuOTItOhgi30rlIqjHwyDcv/Gy3rB5Q/QAj3GN4+0eFmrIdM0HbmmFvzfDYIQHnt26AWvtfXlDEx73ara+5H01WAJPUe7H/oEDXQvYSPMe971aXFaZsi7tW+ODYN/b8kY9D4W9MNrlNMCCvwY2BmDx5tDZjVRpvDdFHrVMUDS0Q0LJpw/jN99YQAggGyHqy/HXzLvqssA9oHlFfOcA44sNJl4YHyu4+jmgRJLKSbXuwK7ax7sL5Ly+YKeC/Kr2ima/7jF48WFKp0HVnkG2E+W9eMdi8zltVEHNSSXhPacM1m/psAHcmQ4/KAXHenDoxyiJqsK47bCqPpsq+8WZpVtOGO05Q/smOuJFniPI2rt8xG3Uf4E6uAZe/uwSeQA76za4hCqEFkokLRn0mpJn4JUvHSbI+a+J86mD/w2nIvz/bNCQNpc73UfnjnMwwrcKFiouhf8H14Lm5s+n9gENluSsvshxeeVhACbKyied87Uc+TsLbwyHt0ZCQZ6CwrPY4cvd6o8yy9A7ij3ols9TLr+BJ9WoezjCY+tKG0Wd+MbhCJrl6KrS9Goch7X2Dsdug4o58AlPHRjQ0wG7F/EnHvn0KoDYhKexezME/QfZDVgadIGe56RhQQi2Nu6/devOtjUCYgBTsLD3efDKCLgXROnuxIMziPFuPwT1iP8c85++eOzD8xR/DHAhFUK0D116QwlTw7q+54TbvIT5lw3GbZ9uZIGH204Us57MOwiLxiCAxIuPbvu90ElSnWizXMo1+RDWw7QPTR8H07jGf8gTvI35euMYjED99rrktGuopBCvVHUc8W1vgInpuF22YT0IJsBTgjgORutlwgUrijOVr2He/5/52FoyaLec/h5YxPq5tXjc892bD97fuL9pOu7ZuvMgd+Jz4SWYgj+gA/4e8DivluPKqSNdRbM0+jtT5M3Y9Hy8qDWXLE+uBkpaSM8BZpB2Osud9no5U3vf680wsgT9qTs1XvpAf9OJyC9yHaNJx0nfG1btjDM5ssx8FVt6bslpaHTipCkzQF69xbAJTvZnvP+zoMe5bwRNnMGgfDhD34U1F5JVojRYdA5/ab5CTZ0gU2X4L7jbeeYO4+Su0UHVA5hyyZvmeIQZrspUMP9xVct17fbYwq0YNgg/P+azC1vr/5HIM7hexW1mukijlpHEtByxypSmDqySOySfn35DmZtVSGyOHB/MWtGuuvZ6trrg7zqom/SPfJPxzG4cOP4QT6q5xsdyCoEj5nXW/VKyUTb3DkpUaHO+1MHLG6AKhJEfIZ2YTM/KSjQLGnlLnbVm4Z+c0FF3zmgX8q0bPic35TUseaGdqVzB1L3PUETqjXXo9Uz1jYcLi1OKiw4fTk2tasl0mTqdUxFabujs8i9xonJh0YKm5b42aun6MLmnR31JVUydE6q9z20+3CZRYvVjfecK3iVPbizt3UGAtYYez0tvGh1rH+ndIICWKNLeICjJPNenlhwTN+kb8C0uxJZtthB9ujsYRKlPfNEKfEEle113rxMhySOcXOMNONiqvJBQcMvsfR+DYeCAXUNQTaFMtzMw3fGhrmHUCLiRIld1Vxpiv0RL3vMjW9xHSauA38jbIemveCcbQy9qfi3QnOEFRJfF9ZaXM7dXXtVC1ZzxDrxVDwzBYtVqyossqdqvT38jgq+FVbN3x9Ne9oKrVfA/xN61IATmLa8xy7zBWxcxW2a8O8w6k7acI38Ea/BacFRwCCc5GjTBiG2n3tmgSU2dbKr2Nae/fz0YkuXGU4DnesOzfW0In2CY/IROq41wt1dbadBlrottc2AU/brQFN9rzzukQJ8L+j4mAuHbsounSzIrmEojMI4Kjqd5K3Qme34/svDccvlAYL8N9nFNF8V1bgdHbN+Y+D+yFqyLy4MsPvnsYmI28LNGyUu11S4e2UDV6yKhqba0nLzLIuU+6NQOzqd1iFuOgaN63pigohVfA9sqAuzKOarbwhuP7oTcveiMNAEU1z/E69kG6ATHn+/7LuWM0t86DKvTgH5B5ZqIAm97aPxCCf5BRcz3FBMt1FM3e4F1oz2+54BYgIlDcbp57b1bm+93Ht/YvLnx7u0Hj69tXH/nrft3371z4/H1u7fv3udsuZb4L6+2gIySl71WFGs1QxP2DZYP+Eeup9zWBCrHFxQh6e+GeNiAeQE8CJ1xJA6Arr4M8LCeMBhG9obr1qA3M3oNbLJAXcPt2LtTJ3RnylBcfePYCw9oZzod+mQ8w5xagyqArAuXAZSG5YnjnPgLylbM1Nc1pndvKQ5sxXywFNm5mmxZElugKMm/sFs+En6GCqf0Blsb0Op6USE5FJJh8Du3cBy6+dKJjsd9bbzOoePHgDI/BqqqFUf1wyCGVey5VnDghaEPg2UL4p6DNzelHgqFHkj7mTOdarWPp1thJNnWX5sODSUhTl6q7cfbH2xvbd55FyX9EUz/KtJAAz9sbfzo1tatH29eu/sj+bGdfLx1J/ux0zIfDtCf2Cy83rhs/dea1uPHmV7qZZU/vpzpttgG6E+qpnzmp4mvTRy76ImukTuWtXtrVKydaOjcenzn+vWN29e3AUaBl9VFQ1LSyN6KdlGHSNd44w0LlinNW2a/EJojI5svCbdSXwq3XBIl6EMZzUOvhIi7Y968xPTk1jYPyG29G1kl23Kp32xN70XMTkQpCa+2YbKFEW328HPnDavTYqd6MG5Y3XrBKRrEz6GXzov2srjIvVBDRR0pAVn9uHCMTPTDY1SlL+euJJ4PwlWGUDRXAKOUHaNdIThE24lnnBQHPAdE+MTF7wd0Vyb+Yu84PRRehpFAXBwWkYAT3e7WX/Dyr1KL3j0cVUiV48VN0Y/t9+89vnNXLG0YKz9v3X0Pnzvy+cd379/YvI9GEb+5eX9ja/P62xt33tq8QSyhk2WSAh+pOI0cv7bQ06fZa8B8rEPycRqMj3MwFDIBlnVk0SoB9yq2yU3dQdb1p99IjWre4UHH7LtDavTG6Hdiyfsi3idmQRs4uhqHfQEiU2fcBWp7m+NoGnp6g1wdQCmCso8nXWBdvZZJoB3iRKPPGtS90BN6JR3XeER5rNHmGHVkt8BmM7Zx8ADs6Dkr3op+HAQjoXrOW5VP37rmhHMDK/x/uLN+iL7MIHzHO64AOjtO/DCKi+8cgK7uOAf+Ltk8pDUi/WUUVt4TDO3UKb+fGxQuOyVJLSAncgDJzozMDPvzDnnRqhNBsv15Nq9ld1ZTQDSCC4EF6AFHoKbujpP3xckSe/FoWHzTmYyexC8ZPpn6NnZkjhLOvA4NjzeqdPEZ9lHl4jNRDv+oY5l2zj9+vHX3xubjx2hAoqemnvr21u2N7W3+KC23nfNt4Zsp2EaWkJWnIhHY6Qs4lWfyTF1ACS6shJQJNs2Y6hZuIZaGo1RmrwVHeCD7YcEBcfoOBHo8/Rq9VeZw1tOcDM1f6ph0e20K6sE4su++o79lzw/mAhTepFGruo2L9FaQoLG6i21mzDMq9V/b6qGW26C3QZezgZlL+mP6hGNEfVYeJPussl/QnRMnK30jwvv6alHV7P9JUHCnV47ZIFi4ZZw217syG81UiBKrkK35IR8mwOO65u36Y/5Yw4Fnz29Ot0TH22Ox2qzjttXdyWGt+NI7K7kgfB9BEnN8oGIvrv0gPFaJCPtQcQqfabPHgdpcZriG8KAurwHUR6AH0q/BP382yPyIO8E9KfijfrBuIGtdFkjyRtxi9k5VzSgvZSoZdSUDHxmdumgDYGyo8uWSnp0l/6KrrJ4UWbwC//2RSwcchrURnZgycvN8PaLsWqsGH2cz5b5Dh1r4wI8x5/TuGDkz+qx6IN721wsr4B27N3BP7Fpq1dSYbdTFkej3uFRNAez6nC0zuwMVt1hjX768By/TVIOJl9/wNqXM23BKuT8zO9RCJaLLZI+s1praWlSp0T5pWtgi8p2+dtsmRtuEIlYB3UlYDdtKnpTiNaN50xxRA7O7HjrTMcq6Nes2/aoAbV8C+hLdjvwxtsAaKOrkMkKkvcGN0xQHcmc2h+5RNbHoGS0aRqnwlctjnk3RlAVll26N1oOWdIWttlv4qtqXZ77MlfQBvFpu5r4nTL7GyB5yfkynN3mMBBwSiMsKCPqzRrrEE3WcApeSd5o0RIaVgvfE0IXLRiEPlFFzQ72q1Y011ObDLG4Kro+RzZr2NmY6ODHeV1k5GpGJCdx9p9jUj+cMWOD9z0ZzKjbGKmIVpyiNOsQFEYfExbw+M8WHYsTnMtFlg72D7hb7/nTMsqCya1BmLSj6qHJ7oLpqsOjsLblMEFVyIcgLvxsaQQ8Tao445Dm0KfZpzOc/KdT8WFcX4k8Kb84cnX3KGt1fT+yO9nziY2jjH+U7E03Rbsz/k22c7LWbzzMH1Zn3K5vPYCIA6gUaNCtBJoS6fEiGyIgTs+cfIQpEk4ixF7nelxqZZ0uMOMRiy0E9H39WyRDF81O3xbWdBlYrtF15/mbxDQPo78rTfFH2JXUI60n1jqPXQeFzPY0Yp0+I80gvyxCgboB/Tsw7Wk3NTcJgNwTcGloUNzYkgxZ3PjTU4d86DuTdEzlIMtyldPmk9D79PCayJ4RcEE+g5ZWfporJSgUEjZ/Mnrd+LA3ogpypHENCVh+IOvLKZtptTJg0rDU6RPGqQRSCtKXBCjmG6Ba/soxd45i5XC9jvpdFdxuhhkiPuSQNef4LneiuL6Bs9kAQokwuY/QvecURo0XSQNCwhnSDZtH1mQZlyqR+acqV6bNQtugi7DnECa4GOu0pO2ENy5nGAVAo3mAE/57Ml5dsOqO1HwMZGU9txqMevCO0ulVCLZcDa3j+81R1JiwQa7FNA+0V5ySb0KNVeykMvBTsA8cHCErdjkXwe+jEI8+rCJtfFnhOdsVf1WQTZ8T9+uxvTp+f/sboTKyMheToSMGp8lm4FZSRjEmbOfbpz8A8//PxkWT0+nlXAGB97mX+H0/CStn+/zIRSxfEqwjeJtaqEhdXTYJ4fe4rb7mN4otvswd5eHjQlzrFmC00SmrkN0IPGgRUEDCYPaOALyBu4CTSxmkOfknnkEHtmlsBZydQhZyCEo/P3Ak6aBtjXXRia78rOXUl1+JjkRN/GTx6fDbxfc+h/P7LlvZIV06Kfmab/9lIajUlux+MBz6PppCRVLXFi11++bLEZVVhGfPAt3R+Mp5m0yqPeIj0J31evApCh91thVPFyf5oXGTSfw0ZjrwdIJXz27d/ZDXFZta+/QH8fhDkGLrYbqBXvFnjOgWlH8g9+qKqoQjvbTBPvtqhgYltRpeL3MNg2LWV5HLmvTV5HMs8z2zYpNI60e4ySUCefaUJ55+7sbrzyR/Xui15UaEcWsmQ8zeUIIkmWK22c7EmcjzsrWAaeSICa71h6Y+UEV4X5/+lpq0oH4MCe09K6DJNIBpFMS0mhGr/SNKk9u6D0jOjEuyiSlSjn/VaG5ogRG8eTWpNwPuC3KJSr0Tq4iWAd4GxzNArcOkbXj+531DPH2TKfqCV/YDKZrsWW17imF3I5zRsA98l+Dd6UUn/l6yWvTwoKpwBgAuvG5jJTWtCe2QYkKv6dK0VrGe028eTFGNJzme9j8mPtQlAqJ/aKt9+UDdxW2gMeK3cpVBP7VeYGJ3OBG09tfjJo1vxwArdWZg/tgL0Ez7MoEE7UPHgn+vt60sbF683N1dXN5pd1/WaG0sb7eZy6+ZqZ6PT2ti8uYLHL2gbpLgfUFxwJ/4N3xkGu/evv8+7U0r76nYGq0vtxeWm21vxmt3Fi/3m6tKg3fS6rZUlt724NOivUtiOHOADp+/RESjYkP7CVk+3olvvjveBiYx1AH352bqFUDKEhazv4b0Q47cH3ra/+4gyUilxRBzKwDsdcu4dEmp8TipCFNUofbKvntXJE+EuvANw8IC+Wa3cAoZ9xC358p2xzlvZOhhop3qT8oob7gFwcjW2AW5WS+q6h9eDYN83V3137HBlKltaFIYkfF5ceBBERcOQ5RIgigpDo0IbZwqv3SId91bsjaxJ5BfitnJhdbgiAaNXKKkhToHY9oYeKbmV64ppp+PiHoozdTai2rvjkTMGjdolOr997/3tOKw/UhGB6COsUEYTFRtEMCs0ClA+8IEfzQUi1SgkjH2Wx7edHuinczWL3vQZqJy/VapRhFDejFB5Tjdc9x6l4aWpjU+YGbjOZAZhU644XeMxzwiylY2dcDQQ4dgLi6Dg3HRi1ZiTjj+sXfinoEHPCbkGuk9qJTODrh7JcfixUJE1yYzF1c7yjfZmc3OlvdrsdjY3m9euLy01N9qbneX29cWVazc3X63MUJNXKDJocHjy1oOAI3HJAHt9zugn7PX2fFd7DH18JGoSpScHRaR3j+TOPKxIuyyB2W7k77pjfGxY8zGEGWxGO2GU+xnsOsGWE+3rsoReckkzP+cTvfJrharv+RjUxNZYqN3F3cDFVKMf0UQnkFU6oUmT+9IK0CkifUYT0/Pb4nA5gJZ2qwJWAWOE8mRBoiNIyX8iiIe3xo+yZPDw7jRuWEXzoqhVTE2KBg7q68VHBQk4KEwsYABS95LrSzDHy6cInTiFjtj1S3nSUGt2h4jFmqZx1VGHzqmJRkcaVLY1rQF3kFgfW62jLv9ZRY9EYS2WULFJ6ugpsGhCk6OPhm8oKG4jNZ+vQdUplfFc7tRh/lZXXZgPheVSc12CJBr+s15bxmwevRdAd8FAW491+613b90ouY5LI0of/ikpWb5wHGT3qW1AuFZ2BQNASEqaFkQgVLey0qV3fj2peN08uSewS7QRaGFRGLFVL7ja0oArYDsFxJxWOUIvKqFnzK4wegwjOyMMaP0srdK+VmxXHipXsLfU0PCrTvvXbdRKV7FrOy8GclM8NyI22zB7wRB7co4t0jQyBXTED8m1fI8zUgQLkNcAGW8Y1BAy6NFscxc1ciEJbkZzZd99x7pKhfRejEl6xvNy4H8gp7Lnpdn9aBIGPzm/Zp2/dA9+oEdv292/vHM+SYO+s/nAhnc75ylIdkke/Ul3bom42SWQMpMpnSR65X1/vHnkXVrQXolC7BShTYroB74y9uLu6qWF7GtR+rYz3hXZUVdW7MVLC/oLUUZut0ECvJIZ2qWF1FdRAzfK4XM0AamXr5L+LOqAth0PgnDEgF45Wu5eWsi8k/Akx2vgVowrdMdkRB3YoA4ASJkC+Xpyz9AVZzKxR+IhVVGVkLgHMsS01CvoGAa8y0fx+YbXm+7SNIyDMXxPnkWBt7wxXQIm0YWxH9GW8ZPECt+6p+EQsCLeZaaHROeV0gNbktniwqIFUt/Gx6lO5DtRZBvgGw5hxctdVkgnU1jh0RVvfGmh5HOCfA907DQVPgiYfnGVXSGljeZgRkFaIAv5FXIJOXJqwdzjS/vuewMPNHC0B8b94dT1UitP34AADEQQPxRp2y27c7G1aHfx7G05SwiC7BsK4RTC14W5uoTFroZmq6Jibny8ezADxiK8uRfiochUKo7gPZ8ApDo29WhKTK1Y50boHNK9Y9V6wF2o0mit2sWtuxhinWByGh3m+QKV6IZV/qLqX1pI00GeLDZHPc91PVcSq9aL2jOIMg5YwRD5VOp9QSdGhv3+Qee237vyWu3e/q6a/ceArMeS4Oo7O0O/t7ODXHq5c2lB1EjVx+0/LkUpAJBzQk9987Uaf61rh6UaNtOA6fRmHaRKRRiWTCDQcXwzWginYzw1JdrZAbJpAveG9vi45Z38sX7cCTeb7adstFSgXmE8PTxDNYGAwJkNRjlTSQH2Zln/b6IW+eabQpaXE1z1aczR5OzyC68EAHU00RxAZOpUB4SnONeN6WjIoqUowkKi5vU9r78vq9NdaFDzmoe3TnNB5KXi2FkAmU7f1WaO8mdewQKkc9wu6wczsocAbIVhZI0D9KzjuR7+2Eoa1hE3C5BkbWS7SnCW9LNmZdCtOpJJQOyEwdQDaGrP393LN7yWgGqtmxuUiic80Oqi3XOsJOuqF6nGV49GQ5m2z3IPmvHEFmzkwfGgubpz/iq25Qg9xpItvJepB22NcWZB6V+L+nveyImaIzlNzX4wWnOikX3Qlgq3bPCWiwdvx8dpQPD/oNGxIKmMek+XBIvxgj4QUWJPVQg6CbFFXn8KVtWxypO7FMoUUBT+QLugUFVtd1G1m25J7KIPxre9A29oDfFf1CYizvAJYZhTf4NuB4bXpJRp5IEzagBKjmAhNYRLCwob/NxHpTL2e/4QMTx7IKnyyWThdCWaegJZNJ1QNqJ7d9u65ULLT1a91mDFaXeavUFvsdkdeKvN3kVnqdldHbhLLae95Fx0TnTqX8g2fGkhBQS/cz3UUYE0lX4s38RSwU6gylEWOmcAOJCTi52EqDQeItQ10HNGwbgpj1uDoglNLiuanHBCWRBuAD36mHY1DbG9H+I38ibiXW2YE47Vli4u9Za77W633x+03QGUGQpNXVRJMGEeUvKeBg8IS77iqhaKEi3oc27QpxQpfIOF6QgE7PAy7uGk2by05zku/h15sUNXhIN6myx29UVgqY9n1DWJWpAs8Z55gBA+uE7IxvOluIodFAv751KEx3XBj7WQjkbHITabuLNhzfqBd9FzB4N1eDGaxpid+QNn0XHdi/hm4PhjLLPsrlx03HWu58CyobdLvZU+1wz24XHRdfsXV/Cx52Azg8Fyb8UTlYb+2Fuzwt2eU2svtRpWe6XTsDpLS3yNZX1dFGlGQAV4kK+55GJdNEfnRKxZWMUJm7uh42JcByosud5ug2uvYuUWVm7j8Tx2p1UXXzrLfPpSu8UfunWru/S6+IiF21Bgib4tduqyz2gS0hnD/WnP7zd73ke+F9bsxS4Ut5eggr2MP2koeMVItmCnQ9fR2YvLVIrbHARjOiNg29sNPOvdW5Y8jtES/jA+cjH/GYVU+hs/kRuedsGQvdCc+vDTGUdN3PCNOScnO+MfYi5rcNSM/I9oQD06urQJryi/5+GeD8rLGO+DcRmINQvtfeucL6RlTOWQznETi0tby+lmjDW8IGCPzqRcQ+y+vk65kYNhcLhmcbPrFl6SAeCg64kbptZEO4yQdndytNC2u0voqK8xmgCxtC7WxLtYRVd76gDINV7WSA7OMCGLlVZrcmQtLuO/TgyAvW614R9JDUhbRGoXmRw6dbxQLzk0crnzutx+mW15lVruypYvQsvNpGVsr43HnUnyLWk5R8qrLSLlH7RWWr32AH/020uLq9YyNv+DdqftdUUaK6JOnLv7REeG3hUXpEC2wrI/3gOiiBVek2dKG1uzJgEFGaludLBr+5h2iWeSwgx5I22m8WHgD4dyPnEV79MKoGQDOnNTvm3KFuxV9QoH33cma3xaW+r1TwIkK/F+MPSONJrZGdsgX/ByEJHgtgZQHOGman8MDJaoUZHwbujDB/wXSGc0wQ1HIH6H09EYGEnnIs7gyB+PnCM8wL49wGsZdVwyS8H5BN6xTGxrNYd+AY5eLzevy62ERbWhwQ78/2pX8KFGqp8us6BunTuyMcPKNNqhN+DBxsGE/oY8LxeXJ0fJLHVb+PRR08cEnDU85xPbPOybWhQNaE0qPOIcaM0sq2YUdYkJ7q7qvS928YlZDTWoI4lnNLW8SRjVc/NHpwY1MWgCs4ZyCMkTyRm5GVBcewm7IcIXI0r6sUDSRA3uhn5nQF/bQ2aVmb8CkbWcsCMQdINMS/aRsa0feLCAuwNJuxh703EfekMy5U1McwIimZg14hFoBv+h2c3MC/7bdCmMR20yhWfZZCG3SUnALpF5d1lSJQou+LRIVLm0RKdIsOgQ5NIGqKJgiBFjM9Y6+RXDSKgMW2oFLiqJDs3jUmKAW3K99IZBL4VhpwfgAWElgEMfU6CkJRRVgt01Pcw/jjQ+Qw3ZTkLai7yWEtqmR16GTeD4RIO4cAB2InudoDIihM6ciJB+McTPcqONo+wqvHWX0nIDhgcG4cjhQbkg2EGodSML1Q5Yl01OQ8BbcGPPcjB/ZezE+lB6yVA6y6mhiEcxoU0Qk2oszeUXGUsb9Z6LF5OxVBhKe9ZQmqGHVgIP6S/2veMBOpojUf8JwEvbBKAXDKwIQYgcv4Yz1bCaS/BHHB9ea7MCSpsGiByvWD9cGwdxjXBVL1iiWHgY7AYJJi92dETyE6tFTUEL+GZA+UQ41GDSjPYcMIJqLatFq9k8+7zSVH92D2WAhrRREE72QJwC1ph1Iu7qxOCAzPGieoWJZgATi/K0TRyE/s3ikJt7YoEqk8IhI8vGJRwC32/zcIKJ0wdzjzj6CWrReMySfNdOFT0hjdDUaFu0uSTKJWOlwPETqQ+4DiAsDJHbLV5sreuvA0pRF+9TFOUcWvbFDGpWIjyK5tAJ3ShPQs6hpCBD+600fHsOiBUTfMtm8JZN0HWX0uC17c5SBkDRHXJ6HYHafHZwPtskErQOJsHEslXzbMFgB11JGunBY3HQDsNgZJqmllolh8A9hRop5O4i8QZ6cShWwHKrZVoAQy9GJothURJodovURw3o0I88mqPUnF3UQGYAoAmpzKpOF7FToyKB1WLSn4yfdUCzWDQC1LZbS2YsUnGFRn2FmFjSBzW0dhRqDyagR+MCH/q7YzSTBizam5R1kkFoWgPwx2RCsyLA9TOK0i7q2DQLSp9YJj6Q6GZKJGozKlSrjk3KlVR7+gN3ZTDIqRZKTC9KBba7VE80v0RHYGQiyPUq2O7o889Ysl266E3w3xWd/a4YRkRCXgdVGJfo5KgXK42LdCfKkWDW+KzBYAf7Eox8y8E+DVxWBRKA/0PGq33GlsBSn0TKiOa5xZ3360bJk7VonOoUu6jjkHu1feJgJh1JmRVS60npPC0TzWQVhEKrdmVwsTdAq5adSAY8IVGaBeJqZrJiE5PTx+j6BzhESfJAxdA+/ssLKMUPTNQgLAac+PTwU13YZPwYfBSZYnQGyZNCJgVlga95BWpHMghET5f8GgQK2K7Ssm4J+vRp+6ZpZoV5nG2jmi2hGrfJ2NWo7gBegLqSpjpt0WIdVOjSlfCN3VllnU8UL1TFNTZ7QN1X5LI/qqGCJbms3goDRPJ+diNN1O1lK3tt3fEF/7ec4ZgGoUjyKSsCm0IGcpu9rExbarXKV9XF7KJyFld7tKigjd6+HzeT2k1MJFqjY230RlOvBWlm3Uf20HO9zJA7ndwK0gVu3wndAkrOsA5uSJE36wp5lsrH9ZvECVsbK2hyXmylDHUpoUjllvp3bzgNax02A5x4ikkvaLC+nuFEtFKQGxHPSPyFyT8tu7UCNk2LQObVxMUaFhbpCL+1QsfaWo/issQCyLG/Rkl962UrtZXDl3LWJSgjBJoWzkzrf0mzsDvoFO+0E3a7qNxSOYO+tVSHEb+ecWoq/2fW9muvSme6IMuRE+3nwfkB3njTwgxVgSB0TTeKi62n2sOYGmESFuARuh2zL70jCsYLku4Ni3ixQfURArpHFyoITeqEGHa8p7vLZ6lg7ZTN3V1OkT7xEaSn5UKtLKdvSdcMUVu3I2i+YI3k3DLdEqJXZmly0QF6yEvWPCFD6ubksG1nxVPev4WMp5m8xjy7SeSDwD3EcB+xSg+p+TB0JsZAgKUJqjAeihZpCtZIk8kC13N9mjJVaxjD9Ex5r0Wz57v+Gnob/THzRFnZHgAH2kuLMH5lL7MIy8opWWGRjF8xn1KJXnWcFroPs9pPC3Ups/rTSbwVwSSOdLrTNMJuzv3QVRoLn7M7D712slTHAllz5yryXUHXZKFQyM9KhsC5qwRM5UgtVKx4NHYcaq7nlCum0zUspI55IQnDBaODLfviDLtFRCvrZj1dD1YU2RadJTIuEpJQrxJ7oyMd1clI19bofvhqIoRmfzFxUNJPGYRJRQiMHCdnNf1g1b14sT9IDwqbttCtkFLCG+nBwsCoX/yZmmOH99qnRib9k+00PTzECGizj/lOnqtSUx/NJJCiekw3lWyWpcFqi9Srbme5M2gl7m+TvpQzZ7pyPVNcut3SvbsVAU2QwzPJuEnNjYpFlDZmRDi3yTSQtGC7znjXC18B+gaDFXcV0cdRkHoZjjod0BowqL5yMYWicRDnbCdpsRXLa+Z/irEMnH6abTL7Y063auabMzWExXTrvQKCzCv29OYA0Qt/x9MRaHV9oCWnNx0CGuGFWCq7eO20JlGpL77p3jgQ4m9Glv6TaRT7g+Om4h1k6HkYYM2761Q3Nm5x1AGoOgkUdYjHlUNdhWqRkY/rfohZXDof10tHuoTeNK9D7SIxVIOego2lHBXSpmR7dwarzIiFvFRoiGACfxTGa8JbAecsPCmYsQajovt86gWhkQ9qzTbZtgUaimySWUdpmyJUcHEp5UdputNQKEx2a1UDUraiW+Cw5JM8BHH2tqphA8rmZtqLvaWL3vJ8TJuiJMVMW6PNxU7O1KfVrUFcNBnKIqWg29iLIgxJ1dN15xb4ZDGymdJSoclllOVSR8rK9RwKW4xCDUONAgt4MRfU06lh3zv8ERDXKrRWN2kNaTflbJzlRFYHhyJrsbxK2C/InX6n38sWmIeGBoPlAaZo/MC92Bl0Vw2SK4neZSWXKU8AoYiGPvqtlD8xy+zaWWdSImFCf/LSUW1jcCdZfUtLMwK9ftgfyhBvh1KcFlt6xFJ6AJZx3ay29Fw/zrXTPQYr5HZJua8nZE7pfsSigB1jIxvxTQKL6QilQv+es++lrTh+BVIgyqb4YXJfawX+acOc2hdX6sawj2yyg/6P1WykU3MiLgoX4iIWXMJ/VkpKN5dEcXK0LJeUXEpckzhKf3eGiYen4iHNpzQGsnP7cTq6t9TNcbcOOTJRPCrrobougx1EI4cCXVo3nW6pMzGa9kp9NTM6RheEMgnVWPfEZmo1CpOSsfrytmLdpOpIT2qbPT6dThoqX5/AHp5wl82uTFwqJoh1VisMr8W0d76RcgGrMEcupudNPAcvpWvmeOSilnGbkwuw2FvoiSj63u7U8TNCW1d2VYoT4N7xCSwuDP9y3yodRMMUx1Sy/Pw/COrOoDdYXU5BbQpXyJEIXqXhPeHk7ba9SJ5myaqieFjm3+Ei83tyUs6a7rwxMNGr7ff1NKJ0FlGhI0EuEbQNSr2Si1WT/zQ3i8nASLleUio1BXZTgd7U2KKD3eKYkFBxFzNY0pTwhq7ud3PRyZRSoIt5mo50quyihMt2iDMXxxipAE9MiZqbLjuXbiniEc0223ZlU9xRE5zRsakKKKkGJ2iSTZj7qDOFCQYaV80cQV9yEwoRZnWDMMCLhGqLlIlbT5barMisKiIQnAtO8c6IOf1ChWHuZUOvGZpsp2gyyVdECnaThAJWkzmjoKrVXlGUoxN6n1UN0+KTqXEp7sBPSva1WPYpiczt8UhL6idF7b4xBauzas7Bove5JKxsulE7m4El+tp/+XSvTE/LBT0NOBEon+zVpUDjqinVazmb6kXJZUkIXjTdm4ZRXJ4b0Kq6uLVcFg0a7iGXeLdUkDIlASpKPaOcj8Q8uYiLtkDlJ00sr/TjAtwvTXRhfxMZz/xTEN+STntLlTzkFwe9DvIsHYwU/0JYcshZNuNGQt56vXjpJwmuGKCra+aPyrqcldxQY1hQpeFfx3XZDlmxJ0kOhxeC9MyQUprlcQs9R3JOqDGKdg1GeU6p5t04aG21OZ27XRztz5VdXKrnA0ppy5r4bbtriOay/SOV9CZ68IBIxseHe17oYemjxNRZSpWVvDW1s0mFDRFrnj8s36ai9lN09L0WQqsxqT05xIElsizzDZaWhLczn/KwykaiAIpzljJqlh7ZdFwPlRk9NycT3XTcgmRHJphR4DpDLTLXaqWmwxyb687MI9N30WCWIXpc2pxuuJJ4H7qMkGUijtXVeuV4XlpKo4/ZWm0ZIuGEaD0XGxWTbi7h1oS40TzpSx/U0Buv1uPFZEEyfvc6M1KROq1i/yQ3Mcm0wO7/Ql2IK8kIhB434Fb/YuS5vmPVJiEecxI1Q8+d9j23OQqkjoDP6P/8YdoNk9tqqOvX+X2IlpYp3rC0tOyGYlH95Od+eVb3pQW5U/bSgtyyi5tV8C8mEdJB0pflpZyW76rfVy4tQIFsucM+fhEbo6h0bwuvB7UomjbEk1zh3ek/n355+uzsk9Nvzz7Du++wDqo/shUfN0UD370WHMETuRu78D8sRukM2O7WEnC2vXYXt+jDKA524V/uV/UvmzuSkPfkTQ5paP7h9Gly0/iLwDJatpYBGvjfVnvVoodVE1xGjCF65GEFeKWG/OCEcbIxXq9Bm3wcNQP5bz1tdngHvTYepJf8kJaW8f8lZthbo4KeyaZ31xtEVy4xb3pLsCZGLVLHUfsyXV9vHcsfR53LfLH9sfgBuMXkVSZDUShSlhG8QOsHNAnCXrpoO1e06ywOvBYjOg1TArEJ1mGvGNjWXMC2nc7qcrcSsC2n3VpcNAKLm+8HkX48BNipakYJs9gkgSZ/kHSBh84izhqLafUYYvklIEHao0pnMQxrPxjS0SPMDBCi5cHqMmJa8gfBkuETcHl58gUBxM5vBVLIJ0j1CSqkdKtPcLWpbzyPoJN0PeZLmFS3BMpuAklTjoQaSm+Pxb5QHMIHJSPoHZmzzYst3GmDm0DqKXBpXUpgkTNCfWIZ7eUuaEcd63anBfTe6laCq9MqgauoV97JLnptda3VVes2/m13u/BjCQFftn6cYAm9IklrzDcMSx93gQBd4pEHlzCn64o8+IAeUmterxYTwz79p9PvTp+ffXr6dcM6/R6473fw+4/w78/OPrFOn1v45fTp2c9PnwN3xhMWnp8+s+DVN/DjezxuwXpACRKF3dD+AMlrxcMVgjORI0FMzInhpU9cOqbt/QAjwsXS4eyLs19a2DMA9s3ps9OvQFz8El/x109hLL/890/+ddbgKQ9dQiUeMlD5RCJUgH4qALEd14mdZsTLH3H4FCB8Cnh7hng7+1xjwqpkW3XXxgMusF7u3ApTPTw05vS/44xQqe9EGTUyPJYDRMQVPlr40sjxkzFgCj2x6uTUGTphU00OMHw1N6Czx9MJMg7i9AkR77UF4PidThM0Am9d6qWOFewBkHttbS0k4sZzPYUNbPQ2vZAT/VuBzD/irbiS/p4Sbj89+wJeWW/58dvTHkx6hhTg3z9opHD63LaozadIvBb8+QoafY5UjMDjB8Qq/rWwsoW6x+nv4N/P7UsLE03EaaSD2cvp83b0r8PeUFAEQPZ1/lrfr0+f60SZq48MQ6IGf4M4fiFFaNFacTpWh3VYq9Ps7HWH8Ly3qr+1OgfacxN+vb2kPzc7H2kaC1M/JoyyxueHfPzGFVpv8F6tkIyqhWkvGOhVGheeIItY+j9Of49rhuYDtKycTmTEEGZ7pvAP3zMdcs6W7A4q4CWsKIaCoad/NmZzZdhAHCZL//R/Aql8c/b56ddILjC3SFm/AQL6OQzijxZN9HdIi2okc0F5fzp+FUCi5vq9IDu+RTpZJEZyzMFain/K7JIQXAGtXaxP7GkNBsnDGeCt20Qbf8+cIGHuV07/TvHqL7Qa7+GRLdkKKUgyTzpUmBKHJKXNEDAHIi6QDn/6N2ADnyEv+dM3KNUQP4CBr89+hsXga5qZfUM0CVzg7K9ZKD6HV98BY2BS/eLsr6EZaO07oN+vYOS/FJzp9Et4+ez0t/jSLgEWk9gy6ngWyQHLRMNK4sQNtZbu01WYCdc+/RfsHeVzekml6EUBULhewWRXXdySt8S/AB8CA6h70G6P4HOz2+yOuhb8X7MLhlqnlTbUErkvOxTsJTM5YlyCPAwcg04mE4d6VZJ7YQaFmuDLoDMv4q5WkXHcQaGQA83r2dmvLKArQbtIoa+GSE9/g7fLo0Ft8TXzXA3km5WsXG5G0+vOPreQYXBnZI0TdSPLIx0Hvzx7SQlpGsx3QqKT7Dz7rIKg/E8pGcPRDT80ycUKYs0sJDKLHiUaqIezhYU40q9MWqTpm6bkayQimm6hJX13+gdFlM9ZyiG9CU2czQdUXo3i+5Xwwfm4l+Jc73jeBInt1wAyKxpPCx1Cgutl+euNQHLYQtb6MrwHj4UuUbnpQOy8xv2UlgyZZMLc0ZlQyvfjk1sutXL6CUpZz6QXLSWjMSXpyuvwSD+y9kbelpr2lDKPP9mm/AqZCUjGn5N8/BS1kK+F3k4AFzZHqSwK/Q7Rqq+ebgoDcsEvBSkeJsbdsJzEXpAIMQ9c9oGz9DY9Xzn9v4gHIy+DVZFyaAJPBZzAl7W8FvZUGN+ANlQgf0ckBkzXrkDkfGdsmsxfIYWKC7OzFJriXftEZoI57QuaSyNUUjQ2Jyk6J02ziCGxUEXCYqtSvmoqIIkqxHyBLlgsw/7srOmmP/ajvZxtX86cVG2+SPuF5N+qtXTQ7g7b7eaKJsMMVkNqal6YkCwvDBNiEheYV6cmqFBATldO/xGlFAqgbwDun7P+g5ymmBtyAFZrW6iYmcYnGbPCongI+vSTAE8r65j6BSpFFnoX8Acue/RYsCIHhXh5ozr2By747OxvUelDLfD36KbAxc9lkPh5HiwwMFAZQgmLqtjnSMlnfwW//3D6jZU9mxhvUkEqQjvkE7RIWUNE8a20qjK97c9O85thKIItV9Lxlcp0f9/Du5KEiPlSKSJVmB0+oXOMwlpcJBN1wVCw7Enc8p2VnRTvw5d7HVFOSWekBZz2L9kUzvH4s8/QVujAUpQ1hXmT18PlDON8FynwDYtcVc+MLJN0ty90p+gXwsH1PZMWkd1X7FRTBPEKp7p/J0jWhy7TfjGn/tX/AA9xz2E3M9tpHQWP+qXLhOB3bQCMkphSrY5H/u6cn0YeZaGi0rO+M4aJjmLrNbwDzXfr1uUrlhv0pyNvHNu7Xrw59PDnteNbbo1vVOTy/0VcZvTu/du8Au85oTOKakNxTbDNd9RghaEXW1t3b2zS5fGPH+PPx4/xbNq3bm9sb4u39BtfQwW8qgpL2XTWUfS+H+/VsMzO+XpdtvRfELga0SMQX936+GML3dTCTheNUKPGVmTXshk6T0K100q3YF2+jEC28bvCDR1eSFN328ckStfVmqFr7hhRewHdF35I51zb/b0wGNFt0qkX9qHXI1lxteC9upVJzSbekl7rj9wGOcrrnLYJXY2gs7t0kboNsPi74xp8GbnWCRfEAT45qa/TfWAIW50gtCfwjzgNvzaCz3SH+Cjo79PTCWNDKw/jpfvZcfB4WQxOBVfHma15REjBeCvarXk2QagjBVH/xBLThYdN4+gauAufk04bFmY7RLH80JtGmNSMxlzD2vcxFUSb7QbJfFkWXu560Zr18BFex4aWC5qKorZ1oih4MIq3riHV9wjWHuC+1rMWrHaru7q0sly34+AmpufAy0vyZQsKtWEyWnU79CgfB8Zt85HLDSSPC3gpGKjAv4I/COO/f/L3REtq2qDXba9fi3DCInn7PJ42227wbwqhwXc8C4+v8Yqgf+o54ubPPqXGt/gUgiAIaxGAvdySvZODny8ngy+vc13xq55qAlEtTxtShG2YWbF3COhyrM0unR/OuPSGMBTPjvmec3H8I6IGeFrDSg5Epe7o2kCoAZToDe1kCxwPdz1pNcRGh7hCryFS/PEu39t6H4i7VoeZ9j/ydBSGNkUnYdptDv3iJe8duwMl8WZ0NcI+XeknOBvevAm8nBetBSV5UeOFGsSagI1iEjS8Jx2Iu8Db2eUL7grfIDiI3skRzbmVlKENbkBqHjSOI8Br40N+2+R6C1annlROamL8XKv4AVXEl8Z62CmiF1CGFwdhns42NlPzhnVbpp0JdsbXbiLRAo65s6QEDlxk74pmVSGZ7UaFWI1FiOG7Q7dZXd/zh0DAkzqNwkBNKsnHw6hiA+USUBOU5dVaI05xUk8xO7y0EKWTRnNj0B4AhtfwfQMEp3cAT9t83BXw19dq/LuukMJFLl+mmilyk1/pkE/4q/F1ARNyGyLezGdm+8E0po8R3uk58uCxxoMSzTGW8Fq/cOqtF3YhmwFW3Vlp8cK0TI3iAJJGibGt88tiwOkeWCrD6RjvIx3navF4RJUTgdaruLWSuB5hS+D4MugnfJB8Mk2xN6mNxSyp5fbTqRce8x2DQbgxHEIHP1DHoEFH9iAIN53+Xq0GwswXQ3Q1oOJgd3dICCL24/Mk4r4QUyE217HYJSp0UtdYiqvzgRRgtQ8ToB6q2PNrT2CA5x99WMcrX0QcvJ5amq7A5wN40tahjhYBGp0h2zOtCFjbGA+US6EHylNyWTGuGM2picV6yBOLC9TFChfXteFlkfJqkzX1Whwm+1ALD6IUk1E48Zu9rI+SKRIqohgSLb66VPLiQ88b4y2pgJZWgx8fBPpDCx/SWkx8D5j5hHaOa9UvILbJLQdyGEj0Om9JQZmBqZGqZU1kIsNJepkAkwrCEXqG7DEwDx5togzHeM0wfEixlH0lTfwxCmQsAIxWtFoHZru4hGdNH6R7TgAHiSthaybjQTFUa8MbqjMBaPBhv2Et1usJeyoY8TmYym26ObV2AIprQanL1gE1JK6r2ZAslm7eq+FgmanWWik17MHGW5ukixEQrE+B5vQQV9HheBg4LlOCMtG0JBggi4b1EImXDFoqptm0nCfze+FU+duUJZ6O/5x9LpuCAYVOX1xocfo/0P1OZvvXykkoDUN8IWsNhCOJKv3PdAiJ3AC/1bId/huaglDzEd23MB2nBv0KhyJDatTWvyR2K7X1SkeRaLR339l+7y2YzTfnT9XEtFFrCf7vYrPdSpxib+prle6Z2ybtuoY6uFg6ZGDhXU2XBT09xI+PQCOb1FDPRdn+sPWICR3JlxzSddsfA/N7+8HWbWPFh7A8xo+o9ocZzzZmkCF/3if+vH+ST1zCdNvXnhA6TlKBd2TnqTQrlYGlGfVkRH9Yt/HKiRqryFkxB+O9O67tp5iHT+oH48KmLP67g9q+OEqkXBYOLTuKU5LQA0PmJ0IUgmZlEHNinfyEeL1PFluZNPwJSENfSsOT1KyGoKrTyXpBekD9ALX6YJ8EiLjWR4gQcamPUA2R96NK/CHRXZrWLi7D/7J5j1eyuZMDTpGj3MmuSp2kX5g5SXeSihZeeyIg4gPv2g08dqC91G7g+XUCPLUrBDeF4J4Q/nZChJ3pul/WbyoX8rUngJGTfCrkYnEiZncV/kdpmB9aF9gjVyPwP0wnRu7LrMhFqHPRGlLWNCzSZme5CgRLZTmimUtU9CzNDwFbhZB0LfjfsLNqwf+2ljv01KTHlwUJOkab5UM970E5wZAck8X4oRKPSJyAYKuG1OaTHkEqXhv/XriQOEAc1EzJjgexSxL33i20AxtkT660UEj7YAwvolhud9eBdC8Q7epg0GYrze/dbB6tvfaEWusHUc3BuuHJ5Gi92TyWHyJgF8kHZeU0XQ831bz2pGUvy767WKxlt7on0bq2v+W1J/ixwwuON4/JBSeX34mOGzYQYO2mmOker3BtjU9wfye5yGtpljUm39S2rTwx5HihV+yAUUxbJbtl9Y5tsOBDVUxkttUT6+TcOWw8KSCDyXoRVQInGh/q8iJ2hjNiYxxBA8sO/tjicjh8xPLqGZmh9l1cY05SR8vdTAsfah1RborGFXqYxcSc/prDFcrxjPI915BglBokHKvLIlOA8qExOeRpNu33i6yHG+hHQ8aJbWnBWKj7jFzIn+WTiSj/BtsFVePnGFNo4j8WvfoOE3RkYMn+UEZF1oqBLIaIvatXgYpfe2KYTBg4QPKVcNB/BhBje1RUjelD4bbCwf3jzPyML1L5GZSxdvbXpKwh9jjA/KE2NenEq4LZgUn/p2Jc5ulCvNYpQCRAz9sBjKGE7EQXJ3KpiQzEbCeI+Kvs/awhZtGNVBfOyn8VbVB1Skc01c5Nm/6kN3SS5UG3iamkmVCa4TSsCTl1VKp8YiCdgxIpt83E6CMgQ/bcOQ2mRPUUCfWGQelEiLG18mWWp0g9pRP5xc8oKfZbmcYgEtRMuwmeC5zrTDrHwdmTPhKIQ2zgXZPBAN39wpvH+0/qFAD4y+27d+wJXi+J7nvyInEaVW1kY8WEv+J+TvYFAqmsydWtSQRobZQ8rTO/T78EnGlP9H0N3ojj2m4I8YD/yciOAFqZXwj3kyQvDKdK5JDlxY3e7XauW5iJb0XuFvCEr5gnkUcVfVMt4ccpWoB5W42qovdRz5Osr6dhlaRaRo8aqLnqcYFk1Ydakd3qGMlxXUGq+U0GZpxJKE/kDwoKZedw4hLtIFfQx0ixRjXMFHHPmgIzb7Mojvd+EO7Xkl51VFJELBUWesIRpQyVNCzXi/Zpj+q5c0im9HDbH4PJGU7pfMapJ92G+fE/yQxGEZYiE7GNRDpw8b9e6Dn766k1x/wOFp3ifbiq1tOMct1U08WFsUYTMaLDqFH5TdYlvlkvV9pkIebAPcFx1XYL7NbkSqYjrBNvci/tSu7l/MhJ+RPTOMhWxpFIg3rE5rOIEY0IaHZ5UV5dfhTi+oJ805Mw2A09jMWuJYxFOBtH9iB0+qj8tyQ/UHl19UyUJ11YhQTbHHN5XVskDDHBL1ZG4kLDtQHAgl04TPE5IQA5OASVJx4s9SsWxetEeauJnBSsZvQ+yiJr5ETVWYgRPx++9oTFvGjhBJNzfm8lbxmiE+tP+kvqon6ycPbph2iuEHTAfrCQyokW6qD1X6kaBjSxWF1pZ/qynOl4f5M8IA+VTyfB2yPLdt8U1AAcxc2MT3PB8ggJRYw2nrDcLJ0kPKwqeZmW8El6PZJTQxOeGK5OAjOCwNLygRcI1+TIEZJmBYrEna4tfUgv4FsyOZbUIifHUtaflAFXY4758JTGkZE1dnRiQMRv2+jnm6ECCKszyZVM22wFxhqJ7rNfoXW2rhowm1rbdpIcIHTuvFh8bkoCf8F9BN+QZfKL3C4CqdG/lDXDsKxhhu23vHMCt0P8llLqhKGjGXmnXwJgeeOF0qMyDu3PsBsu8dTWqE5ZTDIRs56NctJXmeSZJ31KatNJQyzN6jSQpiOphpgNeZlViDoF0wchvVr+rWHYxXSlFAomqpQnX2Xjlu6do3kvyteV1DJPjm8GfmZ+eFQNaDuoOpQN6MJlyilRybFE6r800AnbfHl2ILSyoSQTOp+lpetGlv5TKVGK17CD+rWayoulQ4annqafNazuUqu+XqJsibTbUh6tsmHzUypSm1iTxrRbFKSkeeOsiVzD787+Bl7/hq10oRcW8TqVR6SpjQLG1JBl9i+MmSA1K4R9SoAfYuy3cIRpNdVshBHJatvB1qy0OmvqmtNERVbrmuVE+/Q7rbqeZO3ZRJdPRZIYWZThtZ6MgPgJeTozEaiCEejPqUSxxBGQk8dK/Cm6myGFMUcwFbLWwt3rxWFamVzIYGi7W7IFnyTxWLRKC3e+oJ3DzC/rF8KTAmSBcaoxISfld+uEI29JhM6kDpUgXMvg/4b3unC7bG3O3AuTQQhvJJmje9nTK955ooGltphoUs4MEDtbcK219bXNu5zIWmRTR0YKRWQUVofuf8SOZueJPOG+5EpK+ws0kWhYAyW2ctpCJr4rE0Tq8+SdCGNaNEBJJXPVF6FK/CcVWZiNFzG4CbwkH1J6cNCuajMJRlTAdkW3FMVDTD4jKxkLb8ur1muZz0yMVOQtyLRdkyNCdZ1s56vWfUpPvqzogfKC5prPjANHG8a6HIf2EudMz95NgXGSGlGyNmePR4kn0iBUE4kSW5m6lEKjGkl03cqNyIlTbSQbRF6mFbFJpDp9FZtkJjoWbrBSx5c2P3TU2RzDGVH5ZHqr40MOBvWGekoV4XWRXS3pJFNVntWpu+PhseZvL7FaM240BXGBF40uCJjDi5aUF7GPJMSjNunk7SuRbJ0phU6WCFO01RABaO3hKu9zEG6cRDDRdpYqU1AIVZLlqs8u726pzn4rNC24yeyBX1WasxjwIAj73vU0dRgA2/eOs5nwRCGeDV8EgWxGfWdCG1MqQ13UHZ3GfRSPvPFU69KzMTUXSt7gKEtN7K/IbveR/rePBHHyMYV1unzWwmHgPh7aNwHQ+ONdZTBg1Y/m2AwgUCD2NCEWWrg0RBdPrKM1gDnqh6Ak/6hhHSdPH1gn69ATesC45evOBMjcq2E+Or245SYcpRwomXOXAopAeOMN65wYIfwkz+He8SSIawooq0nA2kcNDTTxDhjBlcuYtfEkQRN7OQRPUWg9Secbe2TPSArW8L2exTftTCkfHfLWBjZZXy8tJ4m6tCyQeazKE8JVBUoUZXJS1CS2AjFJiT1Kjx9HrP3U0i/ASn74CEg9GPeduPZw9EjHyH0sHwC5idTArL+AI51BHUtQum8SAB7ZuJGJFxge9K65DFP5LHJTl1LJsOBVwL8IiK0ZS6jtR9fXduC/dyMoLH/g343JhLQe+O920HeG+OMehhmcEZVLHEmouyY7thC8+7UnFsZd15JYa0Pf/oQ/G1YSMH1lYNQNAMjAUyPBx875jr2Mb8Th7+2Vbre11O6ga2WlJabgxDQPSsmup/pwWe/GiBI+3qCxZD2sZ5/nAW7g1dhFUy6NPiAL/YPJh6woEgmXNcQUa5MkM3D8oUYy+Ji4JbMQmJ3V6cRhPfFX5vRq+fOR3OKQIQneN0c/1qxI4MG6YPnWD2kGEu9WKgmuTUlwl5MsOL3ZJAimWtbhw8DWmkiVa9A51VnvgLYPVzgK8EYjCday5nJLzT2fl4C7WJbJ06elbCShE8I+x2ouJwQn5dLYEzsFDKsnhapUmO2kwTG9FE/AZH3gMreQ0x04w2yUAufYQCyk1g2BLFU9/wAPlCfulICjed7IKklpaBydGXv6rgIacYNfXxDjX7C6GiqzXRhnUU/P53kU8S8Z/WrZK9xLwxJdUlxvzbpodyk4RvsZ9UAOIoJWA8hHauyKAq9TERvCgwmsm/2lIq7xLAmZcNgwOdOQN4oLKlvjI8Uw6+Y5hzQ48/77ZOszeXdqD/Y8K5h4fI2gtedEVgwyxLVAitTtoplgVQDHddkyhGJzo0u+ldOfWvtqBWVO62yI9Ir0BoKnFodivs9GndRK00h5NhTa1gkFR2YDxdnnmtMQO1hKd/CSjEWDgCkSSBBIvGV3VoDg0mzmwyLY1jBjEIovtkWwerHdSm2X+FAArlhjdRQlOywUhv45OcEudfpYMgeLxZNgxELSCSOhTa0sFbciE/SVx00FkBqWiNGovct4M5LekqDtk4bixUYZrYycjNxM2TsFElP43sl7r5yGpHolauKlBXXawKUFeU76wl48GsKP8yf/C75s4bA="

def write_installer(folder):
    unpack_sources(INSTALLER_B64, folder)

def serve(port, token):
    ctx = Ctx()
    ctx.token = token
    srv = Server(("127.0.0.1", port), make_handler(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"serve {VERSION} port={port}")
    started = time.time()
    while not ctx.quit.is_set():
        time.sleep(0.5)
        now = time.time()
        if ctx.bye_at and now > ctx.bye_at:
            break
        if ctx.seen_ping and now - ctx.last_ping > 40:
            break
        if not ctx.seen_ping and now - started > 180:
            break
    srv.shutdown()

def arg_value(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""

def selftest():
    import urllib.request as ur

    ctx = Ctx()
    srv = Server(("127.0.0.1", 0), make_handler(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    html = ur.urlopen(base + "/").read().decode("utf-8")
    assert "<title>" in html and ctx.token in html
    req = ur.Request(base + "/api/state", headers={"X-Token": ctx.token})
    state = json.load(ur.urlopen(req))
    assert "quality" in state and "hasKeys" not in state
    try:
        ur.urlopen(base + "/api/state")
        raise AssertionError("без токена должно быть 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    assert len(icon_ico_bytes()) > 100
    srv.shutdown()
    print("SELFTEST OK", VERSION, "browser:", find_browser())
    return 0

def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--write-icon" in sys.argv:
        with open("clip_search.ico", "wb") as f:
            f.write(icon_ico_bytes())
        return 0
    if "--version" in sys.argv:
        print(VERSION)
        return 0
    if "--notes" in sys.argv:
        out = arg_value("--notes")
        if out and not out.startswith("--"):
            with open(out, "w", encoding="utf-8") as f:
                f.write(RELEASE_NOTES)
        else:
            sys.stdout.buffer.write(RELEASE_NOTES.encode("utf-8"))
        return 0
    if "--write-host" in sys.argv:
        write_host(arg_value("--write-host") or "host")
        return 0
    if "--write-installer" in sys.argv:
        write_installer(arg_value("--write-installer") or "installer")
        return 0
    if "--serve" in sys.argv:
        try:
            serve(int(arg_value("--port")), arg_value("--token"))
        except Exception as e:
            log(f"fatal serve: {e!r}")
            return 1
        return 0
    try:
        run_app()
    except Exception as e:
        log(f"fatal: {e!r}")
        message_box(f"Программа не запустилась: {e}\nПодробности: {os.path.join(data_dir(), 'log.txt')}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
