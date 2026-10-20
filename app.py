#!/usr/bin/env python3
"""WARS: a small, self-contained manga community app powered by SQLite."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import queue
import re
import unicodedata
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PANEL_WARS_DB", ROOT / "panel_wars.sqlite3"))
PORT = int(os.environ.get("PORT", "8001"))
HOST = os.environ.get("HOST", "127.0.0.1")
COOKIE_SECURE = "; Secure" if os.environ.get("COOKIE_SECURE", "").casefold() in {"1", "true", "yes"} else ""
SESSIONS: dict[str, queue.Queue] = {}
SESSIONS_LOCK = threading.Lock()
ROOMS = [("general", "General", "The main reading room"), ("weekly-picks", "Weekly picks", "What are you reading this week?"), ("chapter-talk", "Chapter talk", "Fresh chapters and theories")]
DEFAULT_CHAT_BLOCKED_WORDS = frozenset({
    "asshole", "assholes", "bastard", "bastards", "bitch", "bitches", "bullshit",
    "cunt", "cunts", "fuck", "fucked", "fucker", "fuckers", "fucking", "motherfucker",
    "motherfuckers", "shit", "shits", "shitty", "slut", "sluts", "whore", "whores",
})
CHAT_BLOCKED_WORDS = DEFAULT_CHAT_BLOCKED_WORDS | frozenset(
    word.strip().casefold() for word in os.environ.get("WARS_CHAT_BLOCKED_WORDS", "").split(",") if word.strip()
)
CHAT_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
DM_ROLES = {"reader", "community_moderator", "room_moderator"}
PLACEHOLDER_MANGA_TITLES = (
    "The Last Lantern", "Blue Hour Club", "Iron Orchard", "Paper Moons",
    "Sunday Ramen", "Ghost Circuit", "Crimson Atlas", "Little Giant Steps",
)
EVENT_BACKGROUNDS = {"street", "sakura", "sunset", "ocean", "midnight"}
EVENT_TRANSITIONS = {1000, 3000, 6000, 12000}
MAX_EVENT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_MANGA_PAGE_BYTES = 5 * 1024 * 1024
MAX_MANGA_PDF_BYTES = 20 * 1024 * 1024
MAX_MANGA_CHAPTER_PAGES = 1000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def has_inappropriate_chat_language(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return any(word in CHAT_BLOCKED_WORDS for word in CHAT_WORD_PATTERN.findall(normalized))


def pw_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def pw_ok(password: str, stored: str) -> bool:
    try:
        salt64, digest64 = stored.split("$", 1)
        expected = base64.urlsafe_b64decode(digest64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt64), 180_000)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'reader' CHECK(role IN ('reader','moderator','admin','owner')),
          staff_role TEXT, banned_at TEXT, ban_reason TEXT,
          bio TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manga (
          id INTEGER PRIMARY KEY, title TEXT NOT NULL UNIQUE, author TEXT NOT NULL, genres TEXT NOT NULL,
          synopsis TEXT NOT NULL, chapters INTEGER NOT NULL,
          unit_type TEXT NOT NULL DEFAULT 'chapter' CHECK(unit_type IN ('chapter','volume')),
          status TEXT NOT NULL, palette TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manga_pages (
          id INTEGER PRIMARY KEY,
          manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
          chapter_number INTEGER NOT NULL CHECK(chapter_number > 0),
          page_number INTEGER NOT NULL CHECK(page_number > 0),
          mime_type TEXT NOT NULL CHECK(mime_type IN ('image/jpeg','image/png','image/webp')),
          file_name TEXT NOT NULL,
          image_blob BLOB NOT NULL,
          uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          uploaded_at TEXT NOT NULL,
          UNIQUE(manga_id, chapter_number, page_number)
        );
        CREATE TABLE IF NOT EXISTS manga_chapter_pdfs (
          id INTEGER PRIMARY KEY,
          manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
          chapter_number INTEGER NOT NULL CHECK(chapter_number > 0),
          file_name TEXT NOT NULL,
          pdf_blob BLOB NOT NULL,
          uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          uploaded_at TEXT NOT NULL,
          UNIQUE(manga_id, chapter_number)
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
          rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10), body TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(user_id, manga_id)
        );
        CREATE TABLE IF NOT EXISTS shelf (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('Reading','Plan to read','Completed','On hold','Dropped')),
          progress INTEGER NOT NULL DEFAULT 0 CHECK(progress >= 0), updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id, manga_id)
        );
        CREATE TABLE IF NOT EXISTS room_messages (
          id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
          id INTEGER PRIMARY KEY, user_a INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          user_b INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL,
          CHECK(user_a < user_b), UNIQUE(user_a, user_b)
        );
        CREATE TABLE IF NOT EXISTS direct_messages (
          id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_mutes (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          room_id TEXT NOT NULL, muted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          reason TEXT NOT NULL DEFAULT '', expires_at INTEGER, created_at TEXT NOT NULL,
          PRIMARY KEY(user_id, room_id)
        );
        CREATE TABLE IF NOT EXISTS chat_language_violations (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          violation_count INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
          id INTEGER PRIMARY KEY,
          user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          username TEXT NOT NULL DEFAULT 'Anonymous',
          category TEXT NOT NULL CHECK(category IN ('suggestion','bug','other')),
          message TEXT NOT NULL,
          created_at TEXT NOT NULL,
          handled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS room_message_recent ON room_messages(room_id, id DESC);
        CREATE INDEX IF NOT EXISTS direct_message_recent ON direct_messages(conversation_id, id DESC);
        CREATE INDEX IF NOT EXISTS feedback_recent ON feedback(id DESC);
        CREATE TABLE IF NOT EXISTS app_migrations (
          id TEXT PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS site_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), background TEXT NOT NULL DEFAULT 'street',
          transition_ms INTEGER NOT NULL DEFAULT 3000, updated_at TEXT NOT NULL
        );
        """)
        db.execute("INSERT OR IGNORE INTO site_settings(id,background,transition_ms,updated_at) VALUES(1,'street',3000,?)", (utcnow(),))
        site_columns = {r["name"] for r in db.execute("PRAGMA table_info(site_settings)")}
        for column, definition in (("custom_image", "BLOB"), ("custom_mime", "TEXT"), ("custom_name", "TEXT")):
            if column not in site_columns:
                db.execute(f"ALTER TABLE site_settings ADD COLUMN {column} {definition}")
        user_columns = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        for column, definition in (("staff_role", "TEXT"), ("banned_at", "TEXT"), ("ban_reason", "TEXT")):
            if column not in user_columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        feedback_columns = {r["name"] for r in db.execute("PRAGMA table_info(feedback)")}
        if "handled_at" not in feedback_columns:
            db.execute("ALTER TABLE feedback ADD COLUMN handled_at TEXT")
        manga_columns = {r["name"] for r in db.execute("PRAGMA table_info(manga)")}
        if "is_active" not in manga_columns:
            db.execute("ALTER TABLE manga ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "unit_type" not in manga_columns:
            db.execute("ALTER TABLE manga ADD COLUMN unit_type TEXT NOT NULL DEFAULT 'chapter' CHECK(unit_type IN ('chapter','volume'))")
        if not db.execute("SELECT 1 FROM app_migrations WHERE id='remove_placeholder_manga_v1'").fetchone():
            placeholders = ",".join("?" for _ in PLACEHOLDER_MANGA_TITLES)
            db.execute(f"DELETE FROM manga WHERE title IN ({placeholders})", PLACEHOLDER_MANGA_TITLES)
            db.execute("INSERT INTO app_migrations(id,applied_at) VALUES('remove_placeholder_manga_v1',?)", (utcnow(),))
        if not db.execute("SELECT 1 FROM app_migrations WHERE id='remove_demo_accounts_v1'").fetchone():
            db.execute("DELETE FROM users WHERE username IN ('reader','mod','admin','owner')")
            db.execute("INSERT INTO app_migrations(id,applied_at) VALUES('remove_demo_accounts_v1',?)", (utcnow(),))
        db.execute("UPDATE users SET staff_role='community_moderator',role='reader' WHERE role='moderator' AND staff_role IS NULL")


def effective_role(row) -> str:
    keys = row.keys() if hasattr(row, "keys") else row
    staff_role = row["staff_role"] if "staff_role" in keys else None
    return staff_role or row["role"]


def public_user(row: sqlite3.Row | dict) -> dict:
    return {"id": row["id"], "username": row["username"], "role": effective_role(row), "bio": row["bio"]}


def publish(event: dict, user_ids: list[int] | None = None) -> None:
    message = json.dumps(event, separators=(",", ":"))
    with SESSIONS_LOCK:
        targets = [SESSIONS.get(str(uid)) for uid in user_ids] if user_ids is not None else list(SESSIONS.values())
        for q in targets:
            if q:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PanelWars/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def json_response(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self, max_bytes=1_000_000):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > max_bytes:
                raise ApiError(413, "That request is too large.")
            return json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, ValueError):
            raise ApiError(400, "Please send valid JSON.")

    def user(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = jar.get("pw_session")
        if not token:
            return None
        with connect() as db:
            row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>? AND u.banned_at IS NULL", (token.value, int(time.time()))).fetchone()
        return dict(row) if row else None

    def require_user(self):
        user = self.user()
        if not user:
            raise ApiError(401, "Sign in to continue.")
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
        try:
            if path == "/api/site-background-image":
                with connect() as db:
                    image = db.execute("SELECT custom_image,custom_mime FROM site_settings WHERE id=1").fetchone()
                if not image or not image["custom_image"] or image["custom_mime"] not in ("image/jpeg", "image/png", "image/webp"):
                    raise ApiError(404, "No custom background image is saved.")
                body = image["custom_image"]
                self.send_response(200)
                self.send_header("Content-Type", image["custom_mime"])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                self.wfile.write(body)
                return
            page_image = re.fullmatch(r"/api/manga/(\d+)/pages/(\d+)", path)
            if page_image:
                manga_id, page_id = map(int, page_image.groups())
                with connect() as db:
                    image = db.execute("""SELECT p.image_blob,p.mime_type
                      FROM manga_pages p JOIN manga m ON m.id=p.manga_id
                      WHERE p.id=? AND p.manga_id=? AND m.is_active=1""", (page_id,manga_id)).fetchone()
                if not image:
                    raise ApiError(404, "That manga page is no longer available.")
                body = image["image_blob"]
                self.send_response(200)
                self.send_header("Content-Type", image["mime_type"])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                self.wfile.write(body)
                return
            chapter_pdf = re.fullmatch(r"/api/manga/(\d+)/chapters/(\d+)\.pdf", path)
            if chapter_pdf:
                manga_id, chapter_number = map(int, chapter_pdf.groups())
                with connect() as db:
                    pdf = db.execute("""SELECT p.pdf_blob,p.file_name
                      FROM manga_chapter_pdfs p JOIN manga m ON m.id=p.manga_id
                      WHERE p.manga_id=? AND p.chapter_number=? AND m.is_active=1""", (manga_id,chapter_number)).fetchone()
                if not pdf:
                    raise ApiError(404, "That chapter PDF is no longer available.")
                body = pdf["pdf_blob"]
                safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", pdf["file_name"])[-120:] or "chapter.pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                self.wfile.write(body)
                return
            page_list = re.fullmatch(r"/api/manga/(\d+)/pages", path)
            if page_list:
                manga_id = int(page_list.group(1))
                try:
                    chapter = int(query["chapter"][0]) if "chapter" in query else None
                except (TypeError, ValueError):
                    raise ApiError(400, "Choose a valid chapter.")
                with connect() as db:
                    manga = db.execute("SELECT chapters FROM manga WHERE id=? AND is_active=1", (manga_id,)).fetchone()
                    if not manga:
                        raise ApiError(404, "Manga not found.")
                    if chapter is None:
                        rows = db.execute("""SELECT chapter_number chapter,COUNT(*) page_count,0 has_pdf
                          FROM manga_pages WHERE manga_id=? GROUP BY chapter_number
                          UNION ALL
                          SELECT chapter_number chapter,0 page_count,1 has_pdf
                          FROM manga_chapter_pdfs WHERE manga_id=?
                          ORDER BY chapter""", (manga_id,manga_id)).fetchall()
                        chapters = {}
                        for row in rows:
                            chapter_data = chapters.setdefault(row["chapter"], {"chapter":row["chapter"],"page_count":0,"has_pdf":False})
                            chapter_data["page_count"] += row["page_count"]
                            chapter_data["has_pdf"] = chapter_data["has_pdf"] or bool(row["has_pdf"])
                        self.json_response({"chapters": list(chapters.values())})
                        return
                    if not 1 <= chapter <= manga["chapters"]:
                        raise ApiError(400, "That chapter number is outside this manga’s chapter count.")
                    pdf = db.execute("SELECT file_name FROM manga_chapter_pdfs WHERE manga_id=? AND chapter_number=?", (manga_id,chapter)).fetchone()
                    if pdf:
                        self.json_response({"chapter":chapter,"has_pdf":True,"pdf_url":f"/api/manga/{manga_id}/chapters/{chapter}.pdf","file_name":pdf["file_name"],"pages":[]})
                        return
                    rows = db.execute("""SELECT id,page_number,file_name
                      FROM manga_pages WHERE manga_id=? AND chapter_number=? ORDER BY page_number""", (manga_id,chapter)).fetchall()
                self.json_response({"chapter": chapter, "pages": [{"page_number": r["page_number"], "file_name": r["file_name"], "url": f"/api/manga/{manga_id}/pages/{r['id']}"} for r in rows]})
                return
            if path == "/assets/favicon.png":
                data = (ROOT / "assets" / "favicon.png").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "public, max-age=3600"); self.end_headers(); self.wfile.write(data); return
            if path == "/assets/wars-logo.jpeg":
                data = (ROOT / "assets" / "wars-logo.jpeg").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "public, max-age=3600"); self.end_headers(); self.wfile.write(data); return
            if path == "/assets/street-background.jpg":
                data = (ROOT / "assets" / "street-background.jpg").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "public, max-age=3600"); self.end_headers(); self.wfile.write(data); return
            if path == "/assets/chat-warning-cat.png":
                data = (ROOT / "assets" / "chat-warning-cat.png").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "public, max-age=3600"); self.end_headers(); self.wfile.write(data); return
            if path == "/" or path == "/index.html":
                data = (ROOT / "index.html").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/health":
                self.json_response({"ok": True, "serverBuild": "custom-bg-upload-2026-10-19-2"}); return
            if path == "/api/bootstrap":
                user = self.user()
                with connect() as db:
                    manga = db.execute("""SELECT m.*, COALESCE(ROUND(AVG(r.rating),1),0) rating, COUNT(r.id) review_count,
                      (SELECT COUNT(*) FROM manga_pages p WHERE p.manga_id=m.id) page_count
                      FROM manga m LEFT JOIN reviews r ON r.manga_id=m.id WHERE m.is_active=1 GROUP BY m.id ORDER BY m.id""").fetchall()
                    shelf = []
                    if user:
                        shelf = [dict(r) for r in db.execute("SELECT s.manga_id,s.status,s.progress FROM shelf s JOIN manga m ON m.id=s.manga_id WHERE s.user_id=? AND m.is_active=1", (user["id"],))]
                    people = [public_user(r) for r in db.execute("SELECT id,username,COALESCE(staff_role,role) role,bio,staff_role FROM users WHERE banned_at IS NULL AND ((staff_role IS NULL AND role='reader') OR staff_role IN ('community_moderator','room_moderator')) ORDER BY CASE COALESCE(staff_role,role) WHEN 'community_moderator' THEN 0 WHEN 'room_moderator' THEN 1 ELSE 2 END, username")]
                    members = []
                    if user and effective_role(user) in ("owner", "admin", "community_moderator"):
                        members = [dict(r) for r in db.execute("SELECT id,username,COALESCE(staff_role,role) role,created_at,banned_at,ban_reason FROM users ORDER BY CASE COALESCE(staff_role,role) WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'community_moderator' THEN 2 WHEN 'room_moderator' THEN 3 WHEN 'editor' THEN 4 ELSE 5 END, username")]
                    mutes = []
                    if user and effective_role(user) in ("owner", "room_moderator"):
                        mutes = [dict(r) for r in db.execute("SELECT user_id,room_id,expires_at FROM chat_mutes WHERE expires_at IS NULL OR expires_at>?", (int(time.time()),))]
                    feedback = []
                    if user and effective_role(user) in ("owner", "admin"):
                        feedback = [dict(r) for r in db.execute("SELECT id,username,category,message,created_at,handled_at FROM feedback ORDER BY id DESC LIMIT 100")]
                    site_background = dict(db.execute("SELECT background,transition_ms,custom_name,CASE WHEN custom_image IS NOT NULL THEN 1 ELSE 0 END custom_available FROM site_settings WHERE id=1").fetchone())
                    if site_background["custom_available"]:
                        site_background["image_url"] = f"/api/site-background-image?v={int(time.time())}"
                self.json_response({"user": public_user(user) if user else None, "manga": [dict(r) for r in manga], "shelf": shelf, "people": people, "members": members, "mutes": mutes, "feedback": feedback, "siteBackground": site_background, "rooms": [{"id": a, "name": b, "description": c} for a,b,c in ROOMS], "shelfStatuses": ["Reading", "Plan to read", "Completed", "On hold", "Dropped"], "serverBuild": "custom-bg-upload-2026-10-19-2"}); return
            if path == "/api/reviews":
                mid = int(query.get("manga_id", ["0"])[0])
                with connect() as db:
                    rows = db.execute("SELECT r.id,r.rating,r.body,r.created_at,u.username,COALESCE(u.staff_role,u.role) role FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.manga_id=? ORDER BY r.id DESC", (mid,)).fetchall()
                self.json_response({"reviews": [dict(r) for r in rows]}); return
            if path == "/api/stream":
                user = self.require_user()
                self.send_response(200); self.send_header("Content-Type", "text/event-stream; charset=utf-8"); self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "keep-alive"); self.send_header("X-Accel-Buffering", "no"); self.end_headers()
                q = queue.Queue(maxsize=100)
                key = str(user["id"])
                with SESSIONS_LOCK: SESSIONS[key] = q
                try:
                    self.wfile.write(b": wars connected\n\n"); self.wfile.flush()
                    while True:
                        try: item = q.get(timeout=18); packet = ("data: " + item + "\n\n").encode()
                        except queue.Empty: packet = b": keepalive\n\n"
                        self.wfile.write(packet); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with SESSIONS_LOCK:
                        if SESSIONS.get(key) is q: SESSIONS.pop(key, None)
                return
            if path == "/api/messages":
                user = self.require_user(); room = query.get("room", ["general"])[0]; after = int(query.get("after", ["0"])[0])
                if room not in [r[0] for r in ROOMS]: raise ApiError(404, "Room not found.")
                with connect() as db:
                    rows = db.execute("SELECT x.id,x.room_id,x.body,x.created_at,u.id user_id,u.username,COALESCE(u.staff_role,u.role) role FROM room_messages x JOIN users u ON u.id=x.user_id WHERE x.room_id=? AND x.id>? ORDER BY x.id DESC LIMIT 100", (room, after)).fetchall()
                self.json_response({"messages": [dict(r) for r in reversed(rows)]}); return
            if path.startswith("/api/dm/") and path.endswith("/messages"):
                user = self.require_user(); cid = int(path.split("/")[3])
                with connect() as db:
                    conversation = db.execute("""SELECT
                      CASE WHEN c.user_a=? THEN COALESCE(ub.staff_role,ub.role) ELSE COALESCE(ua.staff_role,ua.role) END partner_role,
                      CASE WHEN c.user_a=? THEN ub.banned_at ELSE ua.banned_at END partner_banned
                      FROM conversations c JOIN users ua ON ua.id=c.user_a JOIN users ub ON ub.id=c.user_b
                      WHERE c.id=? AND (c.user_a=? OR c.user_b=?)""", (user["id"],user["id"],cid,user["id"],user["id"])).fetchone()
                    if effective_role(user) not in DM_ROLES or not conversation or conversation["partner_banned"] or conversation["partner_role"] not in DM_ROLES:
                        raise ApiError(404, "Conversation not found.")
                    rows = db.execute("SELECT d.id,d.body,d.created_at,d.sender_id,u.username,COALESCE(u.staff_role,u.role) role FROM direct_messages d JOIN users u ON u.id=d.sender_id WHERE d.conversation_id=? ORDER BY d.id DESC LIMIT 100", (cid,)).fetchall()
                self.json_response({"messages": [dict(r) for r in reversed(rows)]}); return
            if path == "/api/conversations":
                user = self.require_user()
                with connect() as db:
                    if effective_role(user) not in DM_ROLES:
                        rows = []
                    else:
                        rows = db.execute("""SELECT c.id, CASE WHEN c.user_a=? THEN ub.id ELSE ua.id END partner_id,
                      CASE WHEN c.user_a=? THEN ub.username ELSE ua.username END partner_username,
                      CASE WHEN c.user_a=? THEN COALESCE(ub.staff_role,ub.role) ELSE COALESCE(ua.staff_role,ua.role) END partner_role,
                      (SELECT body FROM direct_messages d WHERE d.conversation_id=c.id ORDER BY d.id DESC LIMIT 1) last_message
                      FROM conversations c JOIN users ua ON ua.id=c.user_a JOIN users ub ON ub.id=c.user_b
                      WHERE (c.user_a=? OR c.user_b=?)
                      AND CASE WHEN c.user_a=? THEN ub.banned_at ELSE ua.banned_at END IS NULL
                      AND CASE WHEN c.user_a=? THEN COALESCE(ub.staff_role,ub.role) ELSE COALESCE(ua.staff_role,ua.role) END IN ('reader','community_moderator','room_moderator')
                      ORDER BY c.id DESC""", (user["id"],user["id"],user["id"],user["id"],user["id"],user["id"],user["id"])).fetchall()
                self.json_response({"conversations": [dict(r) for r in rows]}); return
            self.send_error(404)
        except ApiError as e: self.json_response({"error": e.message}, e.status)
        except Exception as e: print("GET error:", repr(e)); self.json_response({"error": "Something went wrong."}, 500)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            is_page_upload = re.fullmatch(r"/api/manga/\d+/pages", path) is not None
            data = self.body_json(max_bytes=7_100_000 if path == "/api/site-event-background" else 28_100_000 if is_page_upload else 1_000_000)
            if path == "/api/register":
                username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
                if not 3 <= len(username) <= 24 or not username.replace("_", "").isalnum(): raise ApiError(400, "Choose a 3–24 character username using letters, numbers, or underscores.")
                if len(password) < 8: raise ApiError(400, "Use a password with at least 8 characters.")
                with connect() as db:
                    try: db.execute("INSERT INTO users(username,password_hash,role,bio,created_at) VALUES(?,?,'reader','',?)", (username,pw_hash(password),utcnow()))
                    except sqlite3.IntegrityError: raise ApiError(409, "That username is already taken.")
                    uid = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
                self.create_session(uid); return
            if path == "/api/login":
                username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
                with connect() as db: row = db.execute("SELECT id,password_hash,banned_at FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
                if not row or not pw_ok(password, row["password_hash"]): raise ApiError(401, "Username or password didn’t match.")
                if row["banned_at"]: raise ApiError(403, "This account is banned. Contact a community owner if you think this is a mistake.")
                self.create_session(row["id"]); return
            if path == "/api/feedback":
                category = str(data.get("category", "other"))
                message = str(data.get("message", "")).strip()
                if category not in ("suggestion", "bug", "other"): raise ApiError(400, "Choose a feedback type.")
                if not 5 <= len(message) <= 2000: raise ApiError(400, "Write feedback between 5 and 2,000 characters.")
                submitter = self.user()
                with connect() as db:
                    cur = db.execute("INSERT INTO feedback(user_id,username,category,message,created_at) VALUES(?,?,?,?,?)", (submitter["id"] if submitter else None,submitter["username"] if submitter else "Anonymous",category,message,utcnow()))
                    feedback = dict(db.execute("SELECT id,username,category,message,created_at,handled_at FROM feedback WHERE id=?", (cur.lastrowid,)).fetchone())
                    recipients = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned_at IS NULL AND id<>? AND COALESCE(staff_role,role) IN ('owner','admin')", (submitter["id"] if submitter else 0,))]
                publish({"type":"feedback_submitted","feedback":feedback}, recipients)
                self.json_response({"ok":True,"feedback_id":cur.lastrowid}); return
            if path == "/api/feedback/handle":
                user = self.require_user()
                if effective_role(user) not in ("owner", "admin"): raise ApiError(403, "Only the owner and admin can handle feedback.")
                feedback_id = int(data.get("feedback_id", 0))
                handled_at = utcnow()
                with connect() as db:
                    cur = db.execute("UPDATE feedback SET handled_at=? WHERE id=?", (handled_at, feedback_id))
                    if not cur.rowcount: raise ApiError(404, "Feedback not found.")
                    recipients = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned_at IS NULL AND COALESCE(staff_role,role) IN ('owner','admin')")]
                publish({"type":"feedback_handled","feedback_id":feedback_id,"handled_at":handled_at}, recipients)
                self.json_response({"ok":True,"handled_at":handled_at}); return
            user = self.require_user()
            if path == "/api/site-event-background":
                if effective_role(user) != "owner": raise ApiError(403, "Only the owner can change the site event background.")
                background = str(data.get("background", ""))
                try: transition_ms = int(data.get("transition_ms", 3000))
                except (TypeError, ValueError): raise ApiError(400, "Choose a valid transition speed.")
                if background not in EVENT_BACKGROUNDS and background != "custom": raise ApiError(400, "Choose a valid event background.")
                if transition_ms not in EVENT_TRANSITIONS: raise ApiError(400, "Choose a valid transition speed.")
                image_bytes = None
                image_mime = None
                image_name = None
                encoded_image = data.get("custom_image_b64")
                if encoded_image:
                    try:
                        image_bytes = base64.b64decode(str(encoded_image), validate=True)
                    except (binascii.Error, ValueError):
                        raise ApiError(400, "Choose a valid JPEG, PNG, or WebP image.")
                    if not image_bytes or len(image_bytes) > MAX_EVENT_IMAGE_BYTES:
                        raise ApiError(413, "Background images must be 5 MB or smaller.")
                    if image_bytes.startswith(b"\xff\xd8\xff"):
                        image_mime = "image/jpeg"
                    elif image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                        image_mime = "image/png"
                    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                        image_mime = "image/webp"
                    else:
                        raise ApiError(400, "Choose a valid JPEG, PNG, or WebP image.")
                    image_name = str(data.get("custom_image_name", "custom-background"))
                    image_name = re.sub(r"[\x00-\x1f\x7f]", "", image_name.replace("\\", "/").split("/")[-1]).strip()[:120] or "custom-background"
                settings = {"background": background, "transition_ms": transition_ms}
                updated_at = utcnow()
                with connect() as db:
                    if image_bytes is not None:
                        db.execute("UPDATE site_settings SET background=?,transition_ms=?,custom_image=?,custom_mime=?,custom_name=?,updated_at=? WHERE id=1", (background,transition_ms,image_bytes,image_mime,image_name,updated_at))
                    elif background == "custom":
                        saved = db.execute("SELECT custom_image FROM site_settings WHERE id=1").fetchone()
                        if not saved or not saved["custom_image"]:
                            raise ApiError(400, "Choose an image file for your custom background.")
                        db.execute("UPDATE site_settings SET background=?,transition_ms=?,updated_at=? WHERE id=1", (background,transition_ms,updated_at))
                    else:
                        db.execute("UPDATE site_settings SET background=?,transition_ms=?,updated_at=? WHERE id=1", (background,transition_ms,updated_at))
                    saved = db.execute("SELECT custom_name,CASE WHEN custom_image IS NOT NULL THEN 1 ELSE 0 END custom_available FROM site_settings WHERE id=1").fetchone()
                    settings.update(dict(saved))
                    if settings.get("custom_available") or settings["background"] == "custom":
                        settings["image_url"] = f"/api/site-background-image?v={int(time.time())}"
                    recipients = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned_at IS NULL")]
                publish({"type":"site_background_changed","settings":settings}, recipients)
                self.json_response({"ok":True,"settings":settings}); return
            if path == "/api/users/role":
                actor_role = effective_role(user)
                target_id, new_role = int(data.get("user_id", 0)), str(data.get("role", ""))
                allowed_roles = {"reader", "room_moderator", "community_moderator", "editor", "admin"}
                if new_role not in allowed_roles: raise ApiError(400, "Choose a valid member role.")
                if actor_role not in ("owner", "admin", "community_moderator"): raise ApiError(403, "You can’t manage member roles.")
                if target_id == user["id"]: raise ApiError(400, "You can’t change your own role.")
                with connect() as db:
                    target = db.execute("SELECT id,username,role,staff_role FROM users WHERE id=?", (target_id,)).fetchone()
                    if not target: raise ApiError(404, "Member not found.")
                    target_role = effective_role(target)
                    if target_role == "owner": raise ApiError(403, "Owner accounts are protected from role changes.")
                    moderator_roles = {"community_moderator", "room_moderator"}
                    if actor_role == "owner":
                        permitted = True
                    elif actor_role == "admin":
                        permitted = new_role in moderator_roles | {"reader"} and (target_role in moderator_roles or new_role in moderator_roles)
                    else:
                        permitted = new_role in {"room_moderator", "reader"} and (target_role == "room_moderator" or new_role == "room_moderator")
                    if not permitted: raise ApiError(403, "Your role can’t make that role change.")
                    if new_role in ("admin",): db.execute("UPDATE users SET role=?,staff_role=NULL WHERE id=?", (new_role,target_id))
                    elif new_role == "reader": db.execute("UPDATE users SET role='reader',staff_role=NULL WHERE id=?", (target_id,))
                    else: db.execute("UPDATE users SET role='reader',staff_role=? WHERE id=?", (new_role,target_id))
                    updated = db.execute("SELECT id,username,COALESCE(staff_role,role) role,created_at,banned_at,ban_reason FROM users WHERE id=?", (target_id,)).fetchone()
                self.json_response({"ok": True, "member": dict(updated)}); return
            if path == "/api/users/ban":
                if effective_role(user) != "owner": raise ApiError(403, "Only the owner can ban or unban accounts.")
                target_id, banned = int(data.get("user_id",0)), bool(data.get("banned",True))
                reason = str(data.get("reason","")).strip()[:240]
                if target_id == user["id"]: raise ApiError(400, "You can’t ban yourself.")
                with connect() as db:
                    target = db.execute("SELECT id,username,COALESCE(staff_role,role) role FROM users WHERE id=?", (target_id,)).fetchone()
                    if not target: raise ApiError(404, "Member not found.")
                    if target["role"] == "owner": raise ApiError(403, "Owner accounts are protected from bans.")
                    if banned:
                        db.execute("UPDATE users SET banned_at=?,ban_reason=? WHERE id=?", (utcnow(),reason,target_id))
                        db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
                    else:
                        db.execute("UPDATE users SET banned_at=NULL,ban_reason=NULL WHERE id=?", (target_id,))
                if banned: publish({"type":"account_banned"}, [target_id])
                self.json_response({"ok":True,"user_id":target_id,"banned":banned}); return
            if path in ("/api/users/mute", "/api/users/unmute"):
                actor_role = effective_role(user)
                target_id, room_id = int(data.get("user_id",0)), str(data.get("room_id","general"))
                if actor_role not in ("owner", "room_moderator"): raise ApiError(403, "Only the owner or a room moderator can mute readers.")
                if room_id not in [r[0] for r in ROOMS] and not (actor_role == "owner" and room_id == "*"): raise ApiError(400, "Choose a valid chat room.")
                with connect() as db:
                    target = db.execute("SELECT id,username,COALESCE(staff_role,role) role FROM users WHERE id=?", (target_id,)).fetchone()
                    if not target: raise ApiError(404, "Member not found.")
                    if target["role"] == "owner" or target_id == user["id"]: raise ApiError(403, "You can’t mute an owner or yourself.")
                    if actor_role == "room_moderator" and target["role"] != "reader": raise ApiError(403, "Room moderators can mute readers only.")
                    if path == "/api/users/unmute":
                        db.execute("DELETE FROM chat_mutes WHERE user_id=? AND room_id=?", (target_id,room_id))
                    else:
                        minutes = max(1,min(10080,int(data.get("minutes",60))))
                        reason = str(data.get("reason","Inappropriate language in chat")).strip()[:240]
                        db.execute("INSERT INTO chat_mutes(user_id,room_id,muted_by,reason,expires_at,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,room_id) DO UPDATE SET muted_by=excluded.muted_by,reason=excluded.reason,expires_at=excluded.expires_at,created_at=excluded.created_at", (target_id,room_id,user["id"],reason,int(time.time())+minutes*60,utcnow()))
                self.json_response({"ok":True,"user_id":target_id,"room_id":room_id,"muted":path.endswith("/mute")}); return
            if path == "/api/manga":
                if effective_role(user) not in ("owner", "editor"): raise ApiError(403, "Only an owner or editor can add manga to the catalogue.")
                title, author = str(data.get("title","")).strip(), str(data.get("author","")).strip()
                genres, synopsis = str(data.get("genres","")).strip(), str(data.get("synopsis","")).strip()
                unit_type = str(data.get("unit_type","chapter")).strip().lower()
                status = str(data.get("status","Ongoing"))
                try: chapters = int(data.get("chapters",0))
                except (TypeError,ValueError): chapters = 0
                if not title or len(title)>120 or not author or len(author)>100: raise ApiError(400, "Add a title and author within the length limits.")
                if not genres or len(genres)>180 or not synopsis or len(synopsis)>1800: raise ApiError(400, "Add genres and a synopsis within the length limits.")
                if not 1<=chapters<=100000 or unit_type not in ("chapter","volume") or status not in ("Ongoing","Completed","Hiatus"): raise ApiError(400, "Choose a valid chapter or volume count and publication status.")
                with connect() as db:
                    db.execute("INSERT INTO manga(title,author,genres,synopsis,chapters,unit_type,status,palette,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,1,?) ON CONFLICT(title) DO UPDATE SET author=excluded.author,genres=excluded.genres,synopsis=excluded.synopsis,chapters=excluded.chapters,unit_type=excluded.unit_type,status=excluded.status,palette=excluded.palette,is_active=1", (title,author,genres,synopsis,chapters,unit_type,status,"#a8493b|#d5a16c|#443644",utcnow()))
                    manga_id = db.execute("SELECT id FROM manga WHERE title=?", (title,)).fetchone()[0]
                self.json_response({"ok":True,"manga_id":manga_id}); return
            page_upload = re.fullmatch(r"/api/manga/(\d+)/pages", path)
            if page_upload:
                user = self.require_user()
                if effective_role(user) not in ("owner", "editor"):
                    raise ApiError(403, "Only an owner or editor can upload manga pages.")
                manga_id = int(page_upload.group(1))
                try:
                    chapter_number = int(data.get("chapter", 0))
                except (TypeError, ValueError):
                    chapter_number = 0
                image_data = str(data.get("image", ""))
                file_name = str(data.get("file_name", "page")).replace("\\", "/").rsplit("/", 1)[-1].strip()
                file_name = re.sub(r"[\x00-\x1f\x7f]", "", file_name)[:200] or "page"
                if not 1 <= len(image_data) <= 7_000_000:
                    raise ApiError(400, "Choose one image up to 5 MB.")
                try:
                    image_bytes = base64.b64decode(image_data, validate=True)
                except (binascii.Error, ValueError):
                    raise ApiError(400, "That page image could not be read. Choose a JPEG, PNG, or WebP image.")
                if not image_bytes:
                    raise ApiError(400, "That upload is empty.")
                if image_bytes.startswith(b"%PDF-"):
                    if len(image_bytes) > MAX_MANGA_PDF_BYTES:
                        raise ApiError(413, "Each chapter PDF must be 20 MB or smaller.")
                    with connect() as db:
                        manga = db.execute("SELECT chapters,unit_type FROM manga WHERE id=? AND is_active=1", (manga_id,)).fetchone()
                        if not manga:
                            raise ApiError(404, "Manga not found.")
                        if not 1 <= chapter_number <= manga["chapters"]:
                            raise ApiError(400, f"Choose a {manga['unit_type']} number listed for this manga.")
                        db.execute("DELETE FROM manga_pages WHERE manga_id=? AND chapter_number=?", (manga_id,chapter_number))
                        db.execute("""INSERT INTO manga_chapter_pdfs(manga_id,chapter_number,file_name,pdf_blob,uploaded_by,uploaded_at)
                          VALUES(?,?,?,?,?,?) ON CONFLICT(manga_id,chapter_number) DO UPDATE SET
                          file_name=excluded.file_name,pdf_blob=excluded.pdf_blob,uploaded_by=excluded.uploaded_by,uploaded_at=excluded.uploaded_at""",
                          (manga_id,chapter_number,file_name,image_bytes,user["id"],utcnow()))
                    self.json_response({"ok":True,"manga_id":manga_id,"chapter":chapter_number,"format":"pdf"}); return
                if len(image_bytes) > MAX_MANGA_PAGE_BYTES:
                    raise ApiError(413, "Each manga page must be 5 MB or smaller.")
                if image_bytes.startswith(b"\xff\xd8\xff"):
                    mime_type = "image/jpeg"
                elif image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime_type = "image/png"
                elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                    mime_type = "image/webp"
                else:
                    raise ApiError(400, "Use a JPEG, PNG, or WebP image file.")
                with connect() as db:
                    manga = db.execute("SELECT chapters,unit_type FROM manga WHERE id=? AND is_active=1", (manga_id,)).fetchone()
                    if not manga:
                        raise ApiError(404, "Manga not found.")
                    if not 1 <= chapter_number <= manga["chapters"]:
                        raise ApiError(400, f"Choose a {manga['unit_type']} number listed for this manga.")
                    page_number = db.execute("SELECT COALESCE(MAX(page_number),0)+1 FROM manga_pages WHERE manga_id=? AND chapter_number=?", (manga_id,chapter_number)).fetchone()[0]
                    if page_number > MAX_MANGA_CHAPTER_PAGES:
                        raise ApiError(400, "This chapter has reached the 1,000 page limit.")
                    db.execute("DELETE FROM manga_chapter_pdfs WHERE manga_id=? AND chapter_number=?", (manga_id,chapter_number))
                    db.execute("""INSERT INTO manga_pages(manga_id,chapter_number,page_number,mime_type,file_name,image_blob,uploaded_by,uploaded_at)
                      VALUES(?,?,?,?,?,?,?,?)""", (manga_id,chapter_number,page_number,mime_type,file_name,image_bytes,user["id"],utcnow()))
                self.json_response({"ok":True,"manga_id":manga_id,"chapter":chapter_number,"page_number":page_number}); return
            if path.startswith("/api/manga/") and path.endswith("/remove"):
                if effective_role(user) not in ("owner", "editor"): raise ApiError(403, "Only an owner or editor can remove manga from the catalogue.")
                try: manga_id = int(path.split("/")[3])
                except (IndexError,ValueError): raise ApiError(404, "Manga not found.")
                with connect() as db:
                    cur = db.execute("UPDATE manga SET is_active=0 WHERE id=? AND is_active=1", (manga_id,))
                    if not cur.rowcount: raise ApiError(404, "Manga not found.")
                self.json_response({"ok":True,"manga_id":manga_id,"removed":True}); return
            if path == "/api/logout":
                jar = cookies.SimpleCookie(self.headers.get("Cookie", "")); token = jar.get("pw_session")
                if token:
                    with connect() as db: db.execute("DELETE FROM sessions WHERE token=?", (token.value,))
                self.json_response({"ok": True}, headers={"Set-Cookie": f"pw_session=; Path=/; HttpOnly; SameSite=Lax{COOKIE_SECURE}; Max-Age=0"}); return
            if path == "/api/reviews":
                mid, rating, body = int(data.get("manga_id",0)), int(data.get("rating",0)), str(data.get("body","")).strip()
                if not 1 <= rating <= 10: raise ApiError(400, "Rating must be from 1 to 10.")
                if not 8 <= len(body) <= 1200: raise ApiError(400, "Write a review between 8 and 1,200 characters.")
                with connect() as db:
                    if not db.execute("SELECT 1 FROM manga WHERE id=? AND is_active=1", (mid,)).fetchone(): raise ApiError(404, "Manga not found.")
                    db.execute("INSERT INTO reviews(user_id,manga_id,rating,body,created_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,manga_id) DO UPDATE SET rating=excluded.rating,body=excluded.body,created_at=excluded.created_at", (user["id"],mid,rating,body,utcnow()))
                    rows = db.execute("SELECT r.id,r.rating,r.body,r.created_at,u.username,COALESCE(u.staff_role,u.role) role FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.manga_id=? ORDER BY r.id DESC", (mid,)).fetchall()
                    avg = db.execute("SELECT ROUND(AVG(rating),1) FROM reviews WHERE manga_id=?", (mid,)).fetchone()[0]
                self.json_response({"ok": True, "reviews": [dict(r) for r in rows], "rating": avg}); return
            if path == "/api/shelf":
                mid, status, progress = int(data.get("manga_id",0)), str(data.get("status","Reading")), max(0,int(data.get("progress",0)))
                if status not in ("Reading","Plan to read","Completed","On hold","Dropped"): raise ApiError(400, "Choose a valid shelf status.")
                with connect() as db:
                    manga = db.execute("SELECT chapters FROM manga WHERE id=? AND is_active=1", (mid,)).fetchone()
                    if not manga: raise ApiError(404, "Manga not found.")
                    progress = min(progress, manga["chapters"])
                    db.execute("INSERT INTO shelf(user_id,manga_id,status,progress,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,manga_id) DO UPDATE SET status=excluded.status,progress=excluded.progress,updated_at=excluded.updated_at", (user["id"],mid,status,progress,utcnow()))
                self.json_response({"ok": True}); return
            if path == "/api/chat/remove":
                actor_role = effective_role(user)
                if actor_role != "owner": raise ApiError(403, "Only the owner can remove chat messages.")
                try: message_id = int(data.get("message_id", 0))
                except (TypeError, ValueError): raise ApiError(400, "Choose a valid chat message.")
                with connect() as db:
                    message = db.execute("SELECT x.id,x.room_id,x.user_id,COALESCE(u.staff_role,u.role) role FROM room_messages x JOIN users u ON u.id=x.user_id WHERE x.id=?", (message_id,)).fetchone()
                    if not message: raise ApiError(404, "That chat message is no longer available.")
                    db.execute("DELETE FROM room_messages WHERE id=?", (message_id,))
                    recipients = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned_at IS NULL")]
                event = {"type":"chat_message_removed","message_id":message_id,"room_id":message["room_id"]}
                publish(event, recipients)
                self.json_response({"ok":True,"message_id":message_id,"room_id":message["room_id"]}); return
            if path == "/api/chat":
                room, body = str(data.get("room_id", "")), str(data.get("body", "")).strip()
                if room not in [r[0] for r in ROOMS]: raise ApiError(404, "Room not found.")
                if not 1 <= len(body) <= 700: raise ApiError(400, "Messages can be 1–700 characters.")
                flagged = has_inappropriate_chat_language(body)
                with connect() as db:
                    mute = db.execute("SELECT expires_at FROM chat_mutes WHERE user_id=? AND room_id IN (?, '*') AND (expires_at IS NULL OR expires_at>?) ORDER BY room_id DESC LIMIT 1", (user["id"], room, int(time.time()))).fetchone()
                    if mute: raise ApiError(403, "You’re muted in this chat room for now.")
                    if flagged:
                        db.execute("INSERT INTO chat_language_violations(user_id,violation_count,last_at) VALUES(?,1,?) ON CONFLICT(user_id) DO UPDATE SET violation_count=chat_language_violations.violation_count+1,last_at=excluded.last_at", (user["id"], utcnow()))
                        violation_count = db.execute("SELECT violation_count FROM chat_language_violations WHERE user_id=?", (user["id"],)).fetchone()["violation_count"]
                        moderator_ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned_at IS NULL AND id<>? AND COALESCE(staff_role,role) IN ('owner','admin')", (user["id"],))] if violation_count > 1 else []
                    else:
                        cur = db.execute("INSERT INTO room_messages(room_id,user_id,body,created_at) VALUES(?,?,?,?)", (room, user["id"], body, utcnow()))
                        row = db.execute("SELECT x.id,x.room_id,x.body,x.created_at,u.id user_id,u.username,COALESCE(u.staff_role,u.role) role FROM room_messages x JOIN users u ON u.id=x.user_id WHERE x.id=?", (cur.lastrowid,)).fetchone()
                if flagged:
                    if violation_count > 1:
                        room_name = next(name for room_id, name, _ in ROOMS if room_id == room)
                        publish({"type": "chat_language_alert", "room_id": room, "room_name": room_name, "username": user["username"]}, moderator_ids)
                        raise ApiError(400, "Your message was blocked. The owner and admins have been notified because this is a repeat offense.")
                    raise ApiError(400, "Your message was blocked for inappropriate language. If you send another inappropriate message, the owner and admins will be notified.")
                message = dict(row); publish({"type": "room_message", "message": message}); self.json_response({"ok": True, "message": message}); return
            if path == "/api/dm":
                to_id, body = int(data.get("user_id",0)), str(data.get("body","")).strip()
                if not 1 <= len(body) <= 700: raise ApiError(400, "Messages can be 1–700 characters.")
                if to_id == user["id"]: raise ApiError(400, "You can’t message yourself.")
                if effective_role(user) not in DM_ROLES: raise ApiError(403, "Direct messages are available between readers and moderators only.")
                with connect() as db:
                    recipient = db.execute("SELECT id,username,COALESCE(staff_role,role) role,banned_at FROM users WHERE id=?", (to_id,)).fetchone()
                    if not recipient: raise ApiError(404, "Reader not found.")
                    if recipient["banned_at"]: raise ApiError(404, "Reader not found.")
                    if recipient["role"] not in DM_ROLES:
                        raise ApiError(403, "Direct messages are available between readers and moderators only.")
                    low, high = sorted([user["id"],to_id])
                    db.execute("INSERT OR IGNORE INTO conversations(user_a,user_b,created_at) VALUES(?,?,?)", (low,high,utcnow()))
                    cid = db.execute("SELECT id FROM conversations WHERE user_a=? AND user_b=?", (low,high)).fetchone()[0]
                    cur = db.execute("INSERT INTO direct_messages(conversation_id,sender_id,body,created_at) VALUES(?,?,?,?)", (cid,user["id"],body,utcnow()))
                    msg = db.execute("SELECT d.id,d.conversation_id,d.body,d.created_at,d.sender_id,u.username,COALESCE(u.staff_role,u.role) role FROM direct_messages d JOIN users u ON u.id=d.sender_id WHERE d.id=?", (cur.lastrowid,)).fetchone()
                    msg = dict(msg)
                publish({"type":"direct_message","message":msg}, [user["id"],to_id]); self.json_response({"ok":True,"conversation_id":cid,"message":msg}); return
            raise ApiError(404, "That action isn’t available.")
        except ApiError as e: self.json_response({"error": e.message}, e.status)
        except Exception as e: print("POST error:", repr(e)); self.json_response({"error": "Something went wrong."}, 500)

    def create_session(self, uid):
        token = secrets.token_urlsafe(32)
        with connect() as db: db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token,uid,int(time.time())+60*60*24*30))
        with connect() as db: user = db.execute("SELECT id,username,role,staff_role,bio FROM users WHERE id=?", (uid,)).fetchone()
        self.json_response({"ok":True,"user":public_user(user)}, headers={"Set-Cookie": f"pw_session={token}; Path=/; HttpOnly; SameSite=Lax{COOKIE_SECURE}; Max-Age=2592000"})


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"WARS is ready at http://127.0.0.1:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nClosing WARS…")
    finally: server.server_close()


if __name__ == "__main__": main()
