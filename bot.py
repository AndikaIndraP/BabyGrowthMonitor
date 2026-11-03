#!/usr/bin/env python3
"""
bot.py - BabyGrowth Monitor (Telegram Bot + MQTT client + SQLite)

Dijalankan di komputer/server (BUKAN di Wokwi). Tugas:
  1. Subscribe MQTT, validasi, lalu simpan pengukuran ke SQLite (babygrowth.db)
  2. Melayani menu Telegram (inline keyboard) sebagai satu-satunya antarmuka
  3. Mengirim pengingat terjadwal dan memantau status perangkat (ESP32)

PROTOTIPE PEMANTAUAN, BUKAN ALAT DIAGNOSIS MEDIS. Data berat & tinggi berasal dari
load cell + sensor ultrasonik (simulasi Wokwi). Bot membandingkan data dengan acuan WHO (0-24 bulan) hanya
sebagai PEMERIKSAAN AWAL (perlu diperiksa / tidak); diagnosis stunting oleh tenaga kesehatan.
"""

import html
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import who_standards
import paho.mqtt.client as mqtt
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ============================================================
# KONFIGURASI (dibaca dari .env, jangan hardcode rahasia di sini)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("babygrowth")


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        sys.exit(f"Nilai {name} di .env harus berupa angka bulat.")


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.isdigit()
}

MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com").strip()
MQTT_PORT = _int_env("MQTT_PORT", 1883)
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "").strip()
MQTT_TLS = os.getenv("MQTT_TLS", "false").strip().lower() in ("1", "true", "yes")
# Harus sama dengan TOPIC_BASE di main.py (ESP32)
MQTT_TOPIC_BASE = os.getenv(
    "MQTT_TOPIC_BASE", "babygrowth/babygrowth123/device01"
).strip().strip("/")
TOPIC_MEASUREMENT = MQTT_TOPIC_BASE + "/measurement"
TOPIC_STATUS = MQTT_TOPIC_BASE + "/status"

DB_PATH = os.getenv("DB_PATH", "babygrowth.db").strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(BASE_DIR, DB_PATH)
OFFLINE_SECONDS = _int_env("DEVICE_OFFLINE_SECONDS", 45)
TZ_OFFSET_HOURS = _int_env("TZ_OFFSET_HOURS", 7)  # 7 = WIB

LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
TZ_LABEL = "WIB" if TZ_OFFSET_HOURS == 7 else f"UTC{TZ_OFFSET_HOURS:+d}"

# Aturan penanda TEKNIS (bukan interpretasi kesehatan)
SIM_MIN_KG, SIM_MAX_KG = 2.0, 20.0     # rentang input simulasi (sama dengan main.py)
HEIGHT_MIN_CM, HEIGHT_MAX_CM = 30.0, 130.0
JUMP_KG, JUMP_DAYS = 2.0, 7            # perubahan >= 2 kg dalam <= 7 hari
DROP_FRACTION = 0.10                   # penurunan >= 10% dari pengukuran sebelumnya
REMINDER_WINDOW_MIN = 10               # pengingat dikirim maks 10 menit setelah jadwal
MAX_AGE_YEARS = 6                      # batas usia input (bayi dan balita)

DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
DISCLAIMER = (
    "ℹ️ <i>Prototipe pemantauan dengan data simulasi, bukan alat diagnosis medis. "
    "Konsultasikan kondisi anak ke tenaga kesehatan.</i>"
)

# ============================================================
# UTILITAS WAKTU
# ============================================================
def utc_now():
    return datetime.now(timezone.utc)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_now():
    return datetime.now(LOCAL_TZ)


def fmt_dt(iso_str):
    dt = parse_iso(iso_str)
    if not dt:
        return "-"
    return dt.astimezone(LOCAL_TZ).strftime("%d-%m-%Y %H:%M") + " " + TZ_LABEL


def fmt_short_date(iso_str):
    dt = parse_iso(iso_str)
    return dt.astimezone(LOCAL_TZ).strftime("%d-%m-%Y %H:%M") if dt else "-"


def ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} detik lalu"
    if seconds < 3600:
        return f"{seconds // 60} menit lalu"
    if seconds < 86400:
        return f"{seconds // 3600} jam lalu"
    return f"{seconds // 86400} hari lalu"


esc = html.escape

# ============================================================
# DATABASE (SQLite)
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS children (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    birth_date  TEXT NOT NULL,
    sex         TEXT NOT NULL CHECK (sex IN ('L','P')),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id  TEXT NOT NULL UNIQUE,
    device_id       TEXT NOT NULL,
    child_id        INTEGER REFERENCES children(id),
    weight_kg       REAL NOT NULL,
    height_cm       REAL,
    height_manual   INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL,
    measured_at     TEXT NOT NULL,
    ts_from_device  INTEGER NOT NULL DEFAULT 1,
    received_at     TEXT NOT NULL,
    discarded       INTEGER NOT NULL DEFAULT 0,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    reviewed_by     INTEGER,
    reviewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_meas_child ON measurements (child_id, measured_at);
CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    child_id        INTEGER REFERENCES children(id),
    hour            INTEGER NOT NULL,
    minute          INTEGER NOT NULL,
    repeat          TEXT NOT NULL CHECK (repeat IN ('once','daily','weekly')),
    weekday         INTEGER,
    once_date       TEXT,
    last_sent_date  TEXT,
    created_at      TEXT NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    log.info("Database siap: %s", DB_PATH)


def q_all(sql, params=()):
    with closing(db()) as conn:
        return conn.execute(sql, params).fetchall()


def q_one(sql, params=()):
    with closing(db()) as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql, params=()):
    """Jalankan INSERT/UPDATE/DELETE. Return (lastrowid, rowcount)."""
    with closing(db()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid, cur.rowcount


def get_child(cid):
    return q_one("SELECT * FROM children WHERE id = ?", (cid,))


def child_code(cid):
    return f"A{cid:03d}"


def child_label(row):
    return f"{child_code(row['id'])} · {row['name']}"


def age_text(birth_date):
    try:
        b = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError:
        return "-"
    t = local_now().date()
    months = (t.year - b.year) * 12 + (t.month - b.month) - (1 if t.day < b.day else 0)
    months = max(0, months)
    if months >= 12:
        return f"{months // 12} th {months % 12} bln"
    return f"{months} bln"


# ============================================================
# STATUS PERANGKAT (diisi oleh thread MQTT, dibaca oleh bot)
# ============================================================
DEV = {
    "last_seen": None,       # time.monotonic() pesan terakhir (non-retained)
    "last_seen_utc": None,   # datetime UTC
    "state": None,           # 'online' / 'offline' / None
    "uptime": None,
    "mqtt_ok": False,
    "error": None,
}
DEV_LOCK = threading.Lock()


def dev_update(**kw):
    with DEV_LOCK:
        DEV.update(kw)


def dev_touch(state="online", uptime=None):
    with DEV_LOCK:
        DEV["last_seen"] = time.monotonic()
        DEV["last_seen_utc"] = utc_now()
        DEV["state"] = state
        if uptime is not None:
            DEV["uptime"] = uptime


def device_status_text():
    with DEV_LOCK:
        d = dict(DEV)
    elapsed = None if d["last_seen"] is None else time.monotonic() - d["last_seen"]
    online = elapsed is not None and elapsed <= OFFLINE_SECONDS and d["state"] != "offline"

    lines = ["📡 <b>Status Perangkat</b>", ""]
    lines.append("ESP32: " + ("🟢 <b>ONLINE</b>" if online else "🔴 <b>OFFLINE</b>"))
    if d["last_seen_utc"]:
        lines.append(
            f"Data terakhir diterima: {fmt_dt(to_iso(d['last_seen_utc']))} ({ago(elapsed)})"
        )
    else:
        last = q_one("SELECT received_at FROM measurements ORDER BY received_at DESC LIMIT 1")
        lines.append("Belum ada pesan dari perangkat sejak bot dijalankan.")
        if last:
            lines.append(f"Pengukuran terakhir di database: {fmt_dt(last['received_at'])}")
    if online and d["uptime"] is not None:
        lines.append(f"Uptime ESP32: {d['uptime']} detik")
    lines.append("")
    if d["mqtt_ok"]:
        lines.append(f"Koneksi bot ke broker MQTT: 🟢 terhubung ({esc(MQTT_BROKER)}:{MQTT_PORT})")
    else:
        lines.append(f"Koneksi bot ke broker MQTT: 🔴 tidak terhubung ({esc(MQTT_BROKER)}:{MQTT_PORT})")
        if d["error"]:
            lines.append(f"Pesan kesalahan: {esc(str(d['error']))}")
    lines.append("")
    lines.append(
        f"<i>ESP32 dianggap offline jika tidak ada pesan lebih dari {OFFLINE_SECONDS} detik "
        f"(heartbeat dikirim tiap 10 detik).</i>"
    )
    return "\n".join(lines)


# ============================================================
# MQTT
# ============================================================
ID_PATTERN = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")


def handle_measurement(payload):
    """Validasi payload JSON lalu simpan. Return True jika tersimpan baru."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("Payload pengukuran bukan JSON valid, diabaikan")
        return False
    if not isinstance(data, dict):
        log.warning("Payload pengukuran bukan objek JSON, diabaikan")
        return False

    mid = data.get("measurement_id")
    if not isinstance(mid, str) or not ID_PATTERN.match(mid):
        log.warning("measurement_id tidak valid, diabaikan")
        return False

    device_id = data.get("device_id")
    if not isinstance(device_id, str) or not ID_PATTERN.match(device_id):
        device_id = "unknown"

    w = data.get("weight_kg")
    if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0 or w > 500:
        log.warning("weight_kg tidak valid (%r), diabaikan", w)
        return False

    h = data.get("height_cm")
    if h is None:
        height = None
    elif (isinstance(h, bool) or not isinstance(h, (int, float)) or not math.isfinite(h)
          or not HEIGHT_MIN_CM <= h <= HEIGHT_MAX_CM):
        log.warning("height_cm tidak valid (%r), diabaikan (tinggi dianggap kosong)", h)
        height = None
    else:
        height = float(h)

    source = str(data.get("source", "unknown"))[:20]
    received = utc_now()
    ts = parse_iso(data.get("timestamp")) if isinstance(data.get("timestamp"), str) else None
    ts_ok = ts is not None and abs((received - ts).total_seconds()) <= 86400
    measured_at = to_iso(ts) if ts_ok else to_iso(received)

    # child_id dari payload SENGAJA diabaikan: asosiasi hanya lewat konfirmasi di Telegram
    # agar pengukuran tidak terkait ke anak yang salah.
    _, rc = execute(
        """INSERT OR IGNORE INTO measurements
           (measurement_id, device_id, child_id, weight_kg, height_cm, height_manual,
            source, measured_at, ts_from_device, received_at)
           VALUES (?, ?, NULL, ?, ?, 0, ?, ?, ?, ?)""",
        (mid, device_id, float(w), height, source, measured_at, 1 if ts_ok else 0, to_iso(received)),
    )
    dev_touch(state="online")
    if rc == 0:
        log.info("Pengukuran duplikat diabaikan: %s", mid)
        return False
    log.info("Pengukuran tersimpan: %s = %.1f kg, tinggi %s", mid, w,
             f"{height:.1f} cm" if height is not None else "-")
    return True


def handle_status(payload, retained):
    try:
        data = json.loads(payload.decode("utf-8"))
        state = data.get("state") if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        return
    if state not in ("online", "offline"):
        return
    if retained:
        # Pesan retained = kondisi lama dari broker, jangan dianggap 'baru saja online'.
        if state == "offline":
            dev_update(state="offline")
        return
    if state == "online":
        up = data.get("uptime")
        dev_touch(state="online", uptime=up if isinstance(up, int) else None)
    else:
        dev_update(state="offline")


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code.is_failure:
        dev_update(mqtt_ok=False, error=f"Gagal login ke broker: {reason_code}")
        log.error("MQTT gagal terhubung: %s", reason_code)
        return
    dev_update(mqtt_ok=True, error=None)
    client.subscribe([(TOPIC_MEASUREMENT, 1), (TOPIC_STATUS, 1)])
    log.info("MQTT terhubung, subscribe: %s dan %s", TOPIC_MEASUREMENT, TOPIC_STATUS)


def on_mqtt_connect_fail(client, userdata):
    dev_update(mqtt_ok=False, error="Tidak dapat menjangkau broker (cek internet/alamat/port)")
    log.error("MQTT gagal menjangkau broker, mencoba lagi...")


def on_mqtt_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    dev_update(mqtt_ok=False, error=f"Koneksi ke broker terputus ({reason_code}), mencoba tersambung ulang")
    log.warning("MQTT terputus: %s", reason_code)


def on_mqtt_message(client, userdata, msg):
    try:
        if msg.topic == TOPIC_MEASUREMENT:
            handle_measurement(msg.payload)
        elif msg.topic == TOPIC_STATUS:
            handle_status(msg.payload, bool(msg.retain))
    except Exception:
        log.exception("Gagal memproses pesan MQTT")


def start_mqtt():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"babygrowth-bot-{os.getpid()}-{int(time.time())}",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_TLS:
        client.tls_set()
    client.on_connect = on_mqtt_connect
    client.on_connect_fail = on_mqtt_connect_fail
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()  # thread terpisah, auto-reconnect
    return client


# ============================================================
# PEMERIKSAAN AWAL ACUAN WHO (0-24 bulan) - BUKAN DIAGNOSIS
# ============================================================
def age_months_at(child, at_iso):
    """Usia anak (bulan, desimal) pada saat pengukuran."""
    try:
        b = datetime.strptime(child["birth_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    t = parse_iso(at_iso)
    if not t:
        return None
    days = (t.astimezone(LOCAL_TZ).date() - b).days
    return days / 30.4375 if days >= 0 else None


def who_assess_row(child, r):
    if not child:
        return None
    age_m = age_months_at(child, r["measured_at"])
    return who_standards.assess(child["sex"], age_m, r["weight_kg"], r["height_cm"])


def who_flags(res):
    """Alasan 'perlu diperiksa' dari hasil who_standards.assess."""
    out = []
    if not res:
        return out
    h, w = res["height"], res["weight"]
    if h and h["status"] != "normal":
        lvl = "-3 SD (sangat pendek)" if h["status"] == "severe" else "-2 SD (pendek)"
        out.append(
            f"Tinggi {h['value']:.1f} cm di bawah {lvl} acuan WHO "
            f"(batas -2 SD usia ini ≈ {h['sd2']:.1f} cm), indikasi stunting, perlu pemeriksaan tenaga kesehatan"
        )
    if w and w["status"] != "normal":
        lvl = "-3 SD (sangat kurang)" if w["status"] == "severe" else "-2 SD (kurang)"
        out.append(
            f"Berat {w['value']:.1f} kg di bawah {lvl} acuan WHO "
            f"(batas -2 SD usia ini ≈ {w['sd2']:.1f} kg), perlu pemeriksaan tenaga kesehatan"
        )
    return out


def who_text(child, r):
    """Blok teks pemeriksaan awal WHO untuk satu pengukuran."""
    if not child:
        return ""
    res = who_assess_row(child, r)
    head = "🩺 <b>Pemeriksaan awal (acuan WHO)</b>\n"
    if res is None:
        return head + (
            f"Di luar cakupan tabel (usia {who_standards.MAX_MONTHS} bulan ke bawah), "
            "pemeriksaan acuan WHO tidak dilakukan.\n"
        )
    sex = "laki-laki" if child["sex"] == "L" else "perempuan"
    lines = [head + f"Usia saat diukur: {res['age_m']:.1f} bln ({sex})"]
    names = {"normal": "tidak di bawah -2 SD", "low": "di bawah -2 SD", "severe": "di bawah -3 SD"}
    icons = {"normal": "✅", "low": "⚠️", "severe": "🚨"}
    h, w = res["height"], res["weight"]
    if h:
        lines.append(
            f"{icons[h['status']]} Tinggi {h['value']:.1f} cm ≈ {h['z']:+.1f} SD, {names[h['status']]} "
            f"(batas -2 SD ≈ {h['sd2']:.1f} cm, median {h['median']:.1f} cm)"
        )
        if h["status"] != "normal":
            lines.append("   → <b>indikasi stunting</b> (tinggi menurut umur)")
    else:
        lines.append("➖ Tinggi tidak tersedia, penilaian stunting belum bisa dilakukan")
    if w:
        lines.append(
            f"{icons[w['status']]} Berat {w['value']:.1f} kg ≈ {w['z']:+.1f} SD, {names[w['status']]} "
            f"(batas -2 SD ≈ {w['sd2']:.1f} kg, median {w['median']:.1f} kg)"
        )
        if w["status"] != "normal":
            lines.append("   → berat badan kurang menurut umur")
    if res["need_review"]:
        lines.append("\n⚠️ <b>PERLU DIPERIKSA</b> oleh tenaga kesehatan (posyandu/puskesmas).")
        if h and h["status"] != "normal":
            lines.append(
                "<i>Ciri yang perlu diperhatikan: tubuh lebih pendek dibanding anak seusia, "
                "pertumbuhan melambat, berat badan kurang, lebih sering sakit.</i>"
            )
    elif h:
        lines.append("\n✅ <b>Tidak perlu pemeriksaan lanjutan</b> berdasarkan data ini. Tetap pantau rutin.")
    else:
        lines.append("\nℹ️ Lengkapi tinggi badan untuk pemeriksaan stunting.")
    lines.append(
        "<i>Z-score: tabel LMS resmi WHO.</i>" if res.get("method") == "LMS"
        else "<i>Z-score: perkiraan interpolasi (tabel LMS WHO belum dimuat).</i>"
    )
    lines.append("<i>Ini penapisan awal dari data simulasi, bukan diagnosis stunting.</i>")
    return "\n".join(lines) + "\n"


async def send_who_alert(bot, child, m, res=None):
    """Peringatan OTOMATIS ke semua pengguna terdaftar bila ada nilai di bawah -2 SD.

    Dikirim segera setelah pengukuran dikaitkan (tanpa menunggu menu dibuka).
    Kegagalan kirim hanya dicatat, tidak mengganggu alur utama.
    """
    res = res or who_assess_row(child, m)
    if not res or not res["need_review"]:
        return
    flags = who_flags(res)
    severe = any(res[k] and res[k]["status"] == "severe" for k in ("height", "weight"))
    text = (
        f"{'🚨' if severe else '⚠️'} <b>PERINGATAN: perlu diperiksa</b>\n"
        f"Anak: {esc(child_label(child))} (usia {res['age_m']:.1f} bln)\n\n"
        + "\n".join(f"• {esc(f)}" for f in flags)
        + "\n\nSegera konsultasikan ke tenaga kesehatan (posyandu/puskesmas).\n"
        "<i>Penapisan awal dari data simulasi, bukan diagnosis.</i>"
    )
    markup = kb(
        [("📈 Tren", f"t:v:{child['id']}"), ("🚩 Perlu Ditinjau", "v:menu")],
    )
    for uid in sorted(ALLOWED_USER_IDS):
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            log.exception("Gagal mengirim peringatan ke %s", uid)
    log.info("Peringatan otomatis dikirim untuk %s (pengukuran %s)", child_code(child["id"]), m["id"])


# ============================================================
# PENANDA "PERLU DITINJAU" (teknis + acuan WHO)
# ============================================================
def flags_for(conn, r):
    flags = []
    w = r["weight_kg"]
    if r["child_id"] is None:
        flags.append("Belum dikaitkan dengan anak (data tidak lengkap)")
    if not r["ts_from_device"]:
        flags.append("Waktu dari perangkat tidak tersedia, memakai waktu terima")
    if w < SIM_MIN_KG or w > SIM_MAX_KG:
        flags.append(f"Berat {w:.1f} kg di luar rentang input simulasi ({SIM_MIN_KG:.0f}-{SIM_MAX_KG:.0f} kg)")
    if r["child_id"] is not None:
        child = conn.execute("SELECT * FROM children WHERE id = ?", (r["child_id"],)).fetchone()
        flags.extend(who_flags(who_assess_row(child, r)))
        if r["height_cm"] is None:
            flags.append("Panjang/tinggi badan belum diisi (data tidak lengkap)")
        prev = conn.execute(
            """SELECT weight_kg, measured_at FROM measurements
               WHERE child_id = ? AND discarded = 0
                 AND (measured_at < ? OR (measured_at = ? AND id < ?))
               ORDER BY measured_at DESC, id DESC LIMIT 1""",
            (r["child_id"], r["measured_at"], r["measured_at"], r["id"]),
        ).fetchone()
        if prev:
            delta = w - prev["weight_kg"]
            t1, t0 = parse_iso(r["measured_at"]), parse_iso(prev["measured_at"])
            days = (t1 - t0).total_seconds() / 86400 if t1 and t0 else 999
            if abs(delta) >= JUMP_KG and days <= JUMP_DAYS:
                flags.append(
                    f"Perubahan berat {delta:+.1f} kg dalam {days:.1f} hari, mohon verifikasi ulang pengukuran"
                )
            elif delta < 0 and prev["weight_kg"] > 0 and abs(delta) / prev["weight_kg"] >= DROP_FRACTION:
                pct = abs(delta) / prev["weight_kg"] * 100
                flags.append(f"Berat turun {pct:.0f}% dari pengukuran sebelumnya, mohon verifikasi ulang")
    return flags


def review_items():
    """Daftar (row, flags, child_row) untuk pengukuran yang belum diperiksa dan punya penanda."""
    items = []
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT * FROM measurements WHERE reviewed = 0 AND discarded = 0
               ORDER BY received_at DESC, id DESC LIMIT 200"""
        ).fetchall()
        for r in rows:
            fl = flags_for(conn, r)
            if fl:
                child = (
                    conn.execute("SELECT * FROM children WHERE id = ?", (r["child_id"],)).fetchone()
                    if r["child_id"] is not None
                    else None
                )
                items.append((r, fl, child))
    return items


# ============================================================
# TEKS LAYAR
# ============================================================
def child_detail_text(child):
    sex = "Laki-laki" if child["sex"] == "L" else "Perempuan"
    bd = datetime.strptime(child["birth_date"], "%Y-%m-%d").strftime("%d-%m-%Y")
    return (
        "👶 <b>Data Anak</b>\n\n"
        f"ID: <code>{child_code(child['id'])}</code> (nomor {child['id']})\n"
        f"Nama/kode: {esc(child['name'])}\n"
        f"Tanggal lahir: {bd} (usia {age_text(child['birth_date'])})\n"
        f"Jenis kelamin: {sex}\n"
        f"Terdaftar: {fmt_dt(child['created_at'])}"
    )


def trend_text(cid):
    child = get_child(cid)
    head = f"📈 <b>Tren Pertumbuhan</b>\nAnak: {esc(child_label(child))}\n\n"
    rows = q_all(
        """SELECT * FROM measurements WHERE child_id = ? AND discarded = 0
           ORDER BY measured_at DESC, id DESC LIMIT 11""",
        (cid,),
    )
    if not rows:
        return head + "Belum ada pengukuran untuk anak ini. Gunakan menu ⚖️ Catat Pengukuran."
    rows = list(reversed(rows))  # kronologis
    start = 1 if len(rows) == 11 else 0
    lines = []
    for i in range(start, len(rows)):
        r = rows[i]
        line = f"• {fmt_short_date(r['measured_at'])} — <b>{r['weight_kg']:.1f} kg</b>"
        if i > 0:
            line += f" ({rows[i]['weight_kg'] - rows[i - 1]['weight_kg']:+.1f})"
        if r["height_cm"] is not None:
            line += f" · {r['height_cm']:.1f} cm" + (" (manual)" if r["height_manual"] else "")
        lines.append(line)

    out = head + "Riwayat (terlama ke terbaru):\n" + "\n".join(lines) + "\n\n<b>Ringkasan</b>\n"
    last = rows[-1]
    if len(rows) >= 2:
        d = last["weight_kg"] - rows[-2]["weight_kg"]
        word = "naik" if d > 0.05 else "turun" if d < -0.05 else "tetap"
        out += f"• Berat terakhir {last['weight_kg']:.1f} kg, {word}"
        out += f" {abs(d):.1f} kg dibanding pengukuran sebelumnya.\n" if word != "tetap" else " dibanding pengukuran sebelumnya.\n"
        shown = rows[start:]
        if len(shown) >= 2:
            out += (
                f"• Dalam {len(shown)} pengukuran terakhir: {shown[0]['weight_kg']:.1f} → "
                f"{shown[-1]['weight_kg']:.1f} kg ({shown[-1]['weight_kg'] - shown[0]['weight_kg']:+.1f} kg).\n"
            )
    else:
        out += f"• Baru satu pengukuran: {last['weight_kg']:.1f} kg.\n"
    h = q_one(
        """SELECT height_cm, height_manual FROM measurements
           WHERE child_id = ? AND discarded = 0 AND height_cm IS NOT NULL
           ORDER BY measured_at DESC, id DESC LIMIT 1""",
        (cid,),
    )
    if h:
        out += f"• Panjang/tinggi terakhir: {h['height_cm']:.1f} cm" + (" (diinput manual)" if h["height_manual"] else "") + "\n"
    else:
        out += "• Panjang/tinggi badan belum tersedia.\n"
    with_h = [x for x in rows if x["height_cm"] is not None]
    out += "\n" + who_text(child, with_h[-1] if with_h else last)
    out += (
        "\nℹ️ <i>Pemeriksaan WHO dihitung dari pengukuran terakhir saja (penapisan awal). "
        "Diagnosis stunting harus oleh tenaga kesehatan dengan pengukuran yang akurat.</i>"
    )
    return out


def next_occurrence(r, now):
    base = now.replace(hour=r["hour"], minute=r["minute"], second=0, microsecond=0)
    if r["repeat"] == "once":
        d = datetime.strptime(r["once_date"], "%Y-%m-%d").replace(
            hour=r["hour"], minute=r["minute"], tzinfo=LOCAL_TZ
        )
        return d if d >= now else None
    if r["repeat"] == "daily":
        return base if base > now else base + timedelta(days=1)
    cand = base + timedelta(days=(r["weekday"] - now.weekday()) % 7)
    return cand if cand > now else cand + timedelta(days=7)


def repeat_label(r):
    if r["repeat"] == "once":
        d = datetime.strptime(r["once_date"], "%Y-%m-%d").strftime("%d-%m-%Y")
        return f"sekali ({d})"
    if r["repeat"] == "daily":
        return "harian"
    return f"tiap {DAYS[r['weekday']]}"


def schedule_rows(chat_id):
    return q_all(
        """SELECT s.*, c.name AS child_name FROM schedules s
           LEFT JOIN children c ON c.id = s.child_id
           WHERE s.chat_id = ? ORDER BY s.hour, s.minute, s.id""",
        (chat_id,),
    )


def schedule_line(r):
    who = f"{child_code(r['child_id'])} · {r['child_name']}" if r["child_id"] else "Semua anak"
    return f"⏰ {r['hour']:02d}:{r['minute']:02d} {repeat_label(r)} — {esc(who)}"


def summary_text(chat_id):
    n_children = q_one("SELECT COUNT(*) AS n FROM children")["n"]
    n_meas = q_one("SELECT COUNT(*) AS n FROM measurements WHERE discarded = 0")["n"]
    n_unassoc = q_one(
        "SELECT COUNT(*) AS n FROM measurements WHERE discarded = 0 AND child_id IS NULL"
    )["n"]
    last = q_one(
        """SELECT m.*, c.name AS cname FROM measurements m
           LEFT JOIN children c ON c.id = m.child_id
           WHERE m.discarded = 0 ORDER BY m.measured_at DESC, m.id DESC LIMIT 1"""
    )
    now = local_now()
    upcoming = []
    for r in schedule_rows(chat_id):
        nxt = next_occurrence(r, now)
        if nxt:
            upcoming.append((nxt, r))
    upcoming.sort(key=lambda x: x[0])
    n_review = len(review_items())

    lines = ["📋 <b>Ringkasan</b>", ""]
    lines.append(f"👶 Anak terdaftar: <b>{n_children}</b>")
    lines.append(f"⚖️ Pengukuran tercatat: <b>{n_meas}</b> (belum dikaitkan ke anak: {n_unassoc})")
    if last:
        who = f"{child_code(last['child_id'])} · {last['cname']}" if last["child_id"] else "belum dikaitkan"
        lines.append(
            f"🕒 Pengukuran terbaru: {last['weight_kg']:.1f} kg, {fmt_dt(last['measured_at'])} ({esc(who)})"
        )
    else:
        lines.append("🕒 Pengukuran terbaru: belum ada")
    if upcoming:
        lines.append("🔔 Jadwal mendatang:")
        for nxt, r in upcoming[:3]:
            lines.append(f"   • {nxt.strftime('%d-%m-%Y %H:%M')} {TZ_LABEL} — {esc(r['child_name'] or 'Semua anak')}")
    else:
        lines.append("🔔 Jadwal mendatang: belum ada")
    lines.append(f"🚩 Hasil perlu ditinjau: <b>{n_review}</b>")
    return "\n".join(lines)


HELP_TEXT = (
    "❓ <b>Bantuan</b>\n\n"
    "👶 <b>Data Anak</b>: tambah anak (nama/kode, tanggal lahir, jenis kelamin) dan lihat data berdasarkan ID. "
    "Disarankan memakai nama samaran/kode.\n\n"
    "⚖️ <b>Catat Pengukuran</b>: pilih anak, bot mengambil data berat terbaru dari ESP32 (MQTT), "
    "dan tinggi dari ESP32 (MQTT), lalu Anda cukup konfirmasi ya/buang. Hasil langsung dibandingkan dengan acuan WHO "
    "(0-24 bulan): <b>perlu diperiksa</b> atau tidak. Tinggi bisa dikoreksi manual bila perlu.\n\n"
    "📈 <b>Tren Pertumbuhan</b>: riwayat berat, perubahan dari pengukuran sebelumnya, dan ringkasan sederhana.\n\n"
    "🔔 <b>Pengingat</b>: atur jadwal sekali/harian/mingguan. Pengingat hanya aktif selama program bot berjalan.\n\n"
    "🚩 <b>Perlu Ditinjau</b>: penanda teknis (data tidak lengkap, di luar rentang simulasi, perubahan tidak wajar) "
    "beserta alasannya. Tandai sudah diperiksa setelah diverifikasi petugas.\n\n"
    "📋 <b>Ringkasan</b> dan 📡 <b>Status Perangkat</b>: gambaran data dan kondisi ESP32/MQTT.\n\n"
    "⚠️ <b>Penting:</b> ini prototipe pemantauan dengan data simulasi (load cell + ultrasonik di Wokwi). Bukan alat ukur "
    "sungguhan dan bukan alat diagnosis medis. Hasil WHO hanya penapisan awal, bukan diagnosis stunting."
)

# ============================================================
# HELPER TELEGRAM
# ============================================================
CTX = ContextTypes.DEFAULT_TYPE
MENU_BTN = [("⬅️ Menu Utama", "m")]
CANCEL_BTN = [("❌ Batal", "m")]


def kb(*rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


async def show(update, text, markup=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )


def main_menu_markup():
    return kb(
        [("👶 Data Anak", "c:menu"), ("⚖️ Catat Pengukuran", "r:menu")],
        [("📈 Tren Pertumbuhan", "t:menu"), ("🔔 Pengingat", "s:menu")],
        [("🚩 Perlu Ditinjau", "v:menu"), ("📋 Ringkasan", "y:menu")],
        [("📡 Status Perangkat", "d:menu"), ("❓ Bantuan", "h:menu")],
    )


async def show_main_menu(update):
    await show(
        update,
        "👋 <b>BabyGrowth Monitor</b>\nPemantauan pertumbuhan bayi &amp; balita (prototipe, data simulasi).\n\nPilih menu:",
        main_menu_markup(),
    )


async def pick_child(update, title, prefix, extra_rows=()):
    kids = q_all("SELECT * FROM children ORDER BY id LIMIT 40")
    if not kids:
        await show(
            update,
            f"{title}\n\nBelum ada anak terdaftar. Tambahkan dulu.",
            kb([("➕ Tambah Anak", "c:add")], MENU_BTN),
        )
        return
    rows = [[(child_label(k), f"{prefix}{k['id']}")] for k in kids]
    rows.extend(extra_rows)
    rows.append(MENU_BTN)
    await show(update, f"{title}\n\nPilih anak:", kb(*rows))


# ============================================================
# AKSES: hanya Telegram user ID yang diizinkan (+ pairing otomatis)
# ============================================================
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
PAIRING_CODE = None
_pairing_fails = 0


def save_env(key, value):
    """Tulis/ubah satu baris KEY=VALUE di file .env (dibuat jika belum ada)."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", line):
            lines[i] = new
            break
    else:
        lines.append(new)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def ask_token():
    if not sys.stdin or not sys.stdin.isatty():
        sys.exit("TELEGRAM_BOT_TOKEN belum diisi di .env")
    print("\nToken bot belum diatur.")
    print("Buat bot di @BotFather (kirim /newbot), lalu tempel token-nya di sini.\n")
    while True:
        t = input("Token bot: ").strip().strip('"').strip("'")
        if TOKEN_RE.fullmatch(t):
            save_env("TELEGRAM_BOT_TOKEN", t)
            print("Token disimpan di file .env\n")
            return t
        print("Format token tidak valid. Contoh: 123456789:AAxxxxxxxx... Coba lagi.")


def new_pairing_code():
    global PAIRING_CODE, _pairing_fails
    PAIRING_CODE = f"{secrets.randbelow(10 ** 6):06d}"
    _pairing_fails = 0
    print("\n" + "=" * 56)
    print("  PAIRING: kirim kode ini ke bot Anda di Telegram")
    print(f"           KODE:  {PAIRING_CODE}")
    print("=" * 56 + "\n", flush=True)


async def auth_gate(update: Update, context: CTX):
    global PAIRING_CODE, _pairing_fails
    user = update.effective_user
    if user is not None and user.id in ALLOWED_USER_IDS:
        return
    msg = update.effective_message

    # Mode pairing: belum ada user terdaftar, kode tampil di terminal komputer
    if PAIRING_CODE and user is not None and msg is not None:
        text = (msg.text or "").strip()
        if text == PAIRING_CODE:
            ALLOWED_USER_IDS.add(user.id)
            save_env("ALLOWED_USER_IDS", ",".join(str(x) for x in sorted(ALLOWED_USER_IDS)))
            PAIRING_CODE = None
            log.info("User ID %s didaftarkan lewat pairing", user.id)
            await msg.reply_text("✅ Terdaftar! Akun Anda kini boleh memakai bot. Ketik /menu untuk mulai.")
        else:
            if re.fullmatch(r"\d{6}", text):
                _pairing_fails += 1
                if _pairing_fails >= 5:
                    new_pairing_code()  # kode diganti setelah 5 kali salah
            await msg.reply_text("Bot belum dipasangkan. Kirim kode pairing 6 digit yang tampil di terminal komputer.")
        raise ApplicationHandlerStop

    uid = user.id if user else "?"
    log.warning("Akses ditolak untuk user ID %s", uid)
    if msg:
        await msg.reply_text(
            f"⛔ Akses ditolak. User ID Anda: {uid}\nMinta admin menambahkannya ke ALLOWED_USER_IDS di .env."
        )
    elif update.callback_query:
        await update.callback_query.answer("Akses ditolak", show_alert=True)
    raise ApplicationHandlerStop


# ============================================================
# MENU: DATA ANAK
# ============================================================
async def route_children(update, context, a):
    sub = a[0] if a else "menu"
    if sub == "menu":
        kids = q_all("SELECT * FROM children ORDER BY id LIMIT 40")
        rows = [[(child_label(k), f"c:v:{k['id']}")] for k in kids]
        rows.append([("➕ Tambah Anak", "c:add"), ("🔎 Cari berdasarkan ID", "c:find")])
        rows.append(MENU_BTN)
        text = "👶 <b>Data Anak</b>\n\n" + (
            "Pilih anak untuk melihat data." if kids else "Belum ada anak terdaftar."
        )
        await show(update, text, kb(*rows))
    elif sub == "add":
        context.user_data["await"] = {"type": "child_name"}
        context.user_data["draft_child"] = {}
        await show(
            update,
            "➕ <b>Tambah Anak</b>\n\nKetik <b>nama atau kode anak</b> (maks 40 karakter). "
            "Disarankan memakai kode/nama samaran, mis. <code>Bayi A</code>.",
            kb(CANCEL_BTN),
        )
    elif sub == "find":
        context.user_data["await"] = {"type": "child_find"}
        await show(
            update,
            "🔎 Ketik ID anak, contoh <code>A001</code> atau <code>1</code>.",
            kb(CANCEL_BTN),
        )
    elif sub == "v":
        cid = to_int(a[1]) if len(a) > 1 else None
        child = get_child(cid) if cid else None
        if not child:
            await show(update, "Data anak tidak ditemukan.", kb([("⬅️ Data Anak", "c:menu")]))
            return
        await show(
            update,
            child_detail_text(child),
            kb(
                [("⚖️ Catat Pengukuran", f"r:c:{cid}"), ("📈 Tren", f"t:v:{cid}")],
                [("⬅️ Daftar Anak", "c:menu")],
            ),
        )
    elif sub == "sex":
        draft = context.user_data.get("draft_child") or {}
        if "name" not in draft or "birth" not in draft or len(a) < 2 or a[1] not in ("L", "P"):
            await show(update, "Sesi tambah anak sudah berakhir. Ulangi dari menu.", kb([("⬅️ Data Anak", "c:menu")]))
            return
        cid, _ = execute(
            "INSERT INTO children (name, birth_date, sex, created_at) VALUES (?, ?, ?, ?)",
            (draft["name"], draft["birth"], a[1], to_iso(utc_now())),
        )
        context.user_data.pop("draft_child", None)
        child = get_child(cid)
        await show(
            update,
            "✅ Anak berhasil ditambahkan.\n\n" + child_detail_text(child),
            kb(
                [("⚖️ Catat Pengukuran", f"r:c:{cid}"), ("📈 Tren", f"t:v:{cid}")],
                [("⬅️ Daftar Anak", "c:menu")],
            ),
        )


# ============================================================
# MENU: CATAT PENGUKURAN
# ============================================================
PENDING_SHOW = 6  # jumlah data belum-dikaitkan yang ditampilkan sekaligus


async def render_record(update, cid):
    child = get_child(cid)
    if not child:
        await show(update, "Data anak tidak ditemukan.", kb(MENU_BTN))
        return
    pending = q_all(
        """SELECT * FROM measurements WHERE child_id IS NULL AND discarded = 0
           ORDER BY received_at DESC, id DESC LIMIT ?""",
        (PENDING_SHOW,),
    )
    n_pending = q_one(
        "SELECT COUNT(*) AS n FROM measurements WHERE child_id IS NULL AND discarded = 0"
    )["n"]
    refresh = [("🔄 Cek ulang", f"r:c:{cid}"), ("⬅️ Ganti anak", "r:menu")]
    head = f"⚖️ <b>Catat Pengukuran</b>\nAnak: {esc(child_label(child))}\n\n"
    if not pending:
        await show(
            update,
            head + "Belum ada data baru dari ESP32.\n"
            "Atur slider berat dan jarak (tinggi) di Wokwi, tekan tombol hijau, lalu tekan <b>Cek ulang</b>.",
            kb(refresh, MENU_BTN),
        )
        return
    lines = [head + f"Data dari perangkat yang belum dikaitkan ({n_pending}), terbaru di atas:\n"]
    rows = []
    for i, p in enumerate(pending, 1):
        rec = parse_iso(p["received_at"])
        age_s = (utc_now() - rec).total_seconds() if rec else 0
        tinggi = f"{p['height_cm']:.1f} cm" if p["height_cm"] is not None else "tinggi -"
        lines.append(
            f"<b>{i}.</b> {p['weight_kg']:.1f} kg · {tinggi} · {fmt_short_date(p['measured_at'])} ({ago(age_s)})"
        )
        rows.append(
            [(f"✅ Kaitkan #{i}", f"r:ok:{cid}:{p['id']}"), (f"🗑️ Buang #{i}", f"r:drop:{cid}:{p['id']}")]
        )
    if n_pending > len(pending):
        lines.append(f"<i>…dan {n_pending - len(pending)} data lebih lama (buang yang tidak perlu).</i>")
    lines.append(f"\nPilih data yang mau dikaitkan ke <b>{esc(child['name'])}</b>. Data iseng/tes bisa dibuang.")
    rows.append([(f"🗑️ Buang semua ({n_pending})", f"r:dall:{cid}")])
    rows.append(refresh)
    rows.append(MENU_BTN)
    await show(update, "\n".join(lines), kb(*rows))


async def route_record(update, context, a):
    sub = a[0] if a else "menu"
    if sub == "menu":
        await pick_child(update, "⚖️ <b>Catat Pengukuran</b>", "r:c:")
        return
    cid = to_int(a[1]) if len(a) > 1 else None
    if sub == "c":
        await render_record(update, cid)
    elif sub == "drop":
        mid = to_int(a[2]) if len(a) > 2 else None
        execute(
            "UPDATE measurements SET discarded = 1 WHERE id = ? AND child_id IS NULL",
            (mid,),
        )
        await render_record(update, cid)
    elif sub == "dall":
        n = q_one("SELECT COUNT(*) AS n FROM measurements WHERE child_id IS NULL AND discarded = 0")["n"]
        await show(
            update,
            f"Buang <b>semua {n} data</b> yang belum dikaitkan? Tindakan ini tidak bisa dibatalkan.",
            kb([("🗑️ Ya, buang semua", f"r:dallok:{cid}"), ("↩️ Tidak", f"r:c:{cid}")]),
        )
    elif sub == "dallok":
        execute("UPDATE measurements SET discarded = 1 WHERE child_id IS NULL AND discarded = 0")
        await render_record(update, cid)
    elif sub == "ok":
        mid = to_int(a[2]) if len(a) > 2 else None
        child = get_child(cid) if cid else None
        if not child or not mid:
            await show(update, "Data tidak valid.", kb(MENU_BTN))
            return
        _, rc = execute(
            "UPDATE measurements SET child_id = ? WHERE id = ? AND child_id IS NULL AND discarded = 0",
            (cid, mid),
        )
        if rc == 0:
            await show(
                update,
                "Data ini sudah diproses atau tidak tersedia lagi. Silakan cek ulang.",
                kb([("🔄 Cek ulang", f"r:c:{cid}")], MENU_BTN),
            )
            return
        m = q_one("SELECT * FROM measurements WHERE id = ?", (mid,))
        if m["height_cm"] is not None:
            await show(
                update,
                f"✅ Pengukuran dikaitkan ke {esc(child_label(child))}.\n"
                f"Berat <b>{m['weight_kg']:.1f} kg</b> · Tinggi <b>{m['height_cm']:.1f} cm</b> (dari perangkat)\n\n"
                + who_text(child, m),
                kb(
                    [("✏️ Koreksi tinggi", f"r:fix:{cid}:{mid}"), ("📈 Tren", f"t:v:{cid}")],
                    [("🚩 Perlu Ditinjau", "v:menu")],
                    MENU_BTN,
                ),
            )
            await send_who_alert(update.get_bot(), child, m)
            return
        context.user_data["await"] = {"type": "height", "mid": mid, "cid": cid}
        await show(
            update,
            f"✅ Pengukuran <b>{m['weight_kg']:.1f} kg</b> dikaitkan ke {esc(child_label(child))}.\n\n"
            f"Ketik <b>panjang/tinggi badan (cm)</b> secara manual ({HEIGHT_MIN_CM:.0f}-{HEIGHT_MAX_CM:.0f}), "
            "contoh <code>68.5</code>, atau lewati.",
            kb([("⏭️ Lewati", f"r:skip:{cid}:{mid}")]),
        )
        await send_who_alert(update.get_bot(), child, m)
    elif sub == "fix":
        mid = to_int(a[2]) if len(a) > 2 else None
        m = q_one("SELECT * FROM measurements WHERE id = ? AND child_id = ?", (mid, cid))
        if not m:
            await show(update, "Data tidak ditemukan.", kb(MENU_BTN))
            return
        context.user_data["await"] = {"type": "height", "mid": mid, "cid": cid}
        await show(
            update,
            f"Ketik <b>tinggi/panjang badan yang benar (cm)</b> ({HEIGHT_MIN_CM:.0f}-{HEIGHT_MAX_CM:.0f}), "
            "contoh <code>68.5</code>. Nilai akan ditandai <b>input manual</b>.",
            kb([("❌ Batal", f"t:v:{cid}")]),
        )
    elif sub == "skip":
        await show(
            update,
            "✅ Pengukuran tersimpan <b>tanpa</b> panjang/tinggi badan.\n"
            "Data ditandai tidak lengkap di menu 🚩 Perlu Ditinjau sampai tinggi diisi atau ditandai diperiksa.",
            kb([("📈 Lihat Tren", f"t:v:{cid}")], MENU_BTN),
        )


async def finish_height(update, cid, mid, height):
    execute(
        "UPDATE measurements SET height_cm = ?, height_manual = 1 WHERE id = ?",
        (height, mid),
    )
    m = q_one("SELECT * FROM measurements WHERE id = ?", (mid,))
    child = get_child(cid)
    await show(
        update,
        f"✅ Panjang/tinggi <b>{height:.1f} cm</b> tersimpan (ditandai <b>input manual</b>).\n\n"
        + (who_text(child, m) if m and child else ""),
        kb([("📈 Lihat Tren", f"t:v:{cid}")],
           [("🚩 Perlu Ditinjau", "v:menu")], MENU_BTN),
    )
    if m and child:
        res = who_assess_row(child, m)
        if res and res["height"] and res["height"]["status"] != "normal":
            await send_who_alert(update.get_bot(), child, m, res)


# ============================================================
# MENU: TREN
# ============================================================
async def route_trend(update, context, a):
    sub = a[0] if a else "menu"
    if sub == "menu":
        await pick_child(update, "📈 <b>Tren Pertumbuhan</b>", "t:v:")
    elif sub == "v":
        cid = to_int(a[1]) if len(a) > 1 else None
        if not cid or not get_child(cid):
            await show(update, "Data anak tidak ditemukan.", kb(MENU_BTN))
            return
        await show(
            update,
            trend_text(cid),
            kb([("⚖️ Catat Pengukuran", f"r:c:{cid}")], [("⬅️ Pilih anak lain", "t:menu")], MENU_BTN),
        )


# ============================================================
# MENU: PENGINGAT
# ============================================================
async def route_sched(update, context, a):
    chat_id = update.effective_chat.id
    sub = a[0] if a else "menu"

    if sub == "menu":
        rows = schedule_rows(chat_id)
        buttons = [[(f"{r['hour']:02d}:{r['minute']:02d} {repeat_label(r)}", f"s:v:{r['id']}")] for r in rows]
        buttons.append([("➕ Tambah Jadwal", "s:add")])
        buttons.append(MENU_BTN)
        text = "🔔 <b>Pengingat Pengukuran</b>\n\n"
        text += "\n".join(schedule_line(r) for r in rows) if rows else "Belum ada jadwal."
        text += (
            f"\n\n<i>Zona waktu: {TZ_LABEL}. Pengingat hanya aktif selama program bot (bot.py) berjalan; "
            "jadwal yang terlewat saat bot mati tidak dikirim.</i>"
        )
        await show(update, text, kb(*buttons))

    elif sub == "add":
        kids = q_all("SELECT * FROM children ORDER BY id LIMIT 40")
        rows = [[("👥 Semua anak (umum)", "s:ac:0")]] + [[(child_label(k), f"s:ac:{k['id']}")] for k in kids]
        rows.append([("⬅️ Kembali", "s:menu")])
        await show(update, "➕ <b>Tambah Jadwal</b>\n\nPengingat untuk siapa?", kb(*rows))

    elif sub == "ac":
        cid = to_int(a[1]) if len(a) > 1 else 0
        if cid and not get_child(cid):
            await show(update, "Data anak tidak ditemukan.", kb([("⬅️ Kembali", "s:menu")]))
            return
        context.user_data["draft_sched"] = {"child_id": cid or None}
        context.user_data["await"] = {"type": "sched_time"}
        await show(
            update,
            f"Ketik <b>jam pengingat</b> format <code>HH:MM</code> (24 jam, {TZ_LABEL}). Contoh: <code>08:00</code>",
            kb([("❌ Batal", "s:menu")]),
        )

    elif sub == "r":
        draft = context.user_data.get("draft_sched")
        rep = a[1] if len(a) > 1 else ""
        if not draft or "hour" not in draft or rep not in ("once", "daily", "weekly"):
            await show(update, "Sesi sudah berakhir. Ulangi dari menu.", kb([("⬅️ Pengingat", "s:menu")]))
            return
        if rep == "weekly":
            await show(
                update,
                "Pilih hari:",
                kb(
                    [(d, f"s:w:{i}") for i, d in enumerate(DAYS[:4])],
                    [(d, f"s:w:{i + 4}") for i, d in enumerate(DAYS[4:])],
                    [("❌ Batal", "s:menu")],
                ),
            )
            return
        await save_schedule(update, context, rep, None)

    elif sub == "w":
        wd = to_int(a[1]) if len(a) > 1 else None
        if wd is None or not 0 <= wd <= 6:
            return
        await save_schedule(update, context, "weekly", wd)

    elif sub in ("v", "e", "d", "dy"):
        sid = to_int(a[1]) if len(a) > 1 else None
        r = q_one(
            """SELECT s.*, c.name AS child_name FROM schedules s
               LEFT JOIN children c ON c.id = s.child_id
               WHERE s.id = ? AND s.chat_id = ?""",
            (sid, chat_id),
        )
        if not r:
            await show(update, "Jadwal tidak ditemukan.", kb([("⬅️ Pengingat", "s:menu")]))
            return
        if sub == "v":
            nxt = next_occurrence(r, local_now())
            nxt_txt = nxt.strftime("%d-%m-%Y %H:%M") + f" {TZ_LABEL}" if nxt else "-"
            await show(
                update,
                f"🔔 <b>Jadwal #{r['id']}</b>\n\n{schedule_line(r)}\nPengingat berikutnya: {nxt_txt}",
                kb(
                    [("✏️ Ubah jam", f"s:e:{sid}"), ("🗑️ Batalkan", f"s:d:{sid}")],
                    [("⬅️ Pengingat", "s:menu")],
                ),
            )
        elif sub == "e":
            context.user_data["await"] = {"type": "sched_edit", "sid": sid}
            await show(
                update,
                f"Ketik <b>jam baru</b> format <code>HH:MM</code> ({TZ_LABEL}) untuk jadwal ini.",
                kb([("❌ Batal", f"s:v:{sid}")]),
            )
        elif sub == "d":
            await show(
                update,
                f"Batalkan jadwal ini?\n\n{schedule_line(r)}",
                kb([("🗑️ Ya, hapus", f"s:dy:{sid}"), ("↩️ Tidak", f"s:v:{sid}")]),
            )
        else:
            execute("DELETE FROM schedules WHERE id = ? AND chat_id = ?", (sid, chat_id))
            await show(update, "✅ Jadwal dibatalkan.", kb([("⬅️ Pengingat", "s:menu")]))


def once_date_for(hour, minute):
    now = local_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d")


async def save_schedule(update, context, repeat, weekday):
    draft = context.user_data.get("draft_sched")
    if not draft or "hour" not in draft:
        await show(update, "Sesi sudah berakhir. Ulangi dari menu.", kb([("⬅️ Pengingat", "s:menu")]))
        return
    once_date = once_date_for(draft["hour"], draft["minute"]) if repeat == "once" else None
    sid, _ = execute(
        """INSERT INTO schedules (chat_id, child_id, hour, minute, repeat, weekday, once_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (update.effective_chat.id, draft["child_id"], draft["hour"], draft["minute"],
         repeat, weekday, once_date, to_iso(utc_now())),
    )
    context.user_data.pop("draft_sched", None)
    r = q_one(
        "SELECT s.*, c.name AS child_name FROM schedules s LEFT JOIN children c ON c.id = s.child_id WHERE s.id = ?",
        (sid,),
    )
    await show(
        update,
        f"✅ Jadwal tersimpan:\n{schedule_line(r)}\n\n<i>Pengingat aktif selama bot.py berjalan.</i>",
        kb([("🔔 Daftar Jadwal", "s:menu")], MENU_BTN),
    )


async def reminder_job(context: CTX):
    """Dipanggil tiap 30 detik. Setiap jadwal dikirim paling banyak sekali per hari."""
    now = local_now()
    today = now.strftime("%Y-%m-%d")
    for r in q_all(
        "SELECT s.*, c.name AS child_name FROM schedules s LEFT JOIN children c ON c.id = s.child_id"
    ):
        due = now.replace(hour=r["hour"], minute=r["minute"], second=0, microsecond=0)
        if not (due <= now < due + timedelta(minutes=REMINDER_WINDOW_MIN)):
            continue
        if r["repeat"] == "weekly" and now.weekday() != r["weekday"]:
            continue
        if r["repeat"] == "once" and r["once_date"] != today:
            continue
        # "Klaim" jadwal hari ini secara atomik agar tidak terkirim ganda
        _, rc = execute(
            """UPDATE schedules SET last_sent_date = ?
               WHERE id = ? AND (last_sent_date IS NULL OR last_sent_date <> ?)""",
            (today, r["id"], today),
        )
        if rc != 1:
            continue
        who = f"{child_code(r['child_id'])} · {r['child_name']}" if r["child_id"] else "semua anak"
        try:
            await context.bot.send_message(
                chat_id=r["chat_id"],
                text=(
                    "🔔 <b>Pengingat pengukuran</b>\n"
                    f"Waktunya mengukur: {esc(who)}\n\n"
                    "Buka /menu → ⚖️ Catat Pengukuran setelah data berat dikirim dari ESP32."
                ),
                parse_mode=ParseMode.HTML,
            )
            log.info("Pengingat #%s terkirim", r["id"])
        except Exception:
            log.exception("Gagal mengirim pengingat #%s", r["id"])
        if r["repeat"] == "once":
            execute("DELETE FROM schedules WHERE id = ?", (r["id"],))


# ============================================================
# MENU: HASIL PERLU DITINJAU
# ============================================================
async def route_review(update, context, a):
    sub = a[0] if a else "menu"
    if sub == "ok":
        mid = to_int(a[1]) if len(a) > 1 else None
        execute(
            "UPDATE measurements SET reviewed = 1, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (update.effective_user.id, to_iso(utc_now()), mid),
        )
    elif sub == "x":
        mid = to_int(a[1]) if len(a) > 1 else None
        execute("UPDATE measurements SET discarded = 1 WHERE id = ? AND child_id IS NULL", (mid,))
    elif sub == "h":
        mid = to_int(a[1]) if len(a) > 1 else None
        m = q_one("SELECT * FROM measurements WHERE id = ? AND child_id IS NOT NULL", (mid,))
        if m:
            context.user_data["await"] = {"type": "height", "mid": mid, "cid": m["child_id"]}
            await show(
                update,
                f"Ketik <b>panjang/tinggi badan (cm)</b> untuk pengukuran #{mid} "
                f"({HEIGHT_MIN_CM:.0f}-{HEIGHT_MAX_CM:.0f}), contoh <code>68.5</code>.",
                kb([("❌ Batal", "v:menu")]),
            )
            return

    items = review_items()
    if not items:
        await show(
            update,
            "🚩 <b>Hasil Perlu Ditinjau</b>\n\n✅ Tidak ada hasil yang perlu ditinjau.",
            kb(MENU_BTN),
        )
        return
    shown = items[:8]
    lines = [f"🚩 <b>Hasil Perlu Ditinjau</b> ({len(items)})", ""]
    rows = []
    for r, fl, child in shown:
        who = child_label(child) if child else "belum dikaitkan"
        lines.append(f"<b>#{r['id']}</b> · {esc(who)} · {r['weight_kg']:.1f} kg · {fmt_short_date(r['measured_at'])}")
        lines.extend(f"   • {esc(x)}" for x in fl)
        lines.append("")
        btns = [(f"✅ Diperiksa #{r['id']}", f"v:ok:{r['id']}")]
        if r["child_id"] is None:
            btns.append((f"🗑️ Buang #{r['id']}", f"v:x:{r['id']}"))
        elif r["height_cm"] is None:
            btns.append((f"📏 Isi tinggi #{r['id']}", f"v:h:{r['id']}"))
        rows.append(btns)
    if len(items) > len(shown):
        lines.append(f"<i>…dan {len(items) - len(shown)} lainnya.</i>")
    lines.append(
        "ℹ️ <i>Penanda ini hanya pemeriksaan teknis/kelengkapan data, bukan diagnosis. "
        "Tekan ✅ setelah petugas memeriksa.</i>"
    )
    rows.append(MENU_BTN)
    await show(update, "\n".join(lines), kb(*rows))


# ============================================================
# MENU: RINGKASAN, STATUS, BANTUAN
# ============================================================
async def route_summary(update, context, a):
    await show(update, summary_text(update.effective_chat.id), kb([("🔄 Segarkan", "y:menu")], MENU_BTN))


async def route_device(update, context, a):
    await show(update, device_status_text(), kb([("🔄 Segarkan", "d:menu")], MENU_BTN))


async def route_help(update, context, a):
    await show(update, HELP_TEXT, kb(MENU_BTN))


async def route_main(update, context, a):
    context.user_data.pop("draft_child", None)
    context.user_data.pop("draft_sched", None)
    await show_main_menu(update)


ROUTES = {
    "m": route_main,
    "c": route_children,
    "r": route_record,
    "t": route_trend,
    "s": route_sched,
    "v": route_review,
    "y": route_summary,
    "d": route_device,
    "h": route_help,
}


async def on_callback(update: Update, context: CTX):
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest:
        return  # tombol lama/kedaluwarsa (mis. ditekan saat bot mati), abaikan
    context.user_data.pop("await", None)  # tombol baru membatalkan input teks yang tertunda
    parts = (q.data or "").split(":")
    handler = ROUTES.get(parts[0])
    if handler:
        await handler(update, context, parts[1:])


# ============================================================
# INPUT TEKS (alur bertahap)
# ============================================================
def parse_date(text):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_hhmm(text):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


async def on_text(update: Update, context: CTX):
    msg = update.message
    text = (msg.text or "").strip()
    st = context.user_data.get("await")
    if not st:
        await msg.reply_text("Gunakan /menu untuk membuka menu utama.")
        return
    t = st["type"]

    if t == "child_name":
        if not re.fullmatch(r"[\w .,'()\-]{1,40}", text):
            await msg.reply_text("Nama/kode hanya boleh huruf, angka, spasi, titik, koma, tanda hubung (maks 40). Coba lagi:")
            return
        context.user_data["draft_child"] = {"name": text}
        context.user_data["await"] = {"type": "child_dob"}
        await msg.reply_text(
            "Ketik <b>tanggal lahir</b> format <code>YYYY-MM-DD</code> (contoh <code>2026-03-15</code>) atau <code>DD-MM-YYYY</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb(CANCEL_BTN),
        )

    elif t == "child_dob":
        d = parse_date(text)
        today = local_now().date()
        if not d:
            await msg.reply_text("Format tanggal tidak dikenali. Contoh: 2026-03-15")
            return
        if d > today:
            await msg.reply_text("Tanggal lahir tidak boleh di masa depan. Coba lagi:")
            return
        if (today - d).days > MAX_AGE_YEARS * 366:
            await msg.reply_text(f"Prototipe ini untuk bayi/balita (maks {MAX_AGE_YEARS} tahun). Cek kembali tanggalnya:")
            return
        context.user_data["draft_child"]["birth"] = d.strftime("%Y-%m-%d")
        context.user_data.pop("await", None)
        await msg.reply_text(
            "Pilih <b>jenis kelamin</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([("👦 Laki-laki", "c:sex:L"), ("👧 Perempuan", "c:sex:P")], CANCEL_BTN),
        )

    elif t == "child_find":
        m = re.fullmatch(r"[Aa]?0*(\d{1,6})", text)
        child = get_child(int(m.group(1))) if m else None
        context.user_data.pop("await", None)
        if not child:
            await msg.reply_text("ID tidak ditemukan.", reply_markup=kb([("⬅️ Data Anak", "c:menu")]))
            return
        await msg.reply_text(
            child_detail_text(child),
            parse_mode=ParseMode.HTML,
            reply_markup=kb(
                [("⚖️ Catat Pengukuran", f"r:c:{child['id']}"), ("📈 Tren", f"t:v:{child['id']}")],
                [("⬅️ Daftar Anak", "c:menu")],
            ),
        )

    elif t == "height":
        try:
            h = float(text.replace(",", "."))
        except ValueError:
            await msg.reply_text("Masukkan angka dalam cm, contoh 68.5")
            return
        if not math.isfinite(h) or not HEIGHT_MIN_CM <= h <= HEIGHT_MAX_CM:
            await msg.reply_text(f"Nilai di luar rentang input ({HEIGHT_MIN_CM:.0f}-{HEIGHT_MAX_CM:.0f} cm). Coba lagi:")
            return
        context.user_data.pop("await", None)
        await finish_height(update, st["cid"], st["mid"], h)

    elif t == "sched_time":
        hm = parse_hhmm(text)
        if not hm:
            await msg.reply_text("Format jam tidak valid. Contoh: 08:00 atau 14:30")
            return
        context.user_data["draft_sched"].update({"hour": hm[0], "minute": hm[1]})
        context.user_data.pop("await", None)
        await msg.reply_text(
            f"Jam {hm[0]:02d}:{hm[1]:02d}. Pengulangan?",
            reply_markup=kb(
                [("Sekali", "s:r:once"), ("Harian", "s:r:daily"), ("Mingguan", "s:r:weekly")],
                [("❌ Batal", "s:menu")],
            ),
        )

    elif t == "sched_edit":
        hm = parse_hhmm(text)
        if not hm:
            await msg.reply_text("Format jam tidak valid. Contoh: 08:00 atau 14:30")
            return
        sid = st["sid"]
        r = q_one("SELECT * FROM schedules WHERE id = ? AND chat_id = ?", (sid, update.effective_chat.id))
        context.user_data.pop("await", None)
        if not r:
            await msg.reply_text("Jadwal tidak ditemukan.", reply_markup=kb([("⬅️ Pengingat", "s:menu")]))
            return
        once_date = once_date_for(hm[0], hm[1]) if r["repeat"] == "once" else None
        execute(
            "UPDATE schedules SET hour = ?, minute = ?, once_date = ?, last_sent_date = NULL WHERE id = ?",
            (hm[0], hm[1], once_date, sid),
        )
        await msg.reply_text(
            f"✅ Jam jadwal #{sid} diubah menjadi {hm[0]:02d}:{hm[1]:02d}.",
            reply_markup=kb([("🔔 Daftar Jadwal", "s:menu")], MENU_BTN),
        )


# ============================================================
# PERINTAH & ERROR HANDLER
# ============================================================
async def cmd_menu(update: Update, context: CTX):
    context.user_data.clear()
    await show_main_menu(update)


async def on_error(update, context: CTX):
    log.error("Error saat memproses update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Terjadi kesalahan. Coba lagi lewat /menu.")
        except Exception:
            pass


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [BotCommand("menu", "Buka menu utama"), BotCommand("start", "Mulai")]
    )


def main():
    global BOT_TOKEN
    if not TOKEN_RE.fullmatch(BOT_TOKEN):
        BOT_TOKEN = ask_token()
    if not ALLOWED_USER_IDS:
        new_pairing_code()

    init_db()
    log.info("Metode z-score WHO: %s", who_standards.method_summary())
    mqtt_client = start_mqtt()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, auth_gate), group=-1)  # gerbang akses
    app.add_handler(CommandHandler(["start", "menu", "help"], cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(reminder_job, interval=30, first=5)

    log.info("Bot berjalan. Topic: %s", MQTT_TOPIC_BASE)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
