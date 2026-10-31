#!/usr/bin/env python3
"""Carpe science — небольшой сервер сайта.

Отдаёт сайт и страницу админа, объединяет события от сборщика (data/events.json
в репозитории) с одобренными заявками организаторов, считает посещения без cookie
и отметки «сходил(а) благодаря сайту».

Только стандартная библиотека Python 3.10+. Настройки — переменные окружения:
  CARPE_ADMIN_PASSWORD   пароль страницы /admin (обязательно)
  CARPE_SECRET           случайная строка для подписи сессий (обязательно)
  CARPE_REPO_DIR         папка с клоном репозитория (по умолчанию — папка выше server/)
  CARPE_DB               путь к базе SQLite (по умолчанию /var/lib/carpe/carpe.db)
  CARPE_PORT             порт (по умолчанию 8080, слушает только 127.0.0.1)
  CARPE_TG_TOKEN, CARPE_TG_CHAT   необязательно: уведомления о новых заявках в Telegram
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("CARPE_REPO_DIR", ROOT)
PUBLIC = os.path.join(REPO, "public")
EVENTS_FILE = os.path.join(REPO, "data", "events.json")
DB_PATH = os.environ.get("CARPE_DB", "/var/lib/carpe/carpe.db")
PORT = int(os.environ.get("CARPE_PORT", "8080"))
ADMIN_PASSWORD = os.environ.get("CARPE_ADMIN_PASSWORD", "")
SECRET = os.environ.get("CARPE_SECRET", "").encode()
TG_TOKEN = os.environ.get("CARPE_TG_TOKEN", "")
TG_CHAT = os.environ.get("CARPE_TG_CHAT", "")
MSK = timezone(timedelta(hours=3))
SESSION_TTL = 14 * 24 * 3600
MAX_BODY = 32 * 1024

FIELDS = {"space", "bio", "med", "chem", "phys", "earth", "it", "hum", "ocean", "media", "lit", "ling"}
TYPES = {"lecture", "course", "cafe", "school", "talk", "quiz", "winter", "module", "contest", "fest", "tour"}

_lock = threading.Lock()
_events_cache = {"mtime": None, "data": []}
_rate = {}


def today():
    return datetime.now(MSK).date()


def now_iso():
    return datetime.now(MSK).isoformat(timespec="seconds")


# ---------- база ----------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS submissions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, status TEXT DEFAULT 'pending',
              data TEXT, contact TEXT, decided TEXT);
            CREATE TABLE IF NOT EXISTS hidden(event_id TEXT PRIMARY KEY, created TEXT);
            CREATE TABLE IF NOT EXISTS views(day TEXT, path TEXT, count INTEGER, PRIMARY KEY(day, path));
            CREATE TABLE IF NOT EXISTS visitors(day TEXT, vid TEXT, PRIMARY KEY(day, vid));
            CREATE TABLE IF NOT EXISTS refs(day TEXT, ref TEXT, count INTEGER, PRIMARY KEY(day, ref));
            CREATE TABLE IF NOT EXISTS clicks(day TEXT, event_id TEXT, count INTEGER, PRIMARY KEY(day, event_id));
            CREATE TABLE IF NOT EXISTS attend(event_id TEXT, vid TEXT, created TEXT, PRIMARY KEY(event_id, vid));
            """
        )


# ---------- события ----------
def crawler_events():
    try:
        m = os.path.getmtime(EVENTS_FILE)
    except OSError:
        return []
    with _lock:
        if _events_cache["mtime"] != m:
            try:
                with open(EVENTS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                _events_cache["data"] = [e for e in data if isinstance(e, dict) and e.get("id")]
                _events_cache["mtime"] = m
            except (OSError, ValueError):
                pass
        return list(_events_cache["data"])


def approved_events(con):
    out = []
    for r in con.execute("SELECT id, data, decided FROM submissions WHERE status='approved'"):
        e = json.loads(r["data"])
        e.update(id=f"u{r['id']}", verified=True, auto=False, organizer=True, addedAt=r["decided"])
        out.append(e)
    return out


def all_events(con, include_hidden=False):
    hidden = {r[0] for r in con.execute("SELECT event_id FROM hidden")}
    counts = dict(con.execute("SELECT event_id, COUNT(*) FROM attend GROUP BY event_id").fetchall())
    evs = crawler_events() + approved_events(con)
    out = []
    for e in evs:
        if e.get("hidden") and not include_hidden:
            continue
        if e["id"] in hidden:
            if not include_hidden:
                continue
            e = dict(e, adminHidden=True)
        out.append(dict(e, attended=counts.get(e["id"], 0)))
    return out


# ---------- проверка заявки ----------
def clean_submission(p):
    def s(k, n=300):
        v = p.get(k, "")
        return re.sub(r"\s+", " ", str(v)).strip()[:n]

    title, city, d = s("title", 200), s("city", 80), s("date", 10)
    errors = []
    if len(title) < 4:
        errors.append("Укажите название события")
    if not city:
        errors.append("Укажите город или «онлайн»")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        errors.append("Укажите дату")
    else:
        try:
            if date.fromisoformat(d) < today():
                errors.append("Дата уже прошла")
        except ValueError:
            errors.append("Неверная дата")
    url = s("url", 500)
    if not re.match(r"^https?://[^\s/]+\.[^\s]+", url):
        errors.append("Укажите ссылку на официальный анонс или страницу регистрации")
    if errors:
        return None, None, errors
    field = s("field", 20)
    typ = s("type", 20)
    online = city.lower() in ("онлайн", "online")
    ev = {
        "kind": "learn" if typ in ("winter", "module") else "event",
        "d": d,
        "time": s("time", 40) or "время уточняется",
        "title": title,
        "who": s("who", 200),
        "city": "Онлайн" if online else city,
        "online": online,
        "place": s("place", 200) or ("Трансляция" if online else "Уточняйте у организатора"),
        "org": s("org", 200) or "Организатор",
        "field": field if field in FIELDS else "hum",
        "type": typ if typ in TYPES else "lecture",
        "price": "Уточняйте у организатора",
        "aud": "Для всех",
        "desc": s("desc", 1200),
        "src": url,
        "srcName": "Страница организатора",
    }
    if s("lang", 5) == "en":
        ev["lang"] = "en"
    join = s("join", 500)
    if re.match(r"^https?://[^\s/]+\.[^\s]+", join):
        ev["joinUrl"] = join
        ev["online"] = True
    if ev["kind"] == "learn":
        ev["dEnd"] = d
    return ev, "", []


# ---------- сессии и защита ----------
def sign(v):
    return hmac.new(SECRET, v.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{sign(exp)}"


def valid_session(tok):
    try:
        exp, sig = tok.split(".", 1)
        return hmac.compare_digest(sig, sign(exp)) and int(exp) > time.time()
    except Exception:
        return False


def rate_ok(key, limit, window):
    t = time.time()
    with _lock:
        hits = [x for x in _rate.get(key, []) if t - x < window]
        if len(hits) >= limit:
            _rate[key] = hits
            return False
        hits.append(t)
        _rate[key] = hits
        if len(_rate) > 50000:
            _rate.clear()
        return True


def tg_notify(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    def go():
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=10)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8", ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "carpe"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # --- вспомогательное ---
    def ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd and self.client_address[0] in ("127.0.0.1", "::1"):
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def vid(self):
        # анонимный идентификатор посетителя на сутки: без cookie, IP не хранится
        raw = f"{today()}|{self.ip()}|{self.headers.get('User-Agent', '')}".encode()
        return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()[:20]

    def vid_long(self):
        raw = f"{self.ip()}|{self.headers.get('User-Agent', '')}".encode()
        return hmac.new(SECRET, b"attend|" + raw, hashlib.sha256).hexdigest()[:24]

    def send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def is_admin(self):
        return valid_session(self.cookie("carpe_admin"))

    def same_origin_post(self):
        # защита от подделки запросов: POST только со своего сайта
        if self.headers.get("X-Carpe") != "1":
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return not origin or urllib.parse.urlparse(origin).netloc == host

    # --- статика ---
    def static(self, path):
        if path in ("/", "", "/index.html"):
            full = os.path.join(REPO, "index.html")
        else:
            if path in ("/admin", "/admin/"):
                path = "/admin.html"
            full = os.path.normpath(os.path.join(PUBLIC, path.lstrip("/")))
            if not full.startswith(PUBLIC + os.sep):
                full = ""
        if not full or not os.path.isfile(full):
            return self.send(404, "Страница не найдена", "text/plain; charset=utf-8")
        with open(full, "rb") as f:
            data = f.read()
        ext = os.path.splitext(full)[1]
        cache = "no-cache" if ext == ".html" else "public, max-age=3600"
        extra = {"Cache-Control": cache}
        if path == "/admin.html":
            extra["X-Robots-Tag"] = "noindex"
            extra["X-Frame-Options"] = "DENY"
        self.send(200, data, MIME.get(ext, "application/octet-stream"), extra)

    # --- маршруты ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p == "/api/events":
            with db() as con:
                evs = all_events(con)
            return self.send(200, {"events": evs, "updated": self.updated()}, headers={"Cache-Control": "no-cache"})
        if p == "/api/health":
            return self.send(200, {"ok": True})
        if p.startswith("/api/admin/"):
            if not self.is_admin():
                return self.send(401, {"error": "Нужно войти"})
            if p == "/api/admin/summary":
                return self.send(200, self.summary())
            return self.send(404, {"error": "Нет такого адреса"})
        if p.startswith("/api/"):
            return self.send(404, {"error": "Нет такого адреса"})
        return self.static(p)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if not self.same_origin_post():
            return self.send(403, {"error": "Запрос отклонён"})
        try:
            data = self.body()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "Некорректный запрос"})
        ipkey = hashlib.sha256((self.ip() + SECRET.decode(errors="ignore")).encode()).hexdigest()[:16]

        if p == "/api/hit":
            if not rate_ok("hit" + ipkey, 120, 3600):
                return self.send(204)
            path = str(data.get("path", "/"))[:120]
            ref = str(data.get("ref", ""))[:200]
            refhost = urllib.parse.urlparse(ref).netloc.lower().removeprefix("www.") if ref else ""
            host = self.headers.get("Host", "").lower().removeprefix("www.")
            d = str(today())
            with db() as con:
                con.execute("INSERT INTO views VALUES(?,?,1) ON CONFLICT(day,path) DO UPDATE SET count=count+1", (d, path))
                con.execute("INSERT OR IGNORE INTO visitors VALUES(?,?)", (d, self.vid()))
                if refhost and refhost != host:
                    con.execute("INSERT INTO refs VALUES(?,?,1) ON CONFLICT(day,ref) DO UPDATE SET count=count+1", (d, refhost[:100]))
            return self.send(204)

        if p == "/api/click":
            eid = str(data.get("id", ""))[:80]
            if eid and rate_ok("click" + ipkey, 300, 3600):
                with db() as con:
                    con.execute("INSERT INTO clicks VALUES(?,?,1) ON CONFLICT(day,event_id) DO UPDATE SET count=count+1", (str(today()), eid))
            return self.send(204)

        if p == "/api/attend":
            eid = str(data.get("id", ""))[:80]
            with db() as con:
                known = {e["id"]: e for e in all_events(con)}
                e = known.get(eid)
                if not e:
                    return self.send(404, {"error": "Событие не найдено"})
                start = date.fromisoformat(e["d"])
                if today() < start:
                    return self.send(400, {"error": "Отметить можно в день события и позже"})
                if not rate_ok("att" + ipkey, 30, 3600):
                    return self.send(429, {"error": "Слишком много отметок, попробуйте позже"})
                con.execute("INSERT OR IGNORE INTO attend VALUES(?,?,?)", (eid, self.vid_long(), now_iso()))
                n = con.execute("SELECT COUNT(*) FROM attend WHERE event_id=?", (eid,)).fetchone()[0]
            return self.send(200, {"attended": n})

        if p == "/api/submit":
            if data.get("website"):  # поле-ловушка для ботов
                return self.send(200, {"ok": True})
            if not rate_ok("sub" + ipkey, 5, 3600):
                return self.send(429, {"error": "Слишком много заявок с вашего адреса. Попробуйте через час."})
            ev, email, errors = clean_submission(data)
            if errors:
                return self.send(400, {"error": "; ".join(errors)})
            with db() as con:
                con.execute("INSERT INTO submissions(created,data,contact) VALUES(?,?,?)",
                            (now_iso(), json.dumps(ev, ensure_ascii=False), email))
                pending = con.execute("SELECT COUNT(*) FROM submissions WHERE status='pending'").fetchone()[0]
            tg_notify(f"Новая заявка на Carpe science: «{ev['title']}», {ev['city']}, {ev['d']}. На одобрении: {pending}.")
            return self.send(200, {"ok": True})

        if p == "/api/admin/login":
            if not rate_ok("login" + ipkey, 8, 900):
                return self.send(429, {"error": "Слишком много попыток. Подождите 15 минут."})
            pw = str(data.get("password", ""))
            if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                cookie = f"carpe_admin={make_session()}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Strict"
                return self.send(200, {"ok": True}, headers={"Set-Cookie": cookie})
            time.sleep(1)
            return self.send(401, {"error": "Неверный пароль"})

        if p == "/api/admin/logout":
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "carpe_admin=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})

        if p.startswith("/api/admin/"):
            if not self.is_admin():
                return self.send(401, {"error": "Нужно войти"})
            m = re.fullmatch(r"/api/admin/submission/(\d+)/(approve|reject)", p)
            if m:
                sid, action = int(m.group(1)), m.group(2)
                with db() as con:
                    row = con.execute("SELECT data FROM submissions WHERE id=?", (sid,)).fetchone()
                    if not row:
                        return self.send(404, {"error": "Заявка не найдена"})
                    ev = json.loads(row["data"])
                    edits = data.get("edits") or {}
                    if isinstance(edits, dict):
                        for k in ("title", "who", "city", "place", "org", "time", "price", "aud", "desc", "src", "joinUrl", "lang", "d", "field", "type"):
                            if k in edits:
                                ev[k] = str(edits[k])[:1200]
                        if ev.get("src"):
                            ev["srcName"] = ev.get("srcName") or "Страница организатора"
                    status = "approved" if action == "approve" else "rejected"
                    con.execute("UPDATE submissions SET status=?, data=?, decided=? WHERE id=?",
                                (status, json.dumps(ev, ensure_ascii=False), now_iso(), sid))
                return self.send(200, {"ok": True})
            m = re.fullmatch(r"/api/admin/event/([A-Za-z0-9_\-]+)/(hide|show)", p)
            if m:
                eid, action = m.group(1), m.group(2)
                with db() as con:
                    if action == "hide":
                        con.execute("INSERT OR IGNORE INTO hidden VALUES(?,?)", (eid, now_iso()))
                    else:
                        con.execute("DELETE FROM hidden WHERE event_id=?", (eid,))
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "Нет такого адреса"})

        return self.send(404, {"error": "Нет такого адреса"})

    # --- данные для админки ---
    def updated(self):
        try:
            return datetime.fromtimestamp(os.path.getmtime(EVENTS_FILE), MSK).isoformat(timespec="minutes")
        except OSError:
            return None

    def summary(self):
        t = today()
        days = [str(t - timedelta(days=i)) for i in range(29, -1, -1)]
        start = days[0]
        with db() as con:
            views = dict(con.execute("SELECT day, SUM(count) FROM views WHERE day>=? GROUP BY day", (start,)).fetchall())
            uniq = dict(con.execute("SELECT day, COUNT(*) FROM visitors WHERE day>=? GROUP BY day", (start,)).fetchall())
            refs = con.execute("SELECT ref, SUM(count) c FROM refs WHERE day>=? GROUP BY ref ORDER BY c DESC LIMIT 10", (start,)).fetchall()
            clicks = dict(con.execute("SELECT event_id, SUM(count) FROM clicks WHERE day>=? GROUP BY event_id", (start,)).fetchall())
            evs = all_events(con, include_hidden=True)
            subs = [dict(r) for r in con.execute("SELECT id, created, status, data, contact, decided FROM submissions ORDER BY id DESC LIMIT 200")]
            att_total = con.execute("SELECT COUNT(*) FROM attend").fetchone()[0]
            att_30 = con.execute("SELECT COUNT(*) FROM attend WHERE created>=?", (start,)).fetchone()[0]
        for s in subs:
            s["data"] = json.loads(s["data"])
        byid = {e["id"]: e for e in evs}
        top_att = sorted([e for e in evs if e.get("attended")], key=lambda e: -e["attended"])[:15]
        top_clicks = sorted(clicks.items(), key=lambda x: -x[1])[:15]
        upcoming = [e for e in evs if (e.get("dEnd") or e["d"]) >= str(t)]
        return {
            "days": [{"day": d, "views": views.get(d, 0), "visitors": uniq.get(d, 0)} for d in days],
            "refs": [{"ref": r[0], "count": r[1]} for r in refs],
            "attendTotal": att_total,
            "attend30": att_30,
            "topAttended": [{"id": e["id"], "title": e["title"], "city": e.get("city"), "d": e["d"], "attended": e["attended"]} for e in top_att],
            "topClicked": [{"id": i, "title": byid.get(i, {}).get("title", i), "city": byid.get(i, {}).get("city", ""), "clicks": c} for i, c in top_clicks],
            "submissions": subs,
            "events": [{"id": e["id"], "title": e["title"], "city": e.get("city"), "d": e["d"], "verified": e.get("verified"),
                        "auto": e.get("auto"), "organizer": e.get("organizer", False), "hidden": bool(e.get("adminHidden")),
                        "src": e.get("src", "")} for e in sorted(upcoming, key=lambda e: e["d"])],
            "counts": {"upcoming": len([e for e in upcoming if not e.get("adminHidden")]),
                       "cities": len({e.get("city") for e in upcoming if not e.get("adminHidden")}),
                       "pending": len([s for s in subs if s["status"] == "pending"])},
            "updated": self.updated(),
        }


def main():
    if not ADMIN_PASSWORD or len(SECRET) < 16:
        raise SystemExit("Задайте CARPE_ADMIN_PASSWORD и CARPE_SECRET (не короче 16 символов)")
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"Carpe science: http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
