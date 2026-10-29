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

VERSION = "1.1"

if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass
REPO = "clipsearch-app/ClipSearch"
TG_URL = "https://t.me/ClipSearch"
LANG = {"code": "ru"}


def system_lang():
    env = os.environ.get("CLIPS_SYS_LANG", "")
    if env in ("ru", "en"):
        return env
    try:
        import ctypes
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return "ru" if primary in (0x19, 0x22, 0x23, 0x3F) else "en"
    except Exception:
        import locale
        loc = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
        return "ru" if loc[:2] in ("ru", "uk", "be", "kk") else "en"


def L(ru, en):
    return en if LANG["code"] == "en" else ru


RELEASE_NOTES = """- ClipSearch теперь есть для macOS: Apple Silicon (M1 и новее) и Intel, macOS 11 и новее.
- Установщик для Windows ставит именно ту версию, которую вы скачали.
- Мелкие исправления.

<!-- en -->
### English
- ClipSearch is now available for macOS: Apple Silicon (M1 and newer) and Intel, macOS 11 or later.
- The Windows installer now installs exactly the version you downloaded.
- Minor fixes."""
GITHUB_API = os.environ.get("CLIPS_GITHUB_API", "https://api.github.com")

WORKER_URL = os.environ.get("CLIPS_WORKER_URL", "https://api.clip-search.com").rstrip("/")
API_BASE = os.environ.get("CLIPS_API_BASE", WORKER_URL + "/helix")
HEALTH_URL = (API_BASE[: -len("/helix")] if API_BASE.endswith("/helix") else API_BASE) + "/health"
REPORT_URL = os.environ.get("CLIPS_REPORT_URL", WORKER_URL + "/report")


def scrub_paths(text):
    text = re.sub(r"([A-Za-z]:\\Users\\)[^\\\r\n\"']+", lambda m: m.group(1) + "…", text, flags=re.I)
    return re.sub(r"(/home/|/Users/)[^/\r\n\"']+", lambda m: m.group(1) + "…", text)


def report_log(limit=12000):
    try:
        with open(os.path.join(data_dir(), "log.txt"), "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit * 2))
            return scrub_paths(f.read()[-limit:])
    except Exception:
        return ""


def os_name():
    try:
        import platform
        if sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
        return f"{platform.system()} {platform.release()} ({platform.version()})"
    except Exception:
        return sys.platform


def send_report(text, contact, with_log):
    body = {"text": str(text or "")[:2000], "contact": str(contact or "")[:200],
            "log": report_log() if with_log else "",
            "meta": {"version": VERSION, "lang": LANG["code"], "os": os_name()}}
    req = urllib.request.Request(REPORT_URL, data=json.dumps(body).encode(), method="POST", headers={
        "X-ClipSearch": VERSION, "User-Agent": "ClipSearch/" + VERSION, "Content-Type": "application/json", "Accept-Language": LANG["code"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message")
        except Exception:
            msg = ""
        raise ValueError(msg or L(f"Сервер ответил {e.code}.", f"The server responded {e.code}."))
    except Exception:
        raise ValueError(L("Нет связи с сервером ClipSearch. Проверьте интернет.", "No connection to the ClipSearch server. Check your internet connection."))
GQL_URL = os.environ.get("CLIPS_GQL_URL", "https://gql.twitch.tv/gql")
GQL_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

MAX_RESULTS = 2000
TWITCH_CLIPS_START = datetime(2016, 5, 1, tzinfo=timezone.utc)

def config_path():
    override = os.environ.get("CLIPS_CONFIG")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
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
        headers = {"X-ClipSearch": VERSION, "User-Agent": "ClipSearch/" + VERSION, "Accept": "application/json", "Accept-Language": LANG["code"]}
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
        log(f"health: {e!r}")
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
        w.writerow(L(["Название", "Автор клипа", "Стример", "Игра", "Дата", "Просмотры", "Секунд", "Ссылка"],
                     ["Title", "Clip author", "Streamer", "Game", "Date", "Views", "Seconds", "Link"]))
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
.modal.report { width: 560px; display: grid; gap: 12px; }
.modal.report h2 { margin: 0; }
.modal.report p { margin: 0; color: var(--muted); }
.modal.report textarea { width: 100%; resize: vertical; min-height: 110px; padding: 12px 14px; border-radius: 14px; background: rgba(8, 13, 40, .55); border: 1px solid var(--line); color: var(--text); font: inherit; outline: 0; }
.modal.report textarea:focus { border-color: #7d93ff; box-shadow: 0 0 0 3px rgba(91, 124, 255, 0.18); }
.rp-contact { height: 46px; }
.rp-check { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; cursor: pointer; }
.rp-prev summary { cursor: pointer; color: #a9b9ff; font-size: 13.5px; }
.rp-prev pre { max-height: 180px; overflow: auto; margin: 8px 0 0; padding: 10px 12px; border-radius: 12px; background: rgba(3, 6, 20, .6); color: var(--muted); font-size: 11.5px; white-space: pre-wrap; word-break: break-word; }
.langsw { display: flex; gap: 4px; margin: 12px 8px 0; padding: 3px; border-radius: 999px; background: rgba(8, 13, 40, .55); border: 1px solid var(--line); width: max-content; }
.langsw button { border: 0; background: none; color: var(--muted); font: inherit; font-size: 12px; font-weight: 600; letter-spacing: .04em; padding: 4px 11px; border-radius: 999px; cursor: pointer; }
.langsw button.on { background: var(--accent); color: #fff; }

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
  .nav button span:not(.ic), .brand b, .side .foot, .langsw { display: none; }
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
      <button id="navReport" type="button"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M12 3.5 21.5 20h-19z"/><path d="M12 10v4.5M12 17.2v.1"/></svg></span><span>Сообщить о проблеме</span></button>
      <button id="navTg" type="button"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M4 12.5 20 5l-3 14-5.5-4.5L9 17l-.5-4.5z"/><path d="m8.5 12.5 8-5.5"/></svg></span><span>Telegram-канал</span></button>
      <button id="navQuit"><span class="ic"><svg class="i" viewBox="0 0 24 24"><path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/></svg></span><span>Выйти</span></button>
    </nav>
    <div class="langsw" role="group" aria-label="Язык"><button type="button" data-lang="ru">RU</button><button type="button" data-lang="en">EN</button></div>
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
<div class="veil" id="reportVeil">
  <div class="modal report" role="dialog" aria-labelledby="rpTitle">
    <h2 id="rpTitle">Сообщить о проблеме</h2>
    <p>Опишите, что случилось и что вы делали перед этим. Отчёт придёт разработчику в Telegram.</p>
    <textarea id="rpText" rows="5" maxlength="2000" placeholder="Например: при скачивании клипа появляется ошибка…"></textarea>
    <label class="field rp-contact"><input id="rpContact" maxlength="200" placeholder="Как с вами связаться (необязательно), например @ник в Telegram"></label>
    <label class="rp-check"><input type="checkbox" id="rpLog" checked> <span>Приложить журнал ошибок</span></label>
    <details class="rp-prev"><summary>Что будет отправлено</summary><pre id="rpPreview">…</pre></details>
    <div class="foot"><button class="btn" id="rpCancel" type="button">Отмена</button><button class="btn primary" id="rpSend" type="button">Отправить</button></div>
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
let lang = "__LANG__";
if (lang !== "ru" && lang !== "en") lang = /^(ru|uk|be|kk)\b/i.test(navigator.language || "") ? "ru" : "en";
window.CS_LANG = lang;
document.documentElement.lang = lang;
if (lang === "ru") return;

const EN = {
  "Поиск клипов": "ClipSearch", "Свернуть": "Minimize", "Развернуть": "Maximize", "Закрыть": "Close", "Разделы": "Sections",
  "Поиск": "Search", "Загрузки": "Downloads", "Проверяю связь…": "Checking connection…", "Программа": "App", "Выйти": "Quit",
  "Проверить обновления": "Check for updates", "Доступно обновление": "Update available", "Найдите нужный": "Find the right", "клип": "clip",
  "Укажите стримера, игру, слова из названия и точный период. Можно заполнить только часть полей.": "Enter a streamer, a game, words from the title and an exact period. You can fill in just some of the fields.",
  "Стример, например shroud. Можно несколько через запятую": "Streamer, e.g. shroud. Several, separated by commas",
  "Очистить": "Clear", "Категория, например Just Chatting. Можно несколько": "Category, e.g. Just Chatting. Several allowed",
  "Слова из названия. «-слово» исключает клипы с ним": "Words from the title. “-word” excludes clips with it",
  "Автор клипа": "Clip author", "Просмотров от": "Min. views", "Период": "Period", "1 час": "1 hour", "24 часа": "24 hours",
  "7 дней": "7 days", "30 дней": "30 days", "Всё время": "All time", "Выбрать даты": "Pick dates", "Выбор периода": "Period",
  "Предыдущий месяц": "Previous month", "Следующий месяц": "Next month", "Нажмите на первую и последнюю дату": "Click the first and the last date",
  "Теперь выберите последнюю дату": "Now pick the last date", "С": "From", "по": "to", "дд.мм.гггг": "dd.mm.yyyy",
  "Или последние": "Or the last", "минут": "minutes", "часов": "hours", "дней": "days", "недель": "weeks", "месяцев": "months",
  "Применить": "Apply", "Готово": "Done", "Искать": "Search", "Остановить": "Stop", "Очистить все поля и вернуть период «7 дней»": "Clear all fields and reset the period to “7 days”",
  "Сбросить": "Reset", "Период режется на недельные куски: находит больше старых и непопулярных клипов, но поиск идёт дольше": "The period is split into week-long chunks: finds more old and less popular clips, but takes longer",
  "Глубокий поиск": "Deep search", "Найдено": "Found", "Сбросить результаты (Esc)": "Clear results (Esc)", "Просмотры": "Views", "Дата": "Date",
  "Длительность": "Duration", "Название": "Title", "Горячие клавиши (?)": "Keyboard shortcuts (?)", "Здесь появятся клипы": "Clips will appear here",
  "Впишите стримера или категорию, выберите период и нажмите «Искать». Дату можно задать точно: с 01.02.2024 по 03.02.2026.": "Enter a streamer or a category, choose a period and press “Search”. Dates can be exact: from 01.02.2024 to 03.02.2026.",
  "Показать ещё": "Show more", "Выбранные клипы": "Selected clips", "Нажмите на карточки, чтобы выбрать": "Click cards to select them",
  "Качество скачивания": "Download quality", "Скопировать ссылки": "Copy links", "Выбрать все найденные": "Select all results", "Снять выбор": "Clear selection",
  "Сохранить список в CSV": "Save list as CSV", "Открыть": "Open", "Сменить": "Change", "Скачанные клипы появятся здесь.": "Downloaded clips will appear here.",
  "Когда сняты клипы": "When clips were made", "График: сколько найденных клипов приходится на каждый отрезок периода": "Chart: how many of the found clips fall into each part of the period",
  "После поиска здесь появится график.": "A chart will appear here after a search.", "Клипы, скачанные в этой сессии, и всё, что уже лежит в папке загрузок.": "Clips downloaded in this session and everything already in the downloads folder.",
  "Папка загрузок": "Downloads folder", "Открыть в проводнике": "Open in Explorer", "Сменить папку": "Change folder", "клипов в папке": "clips in the folder",
  "занимают клипы": "used by clips", "свободно на диске": "free on disk", "Считаю место на диске…": "Calculating disk space…", "Очередь этой сессии": "This session’s queue",
  "Очистить готовые": "Clear finished", "Последние файлы в папке": "Latest files in the folder", "Обновить": "Refresh", "Загружаю список…": "Loading the list…",
  "Загружаю видео…": "Loading video…", "Начало фрагмента": "Clip start", "Конец фрагмента": "Clip end", "Начало:": "Start:", "Конец:": "End:", "Длина фрагмента:": "Length:",
  "Клавиша I": "Key I", "Клавиша O": "Key O", "Начало здесь": "Start here", "Конец здесь": "End here", "Посмотреть фрагмент": "Preview the part", "Весь клип": "Whole clip",
  "Тяните ползунки или ставьте метки по ходу видео. Пробел — пауза.": "Drag the handles or set marks while the video plays. Space pauses.",
  "Открыть на Twitch": "Open on Twitch", "Скачать весь клип": "Download whole clip", "Скачать фрагмент": "Download the part", "Доступна новая версия": "A new version is available",
  "У вас": "You have", "новая": "new", "Скачиваю установщик…": "Downloading the installer…", "Позже": "Later", "Горячие клавиши": "Keyboard shortcuts",
  "Работают в списке найденных клипов, когда курсор не стоит в поле ввода.": "They work in the results list when the cursor is not in a text field.", "Понятно": "Got it",
  "январь": "January", "февраль": "February", "март": "March", "апрель": "April", "май": "May", "июнь": "June", "июль": "July", "август": "August",
  "сентябрь": "September", "октябрь": "October", "ноябрь": "November", "декабрь": "December",
  "Пн": "Mo", "Вт": "Tu", "Ср": "We", "Чт": "Th", "Пт": "Fr", "Сб": "Sa", "Вс": "Su",
  "Выберите даты в календаре": "Pick dates in the calendar", "Впишите число больше нуля": "Enter a number greater than zero", "Сегодня": "Today", "Вчера": "Yesterday",
  "Последние 7 дней": "Last 7 days", "Последние 30 дней": "Last 30 days", "Последние 90 дней": "Last 90 days", "Этот год": "This year", "Прошлый год": "Last year",
  "Ещё стример…": "Another streamer…", "Ещё категория…": "Another category…", "Ещё автор…": "Another author…", "неизвестно": "unknown",
  "Не удалось воспроизвести видео. Попробуйте открыть клип на Twitch.": "Couldn’t play the video. Try opening the clip on Twitch.", "Такой категории не нашлось": "No such category",
  "Укажите стримера или категорию. Можно и то и другое.": "Enter a streamer or a category. Or both.", "Ищу…": "Searching…", "Подключаюсь к Twitch": "Connecting to Twitch",
  "Остановлено": "Stopped", "Ищем клипы": "Searching for clips", "Считаю, сколько осталось…": "Estimating time left…", "Ничего не найдено": "Nothing found",
  "Ничего не нашлось": "Nothing found", "Расширьте период, уберите часть слов или включите глубокий поиск.": "Widen the period, remove some words or turn on deep search.",
  "Останавливаю…": "Stopping…", "Дождитесь окончания поиска": "Wait for the search to finish", "Смотреть": "Watch", "Выбрать клип": "Select clip", "Стример": "Streamer",
  "Смотреть и обрезать": "Watch and trim", "Скопировать ссылку": "Copy link", "Ссылка скопирована": "Link copied", "Не удалось скопировать": "Couldn’t copy",
  "Список сохранён в папку загрузок": "List saved to the downloads folder", "Скачиваю клип": "Downloading the clip", "Загрузки завершены": "Downloads finished",
  "Пробую ещё раз": "Trying again", "Загрузка отменена": "Download cancelled", "в очереди": "queued", "готово": "done", "ошибка": "error", "отменено": "cancelled",
  "Отменить загрузку": "Cancel download", "Открыть файл": "Open file", "Показать в папке": "Show in folder", "Повторить": "Retry",
  "Готово: файл в папке загрузок.": "Done: the file is in the downloads folder.", "не удалось скачать": "download failed",
  "Пока ничего не скачано. Выберите клипы на вкладке «Поиск» и нажмите «Скачать в 1080p» или откройте клип и скачайте фрагмент.": "Nothing downloaded yet. Select clips on the “Search” tab and press “Download in 1080p”, or open a clip and download a part of it.",
  "Папка загрузок изменена": "Downloads folder changed", "Папка не выбрана": "No folder selected",
  "Папки ещё нет: она появится после первой загрузки.": "The folder doesn’t exist yet: it will appear after the first download.", "В папке пока нет клипов.": "No clips in the folder yet.",
  "Twitch подключён": "Twitch connected", "Нет связи с Twitch ·": "No connection to Twitch ·", "проверить": "retry",
  "Программа закрыта. Это окно можно закрыть.": "The app has been closed. You can close this window.", "Часто ищете": "Frequent searches",
  "Убрать из «Часто ищете»": "Remove from “Frequent searches”", "Убрано из «Часто ищете»": "Removed from “Frequent searches”", "Проверяю…": "Checking…", "нет связи": "no connection",
  "Исправления и улучшения.": "Fixes and improvements.", "Не удалось скачать обновление": "Couldn’t download the update",
  "Запускаю установку… Программа перезапустится сама.": "Starting the installer… The app will restart by itself.", "Установщик скачан. Запустите его, чтобы обновиться.": "The installer has been downloaded. Run it to update.",
  "Наилучшее качество": "Best quality", "Рекомендуется": "Recommended", "Файл примерно вдвое меньше": "About half the file size", "Минимальный размер — удобно переслать": "Smallest size — easy to share",
  "или в": "or in", "Выбрать": "Select", "Пробел": "Space", "Скопировать название": "Copy title", "Название скопировано": "Title copied",
  "Перейти к соседнему клипу": "Move to the next clip", "Смотреть и обрезать клип": "Watch and trim the clip", "Выбрать клип или снять выбор": "Select or unselect the clip",
  "Скачать клип": "Download the clip", "Скачать все выбранные": "Download all selected", "Скопировать ссылку на клип": "Copy the clip link",
  "Меню клипа (как правый клик)": "Clip menu (like right-click)", "Перейти к полю «Стример»": "Go to the “Streamer” field", "Показать эту подсказку": "Show this help",
  "Закрыть меню и окна": "Close menus and windows", "Сначала выберите клипы": "Select clips first", "Выбор снят": "Selection cleared",
  "Enter в любом поле запускает поиск": "Enter in any field starts the search", "Telegram-канал": "Telegram channel", "Язык": "Language",
  "Обновлений нет.": "No updates.", "Неверный клип.": "Invalid clip.", "Не выбрано ни одного клипа.": "No clips selected.", "Неизвестная команда.": "Unknown command.",
  "Можно открывать только ссылки Twitch.": "Only Twitch links can be opened.", "Файл не найден: возможно, его переместили или удалили.": "File not found: it may have been moved or deleted.",
  "Укажите стримера или категорию (можно и то и другое).": "Enter a streamer or a category (or both).", "Дата «с» должна быть раньше даты «по».": "The “from” date must be earlier than the “to” date.",
  "Можно искать сразу не больше чем по 10 стримерам.": "You can search at most 10 streamers at once.", "Можно искать сразу не больше чем по 10 категориям.": "You can search at most 10 categories at once.",
  "Можно искать сразу не больше чем по 10 авторам.": "You can search at most 10 authors at once.", "Сервер ClipSearch пока не настроен.": "The ClipSearch server isn’t set up yet.",
  "Не удалось прочитать дату.": "Couldn’t read the date.", "Не удалось проверить обновления: нет связи с GitHub.": "Couldn’t check for updates: no connection to GitHub.",
  "Не удалось обрезать клип (ffmpeg). Подробности в log.txt.": "Couldn’t trim the clip (ffmpeg). See log.txt for details.",
  "Для обрезки нужен ffmpeg, а он не найден рядом с программой. Пересоберите программу новым build-exe.yml.": "Trimming needs ffmpeg, but it wasn’t found next to the app. Reinstall the app.",
  "без названия": "untitled", "нет такой задачи": "no such task", "Перейти к поиску": "Go to search",
  "Сообщить о проблеме": "Report a problem", "Опишите, что случилось и что вы делали перед этим. Отчёт придёт разработчику в Telegram.": "Describe what happened and what you were doing before. The report goes to the developer on Telegram.",
  "Например: при скачивании клипа появляется ошибка…": "For example: an error appears when downloading a clip…", "Как с вами связаться (необязательно), например @ник в Telegram": "How to reach you (optional), e.g. your Telegram @name",
  "Приложить журнал ошибок": "Attach the error log", "Что будет отправлено": "What will be sent", "Отмена": "Cancel", "Отправить": "Send", "Спасибо! Отчёт отправлен.": "Thank you! The report has been sent.",
  "Опишите, что случилось.": "Please describe what happened.", "Опишите проблему.": "Please describe the problem.", "Слишком много отчётов. Попробуйте через час.": "Too many reports. Please try again in an hour.",
  "Отчёт сейчас не принимается, попробуйте позже.": "Reports can't be received right now, try again later.", "Не удалось отправить отчёт.": "Couldn't send the report.",
  "Нет связи с сервером ClipSearch. Проверьте интернет.": "No connection to the ClipSearch server. Check your internet connection.",
  "клип выбран": "clip selected", "клипа выбрано": "clips selected", "клипов выбрано": "clips selected",
};
const UNIT = [
  [/^(минут[ауы]?)$/, "minute"], [/^(час|часа|часов)$/, "hour"], [/^(день|дня|дней)$/, "day"], [/^(неделю|недели|недель)$/, "week"], [/^(месяц|месяца|месяцев)$/, "month"],
];
const unit = (n, w) => { for (const [re, u] of UNIT) if (re.test(w)) return u + (String(n).replace(/\D/g, "") === "1" ? "" : "s"); return w; };
const N = "(\\d[\\d\\s\\u00a0\\u202f.,]*)";
const clips = (n) => (String(n).replace(/\D/g, "") === "1" ? "clip" : "clips");
const RULES = [
  [new RegExp("^Найдено " + N + " (клип|клипа|клипов)$"), (_, n) => `Found ${n} ${clips(n)}`],
  [new RegExp(N + " (клип|клипа|клипов)(?![а-я])", "g"), (_, n) => `${n} ${clips(n)}`],
  [new RegExp("^" + N + " (клип выбран|клипа выбрано|клипов выбрано)$"), (_, n) => `${n} ${clips(n)} selected`],
  [/Последние (\d+) ([а-яё]+)/g, (_, n, w) => `Last ${n} ${unit(n, w)}`],
  [/за всё время/g, "all time"],
  [/(^|\s)за (\d+) ([а-яё]+)/g, (_, s, n, w) => `${s}last ${n} ${unit(n, w)}`],
  [/с (\d\d\.\d\d\.\d{4}) по (\d\d\.\d\d\.\d{4})/g, "from $1 to $2"],
  [/Просмотрено клипов: /g, "Clips scanned: "], [/ · найдено: /g, " · found: "], [/Осталось примерно: /g, "Time left: about "],
  [/ \(период (\d+) из (\d+)\)/g, " (period $1 of $2)"], [/ · стример (\d+) из (\d+): /g, " · streamer $1 of $2: "],
  [/ Часть клипов не просмотрена: сузьте период или включите глубокий поиск\./g, " Some clips weren’t scanned: narrow the period or turn on deep search."],
  [/^Версия (.+)$/, "Version $1"], [/^Показать ещё \((\d+)\)$/, "Show more ($1)"],
  [/^Скопировано ссылок: (\d+)$/, "Links copied: $1"], [/^Добавлено в очередь: (\d+)$/, "Added to queue: $1"], [/^Скачиваю клипов: (\d+)$/, "Downloading clips: $1"],
  [/^Не удалось скачать: (\d+)$/, "Failed to download: $1"], [/^всего (\d+)$/, "total $1"], [/^Выбрано клипов: (\d+)$/, "Clips selected: $1"],
  [/^Скачиваю и обрезаю фрагмент… /, "Downloading and trimming… "], [/^Скачиваю клип… /, "Downloading… "], [/^Ошибка: /, "Error: "],
  [/^Диск занят на (\d+)%: (.+) из (.+)$/, "Disk $1% full: $2 of $3"],
  [/^Больше всего клипов \((\d+)\) пришлось на период около (.+)\. Наведите на столбик, чтобы увидеть число\.$/, "Most clips ($1) were made around $2. Hover a bar to see the number."],
  [/^У вас последняя версия (.+)$/, "You have the latest version $1"], [/^Доступна версия (.+)$/, "Version $1 available"], [/^Доступна новая версия (.+)$/, "New version $1 available"],
  [/^Скачиваю установщик… (\d+)%$/, "Downloading the installer… $1%"], [/^Качество: (\d+)p$/, "Quality: $1p"], [/^Скачать в (\d+)p$/, "Download in $1p"],
  [/^Клипы будут скачиваться в (\d+)p$/, "Clips will download in $1p"], [/^Скачать выбранные \((\d+)\)$/, "Download selected ($1)"],
  [/^Скопировать ссылки выбранных \((\d+)\)$/, "Copy selected links ($1)"], [/^Все клипы (.+)$/, "All clips by $1"], [/^Клипы автора (.+)$/, "Clips by author $1"],
  [/^Убрать (.+)$/, "Remove $1"], [/ \(фрагмент ([^)]+)\)/g, " (part $1)"], [/(\d[\d,.]*) с$/, "$1 s"], [/(\d+) мин( (\d+) с)?$/, (_, m, __, s) => `${m} min` + (s ? ` ${s} s` : "")],
  [/^(\d+) мин назад$/, "$1 min ago"], [/^(\d+) ч назад$/, "$1 h ago"], [/^(\d+) дн назад$/, "$1 d ago"], [/^(\d+) мес назад$/, "$1 mo ago"],
  [/\((\d+) мин назад\)/, "($1 min ago)"], [/\((\d+) ч назад\)/, "($1 h ago)"], [/\((\d+) дн назад\)/, "($1 d ago)"], [/\((\d+) мес назад\)/, "($1 mo ago)"],
  [/(\d) (Б|КБ|МБ|ГБ|ТБ)(?![А-Яа-я])/g, (_, d, u) => d + " " + { "Б": "B", "КБ": "KB", "МБ": "MB", "ГБ": "GB", "ТБ": "TB" }[u]],
  [/^(\S+) (\d{4})$/, (m, w, y) => (EN[w] ? EN[w] + " " + y : m)],
  [/^Стример «(.+)» не найден\.$/, "Streamer “$1” not found."], [/^Категория «(.+)» не найдена\.$/, "Category “$1” not found."],
  [/^Ник стримера «(.+)» выглядит неверно: только латинские буквы, цифры и «_»\.$/, "Streamer name “$1” looks wrong: only Latin letters, digits and “_”."],
  [/^Нет связи с Twitch: (.*)$/, "No connection to Twitch: $1"], [/^Нет связи: (.*)$/, "No connection: $1"], [/^Twitch ответил (\d+)\.?\s*(.*)$/, "Twitch responded $1. $2"],
  [/^Сервер ClipSearch ответил (\d+)\.$/, "The ClipSearch server responded $1."], [/^Сервер ClipSearch временно недоступен \(ответ (\d+)\)\.$/, "The ClipSearch server is temporarily unavailable (response $1)."],
  [/^Не удалось получить ссылку на скачивание клипа (.+)\.$/, "Couldn’t get a download link for clip $1."], [/^Неожиданная ошибка: (.*)$/, "Unexpected error: $1"],
  [/^Слишком много запросов, подождите минуту\.$/, "Too many requests, please wait a minute."],
];
function tr(s) {
  if (!s || !/[А-Яа-яЁё]/.test(s)) return s;
  const t = s.trim();
  if (EN[t] != null) return s.replace(t, EN[t]);
  let out = t;
  for (const [re, rep] of RULES) out = out.replace(re, rep);
  if (out !== t && /^за /.test(t)) out = out.charAt(0).toUpperCase() + out.slice(1);
  return out === t ? s : s.replace(t, out);
}
window.CS_T = tr;
const ATTRS = ["placeholder", "title", "aria-label"];
function walk(root) {
  if (root.nodeType === 3) { const v = tr(root.nodeValue); if (v !== root.nodeValue) root.nodeValue = v; return; }
  if (root.nodeType !== 1 || root.tagName === "SCRIPT" || root.tagName === "STYLE") return;
  for (const a of ATTRS) { const v = root.getAttribute(a); if (v) { const t = tr(v); if (t !== v) root.setAttribute(a, t); } }
  if (root.tagName === "INPUT" && root.type === "button") { const t = tr(root.value); if (t !== root.value) root.value = t; }
  for (let c = root.firstChild; c; c = c.nextSibling) walk(c);
}
walk(document.body);
document.title = tr(document.title);
new MutationObserver((ms) => {
  for (const m of ms) {
    if (m.type === "characterData") walk(m.target);
    else if (m.type === "attributes") { const v = m.target.getAttribute(m.attributeName); const t = tr(v); if (v && t !== v) m.target.setAttribute(m.attributeName, t); }
    else m.addedNodes.forEach(walk);
  }
}).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
})();
</script>
<script>
(function () {
"use strict";
const T = "__TOKEN__";
const LOC = window.CS_LANG === "en" ? "en-US" : "ru-RU";
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
  $("pMeta").innerHTML = (r.broadcaster ? `<span>${IC.tv}${esc(r.broadcaster)}</span>` : "") + `<span>${IC.user}${esc(r.creator || "неизвестно")}</span>` + (r.game ? `<span>${IC.game}${esc(r.game)}</span>` : "") + `<span>${IC.eye}${r.views.toLocaleString(LOC)}</span>`;
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
  $("sNum").textContent = `Просмотрено клипов: ${p.scanned.toLocaleString(LOC)} · найдено: ${p.found.toLocaleString(LOC)}`;
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
  $("resTitle").textContent = n ? `Найдено ${n.toLocaleString(LOC)} ${plural(n, ["клип", "клипа", "клипов"])}` : "Ничего не найдено";
  let st = `${periodText()}. Просмотрено клипов: ${res.info.scanned.toLocaleString(LOC)}.`;
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
    `<span>${IC.clock}${r.ts.toLocaleString(LOC, { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })} <span class="rel">(${ago(r.ts)})</span></span>` +
    `<span>${IC.eye}${r.views.toLocaleString(LOC)}</span></div></div>` +
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
    $("stCount").textContent = f.count.toLocaleString(LOC);
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
      `<div style="min-width:0"><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div><div class="sb">${fmtBytes(r.size)} · ${new Date(r.mtime * 1000).toLocaleString(LOC, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</div></div>` +
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
  return withTime ? d.toLocaleString(LOC, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
                  : d.toLocaleDateString(LOC, { day: "2-digit", month: "2-digit", year: "2-digit" });
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
  tip.innerHTML = `<b>${c.toLocaleString(LOC)} ${plural(c, ["клип", "клипа", "клипов"])}</b><span>${chartFmt(a, withTime)} – ${chartFmt(z, withTime)}</span>`;
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
if (window.cs && !window.cs.nativeFrame) {
  document.body.classList.add("has-wc"); $("wc").hidden = false;
  $("wc").addEventListener("click", (e) => { const b = e.target.closest("[data-w]"); if (b) window.cs.win(b.dataset.w); });
}
$("navTg").addEventListener("click", () => api("/api/openurl", { url: "https://t.me/ClipSearch" }));
let rpData = { log: "", os: "", version: "" };
function rpPaint() {
  const lines = [(window.CS_LANG === "en" ? "Version: " : "Версия: ") + rpData.version, (window.CS_LANG === "en" ? "System: " : "Система: ") + rpData.os,
    (window.CS_LANG === "en" ? "Contact: " : "Контакт: ") + ($("rpContact").value.trim() || "—"), "", $("rpText").value.trim() || "…"];
  if ($("rpLog").checked) lines.push("", "— log.txt —", rpData.log || (window.CS_LANG === "en" ? "(empty)" : "(пусто)"));
  $("rpPreview").textContent = lines.join("\n");
}
$("navReport").addEventListener("click", async () => {
  showVeil($("reportVeil")); setTimeout(() => $("rpText").focus(), 60);
  try { rpData = await api("/api/report/preview", {}); } catch (_) {}
  rpPaint();
});
["rpText", "rpContact", "rpLog"].forEach((id) => $(id).addEventListener("input", rpPaint));
$("rpLog").addEventListener("change", rpPaint);
$("rpCancel").addEventListener("click", () => hideVeil($("reportVeil")));
$("reportVeil").addEventListener("mousedown", (e) => { if (e.target === $("reportVeil")) hideVeil($("reportVeil")); });
$("rpSend").addEventListener("click", async () => {
  const b = $("rpSend"); b.disabled = true;
  const r = await api("/api/report", { text: $("rpText").value, contact: $("rpContact").value, log: $("rpLog").checked }).catch(() => ({ error: "нет связи" }));
  b.disabled = false;
  if (r.error) { toast(r.error); return; }
  hideVeil($("reportVeil")); $("rpText").value = ""; toast("Спасибо! Отчёт отправлен.");
});
document.querySelectorAll(".langsw button").forEach((b) => {
  b.classList.toggle("on", b.dataset.lang === window.CS_LANG);
  b.addEventListener("click", async () => { if (b.dataset.lang === window.CS_LANG) return; await api("/api/settings", { lang: b.dataset.lang }); location.reload(); });
});
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
  const parts = String(text || "").split(/<!--\s*en\s*-->/i);
  return (window.CS_LANG === "en" && parts[1] ? parts[1] : parts[0]).split(/\r?\n/).map((l) => l.replace(/^\s*[-*•]\s*/, "").trim()).filter((l) => l && !/^#/.test(l)).slice(0, 8);
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
  if (e.key === "Escape" && isOpen($("reportVeil"))) { hideVeil($("reportVeil")); return; }
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
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
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
        LANG["code"] = self.cfg.get("lang") if self.cfg.get("lang") in ("ru", "en") else system_lang()
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
    if sys.platform == "darwin":
        prompt = L("Куда сохранять клипы", "Where to save clips").replace('"', "")
        loc = f' default location (POSIX file "{initial}")' if initial and os.path.isdir(initial) and '"' not in initial else ""
        try:
            r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}"{loc})'],
                               capture_output=True, timeout=600)
            return r.stdout.decode("utf-8", "replace").strip().rstrip("/") or ""
        except Exception:
            return ""
    if not sys.platform.startswith("win"):
        return ""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description=$env:CLIPS_TITLE;"
        "if($env:CLIPS_INIT -and (Test-Path $env:CLIPS_INIT)){$f.SelectedPath=$env:CLIPS_INIT};"
        "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, timeout=600, env={**os.environ, "CLIPS_INIT": initial, "CLIPS_TITLE": L("Куда сохранять клипы", "Where to save clips")},
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
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    except Exception as e:
        log(f"reveal: {e!r}")

def vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)
    except Exception:
        return (0,)

UPDATE_ASSET = "ClipSearch-mac.zip" if sys.platform == "darwin" else "ClipSearchSetup.exe"

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
    setup = next((a for a in d.get("assets") or [] if a.get("name") == UPDATE_ASSET), None)
    return {
        "current": VERSION, "latest": latest, "newer": vtuple(latest) > vtuple(VERSION),
        "notes": str(d.get("body") or ""), "setupUrl": setup.get("browser_download_url") if setup else "",
        "size": int(setup.get("size") or 0) if setup else 0,
    }

def download_setup(info, job):
    import tempfile
    if sys.platform == "darwin":
        path = os.path.join(tempfile.gettempdir(), f"ClipSearch-mac-{info['latest']}.zip")
    else:
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
                html = UI_HTML.replace("__TOKEN__", ctx.token).replace("__VERSION__", VERSION).replace("__LANG__", LANG["code"])
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
                    "lang": LANG["code"],
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
                if b.get("lang") in ("ru", "en"):
                    ctx.cfg["lang"] = LANG["code"] = b["lang"]
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
                    w.writerow(L(["Название", "Автор клипа", "Стример", "Игра", "Дата (UTC)", "Просмотры", "Секунд", "Ссылка"],
                                 ["Title", "Clip author", "Streamer", "Game", "Date (UTC)", "Views", "Seconds", "Link"]))
                    for r in rows:
                        w.writerow([r.get("title"), r.get("creator"), r.get("broadcaster"), r.get("game"),
                                    r.get("created"), r.get("views"), round(float(r.get("duration") or 0)), r.get("url")])
                return {"path": p}
            if path == "/api/pickfolder":
                return {"path": pick_folder(ctx.download_dir())}
            if path == "/api/openurl":
                url = str(b.get("url", ""))
                host = urllib.parse.urlparse(url).hostname or ""
                if url == TG_URL:
                    webbrowser.open(url)
                    return {"ok": True}
                if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
                    raise ValueError("Можно открывать только ссылки Twitch.")
                webbrowser.open(url)
                return {"ok": True}
            if path == "/api/report/preview":
                return {"log": report_log(), "os": os_name(), "version": VERSION}
            if path == "/api/report":
                if len(str(b.get("text", "")).strip()) < 3:
                    raise ValueError(L("Опишите, что случилось.", "Please describe what happened."))
                return send_report(b.get("text"), b.get("contact"), bool(b.get("log")))
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
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", p])
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

        ctypes.windll.user32.MessageBoxW(0, text, "ClipSearch", 0x10)
    except Exception:
        if sys.platform == "darwin":
            try:
                subprocess.run(["osascript", "-e", "display alert \"ClipSearch\" message " + json.dumps(str(text))], timeout=120)
                return
            except Exception:
                pass
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
            f.write(text.replace("__APP_VERSION__", VERSION))
    with open(os.path.join(folder, "clip_search.ico"), "wb") as f:
        f.write(icon_ico_bytes())

def write_host(folder):
    unpack_sources(HOST_B64, folder)

INSTALLER_B64 = "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"

def write_installer(folder):
    unpack_sources(INSTALLER_B64, folder)

MAC_B64 = "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"

def write_mac(folder):
    unpack_sources(MAC_B64, folder)

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
    if "--nettest" in sys.argv:
        r = check_link()
        print("NETTEST", json.dumps(r, ensure_ascii=False), os.environ.get("SSL_CERT_FILE", ""))
        return 0 if r.get("ok") else 1
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
    if "--write-mac" in sys.argv:
        write_mac(arg_value("--write-mac") or "mac")
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
