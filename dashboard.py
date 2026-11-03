import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template

import paho.mqtt.client as mqtt
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import who_standards as who

# ============================================================
# KONFIGURASI HALAMAN & MQTT (Sesuai main.py)
# ============================================================
st.set_page_config(
    page_title="BabyGrowth - Pemantauan Pertumbuhan Bayi",
    layout="wide",
    initial_sidebar_state="expanded",
)

MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
DEVICE_ID = "device01"
TOPIC_BASE = f"babygrowth/babygrowth123/{DEVICE_ID}"
TOPIC_MEASUREMENT = f"{TOPIC_BASE}/measurement"
TOPIC_STATUS = f"{TOPIC_BASE}/status"

# Database SQLite milik bot Telegram (bot.py). Dashboard membaca data bayi & pengukuran dari sini,
# jadi harus menunjuk ke file yang sama dengan yang dipakai bot (DB_PATH di .env bot).
# Cara mengatur: taruh babygrowth.db di folder yang sama dengan dashboard.py, atau isi variabel
# lingkungan BABYGROWTH_DB dengan path lengkapnya.
DB_PATH = Path(os.getenv("BABYGROWTH_DB") or Path(__file__).with_name("babygrowth.db"))
OFFLINE_SECONDS = 45          # alat dianggap mati bila tidak ada heartbeat selama ini (sama dengan bot)
NAMA_RE = re.compile(r"[\w .,'()\-]{1,40}")   # aturan nama yang sama dengan bot
MAX_UMUR_HARI = 6 * 366       # batas umur input yang sama dengan bot (6 tahun)

BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


# ============================================================
# FUNGSI BANTU
# ============================================================
def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wib_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def render_html(s):
    """Tampilkan HTML. Spasi di awal baris dibuang supaya tidak dianggap blok kode oleh markdown."""
    cleaned = "\n".join(line.strip() for line in s.strip().splitlines() if line.strip())
    st.markdown(cleaned, unsafe_allow_html=True)


def nz(x):
    return 0.0 if pd.isna(x) else float(x)


def fnum(x, desimal, satuan=""):
    return "-" if pd.isna(x) else f"{x:.{desimal}f}{satuan}"


def fmt_waktu(ts):
    if pd.isna(ts):
        return "-"
    return f"{ts.day} {BULAN[ts.month - 1]} {ts.year}, {ts.strftime('%H:%M')}"


def durasi(detik):
    try:
        detik = int(detik)
    except (TypeError, ValueError):
        return "-"
    jam, sisa = divmod(detik, 3600)
    menit, dtk = divmod(sisa, 60)
    if jam:
        return f"{jam} jam {menit} menit"
    if menit:
        return f"{menit} menit"
    return f"{dtk} detik"


def teks_selisih(diff, satuan, desimal, ada_sebelumnya):
    if not ada_sebelumnya or pd.isna(diff):
        return "Belum ada pengukuran sebelumnya", "bi-dash"
    if abs(diff) < 0.5 * 10 ** (-desimal):
        return "Sama seperti pengukuran sebelumnya", "bi-dash"
    if diff > 0:
        return f"Naik {diff:.{desimal}f} {satuan} dari pengukuran sebelumnya", "bi-arrow-up-right"
    return f"Turun {abs(diff):.{desimal}f} {satuan} dari pengukuran sebelumnya", "bi-arrow-down-right"


# ============================================================
# PROFIL BAYI & PENILAIAN STATUS GIZI
# ============================================================
LEVEL_CLASS = {"ok": "ok", "warn": "warn", "bad": "bad", "invalid": "warn", "na": "na"}


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def kode_anak(cid):
    """Kode bayi sama seperti di bot Telegram: id 1 -> A001."""
    return f"A{int(cid):03d}"


def load_children():
    """Semua bayi yang terdaftar di bot: {kode: {id, nama, sex, lahir}}."""
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT id, name, birth_date, sex FROM children ORDER BY id").fetchall()
    return {kode_anak(r["id"]): {"id": r["id"], "nama": r["name"], "sex": r["sex"], "lahir": r["birth_date"]}
            for r in rows}


KOLOM_UKUR = ["measurement_id", "child_id", "weight_kg", "height_cm", "height_manual", "source", "timestamp"]


def load_measurements():
    """Pengukuran yang sudah dikaitkan ke bayi (DataFrame) + info data yang belum dikaitkan."""
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """SELECT measurement_id, child_id, weight_kg, height_cm, height_manual, source,
                      measured_at AS timestamp
               FROM measurements WHERE discarded = 0 AND child_id IS NOT NULL
               ORDER BY measured_at, id"""
        ).fetchall()
        n_belum = conn.execute(
            "SELECT COUNT(*) AS n FROM measurements WHERE discarded = 0 AND child_id IS NULL"
        ).fetchone()["n"]
        last = conn.execute(
            """SELECT weight_kg, height_cm, measured_at FROM measurements
               WHERE discarded = 0 AND child_id IS NULL ORDER BY received_at DESC, id DESC LIMIT 1"""
        ).fetchone()
    df = pd.DataFrame([tuple(r) for r in rows], columns=KOLOM_UKUR)
    df["child_id"] = df["child_id"].map(kode_anak)
    df["weight_kg"] = pd.to_numeric(df["weight_kg"], errors="coerce")
    df["height_cm"] = pd.to_numeric(df["height_cm"], errors="coerce")
    df["height_manual"] = pd.to_numeric(df["height_manual"], errors="coerce").fillna(0).astype(int)
    df["ts_wib"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True) + pd.Timedelta(hours=7)
    belum = None
    if last:
        belum = {"w": last["weight_kg"], "h": last["height_cm"],
                 "ts": pd.to_datetime(last["measured_at"], errors="coerce", utc=True) + pd.Timedelta(hours=7)}
    return df, n_belum, belum


def simpan_anak(nama, sex, lahir_iso, cid=None):
    """Tambah (cid=None) atau ubah bayi di database bot. Mengembalikan kode bayi, atau None bila gagal."""
    try:
        with closing(db_connect()) as conn:
            if cid is None:
                cur = conn.execute(
                    "INSERT INTO children (name, birth_date, sex, created_at) VALUES (?, ?, ?, ?)",
                    (nama, lahir_iso, sex, utc_now_str()),
                )
                cid = cur.lastrowid
            else:
                conn.execute("UPDATE children SET name = ?, birth_date = ?, sex = ? WHERE id = ?",
                             (nama, lahir_iso, sex, cid))
            conn.commit()
        return kode_anak(cid)
    except sqlite3.Error:
        return None


def label_bayi(kode, profiles):
    nama = (profiles.get(kode) or {}).get("nama")
    return f"{kode} · {nama}" if nama else kode


def baca_profil(prof):
    """Kembalikan (jenis_kelamin, tanggal_lahir) bila profil lengkap, selain itu None."""
    try:
        lahir = date.fromisoformat(prof["lahir"])
    except (KeyError, TypeError, ValueError):
        return None
    if prof.get("sex") not in ("L", "P"):
        return None
    return prof["sex"], lahir


def nilai_baris(r, prof):
    """Penilaian WHO untuk satu baris pengukuran. None bila profil belum lengkap."""
    p = baca_profil(prof)
    if p is None or pd.isna(r["ts_wib"]):
        return None
    sex, lahir = p
    umur = who.umur_bulan(lahir, r["ts_wib"].date())
    hasil = who.assess_status(sex, umur, r["weight_kg"], r["height_cm"])
    hasil["umur_bulan"] = umur
    return hasil


# ============================================================
# MANAJEMEN STATE INTERAKTIF
# ============================================================
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = True
if "active_metric" not in st.session_state:
    st.session_state.active_metric = "KG"  # Pilihan: KG, CM, BMI
if "auto_refresh" not in st.session_state:
    st.session_state.auto_refresh = True   # Aktif agar data baru dari alat langsung muncul

if st.session_state.auto_refresh:
    st_autorefresh(interval=5000, limit=None, key="mqtt_live_refresh")


# ============================================================
# PENYIMPANAN DATA & LISTENER MQTT
# ============================================================
@st.cache_resource
class MQTTDataStore:
    """Menyimpan status alat (heartbeat dari ESP32). Data pengukuran dibaca dari database bot."""
    def __init__(self):
        self.state = None            # 'online' / 'offline' / None (belum ada kabar)
        self.uptime = None
        self.last_mono = None
        self.last_seen = "-"
        self.connected = False

    def touch(self, state="online", uptime=None):
        self.state = state
        self.last_mono = time.monotonic()
        self.last_seen = wib_now().strftime("%H:%M:%S")
        if uptime is not None:
            self.uptime = uptime

    def online(self):
        return (self.last_mono is not None and self.state != "offline"
                and time.monotonic() - self.last_mono <= OFFLINE_SECONDS)


store = MQTTDataStore()


@st.cache_resource
def start_mqtt_listener():
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            store.connected = True
            client.subscribe([(TOPIC_STATUS, 1)])

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if msg.topic != TOPIC_STATUS or not isinstance(payload, dict):
                return
            state = payload.get("state")
            if state not in ("online", "offline"):
                return
            if msg.retain:               # pesan lama dari broker: jangan dianggap baru saja online
                if state == "offline":
                    store.state = "offline"
                return
            if state == "online":
                up = payload.get("uptime")
                store.touch("online", up if isinstance(up, int) else None)
            else:
                store.state = "offline"
        except Exception:
            pass

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception:
        pass
    return client


mqtt_client = start_mqtt_listener()

# ============================================================
# SIDEBAR: PENGATURAN & UJI COBA
# ============================================================
with st.sidebar:
    st.markdown("### Pengaturan")
    st.toggle(
        "Perbarui data otomatis",
        key="auto_refresh",
        help="Nyalakan agar pengukuran baru dari alat langsung muncul di layar.",
    )
    st.caption("Saat menyala, layar memeriksa data baru setiap 5 detik.")
    st.caption(f"Database bot: `{DB_PATH.name}`")

    with st.expander("Uji coba alat (untuk petugas teknis)"):
        st.caption(
            f"Mengirim data uji ke broker `{MQTT_BROKER}`. Bot Telegram menyimpannya sebagai data yang "
            "belum dikaitkan; kaitkan ke bayi lewat menu Catat Pengukuran."
        )
        sim_w = st.slider("Berat badan (kg)", 2.0, 20.0, 6.95, 0.05)
        sim_h = st.slider("Panjang badan (cm)", 30.0, 120.0, 64.2, 0.1)
        if st.button("Kirim data uji ke broker", use_container_width=True, type="primary"):
            payload = {
                "measurement_id": f"{DEVICE_ID}-dash-{int(time.time())}",
                "device_id": DEVICE_ID,
                "weight_kg": round(sim_w, 2),
                "height_cm": round(sim_h, 1),
                "source": "sim-dashboard",
                "timestamp": utc_now_str(),
            }
            try:
                mqtt_client.publish(TOPIC_MEASUREMENT, json.dumps(payload), qos=1)
                st.toast("Data uji dikirim. Buka bot Telegram untuk mengaitkannya.", icon="📤")
            except Exception:
                st.error("Gagal mengirim ke broker.")

# ============================================================
# WARNA & CSS (TERANG / GELAP)
# ============================================================
is_dark = st.session_state.dark_mode

c_bg_main = "#0F1115" if is_dark else "#F2F4F7"
c_bg_card = "#181B20" if is_dark else "#FFFFFF"
c_bg_soft_blue = "#162636" if is_dark else "#E3F2FD"
c_bg_pill = "#232730" if is_dark else "#EEF0F4"
c_text_primary = "#F8F9FA" if is_dark else "#141518"
c_text_secondary = "#A6ACB9" if is_dark else "#555C69"
c_border = "#2A2F39" if is_dark else "#DDE1E7"
c_lime = "#CCF655"
c_lime_text = "#141518"
c_accent = "#CCF655" if is_dark else "#5E8C00"      # warna garis grafik (lime terlalu pucat di latar putih)
c_row_hi = "rgba(204,246,85,0.13)" if is_dark else "rgba(120,170,0,0.14)"
c_shadow = "none" if is_dark else "0 1px 3px rgba(20,21,24,0.06)"
c_bad = "#FF7A7A"
c_warn = "#FFC857" if is_dark else "#F5A524"

CSS = Template("""
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root, .stApp { color-scheme: $scheme; }

/* ---------- Dasar halaman ---------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background-color: $bg !important;
    color: $text !important;
}
html, body, .stApp, .stApp p, .stApp label, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp button, .stApp input, .stApp textarea, .stApp table, .stApp th, .stApp td,
.stApp [data-testid="stMarkdownContainer"] {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
}
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* Semua teks bawaan Streamlit mengikuti warna tema kita (inilah perbaikan teks putih di mode terang) */
.stApp p, .stApp li, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp [data-testid="stMarkdownContainer"], .stApp [data-testid="stWidgetLabel"],
.stApp [data-testid="stWidgetLabel"] * { color: $text !important; }
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stCaptionContainer"] * { color: $text2 !important; }
.stApp code { background: $pill !important; color: $text !important; border-radius: 6px; }

.block-container { padding-top: 2.6rem !important; padding-bottom: 3rem !important; max-width: 1320px !important; }

/* ---------- Header & toolbar Streamlit ---------- */
header[data-testid="stHeader"] { background: transparent !important; }
header[data-testid="stHeader"] *, [data-testid="stToolbar"] * { color: $text2 !important; }
[data-testid="stAppDeployButton"], .stDeployButton { display: none !important; }
[data-testid="stSidebarCollapseButton"] *, [data-testid="stExpandSidebarButton"] *, [data-testid="collapsedControl"] * {
    color: $text2 !important;
}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child {
    background: $card !important;
}
[data-testid="stSidebar"] { border-right: 1px solid $border; }
[data-testid="stSidebar"] * { color: $text !important; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] * { color: $text2 !important; }
[data-testid="stExpander"] details {
    background: $pill !important;
    border: 1px solid $border !important;
    border-radius: 18px !important;
}

/* ---------- Tombol ---------- */
.stApp .stButton > button, .stApp button[data-testid^="stBaseButton"] {
    border-radius: 50px !important;
    border: 1px solid $border !important;
    background-color: $card !important;
    color: $text !important;
    font-weight: 600 !important;
    font-size: 0.92rem !important;
    min-height: 44px;
    padding: 0.5rem 1.1rem !important;
    box-shadow: $shadow;
    transition: all 0.2s ease;
}
.stApp .stButton > button *, .stApp button[data-testid^="stBaseButton"] * { color: inherit !important; }
.stApp .stButton > button:hover, .stApp button[data-testid^="stBaseButton"]:hover {
    border-color: $lime !important;
    background-color: $lime !important;
    color: $limetext !important;
}
.stApp .stButton > button[kind="primary"], .stApp button[data-testid="stBaseButton-primary"] {
    background-color: $lime !important;
    border-color: $lime !important;
    color: $limetext !important;
}
.stApp .stButton > button[kind="primary"] *, .stApp button[data-testid="stBaseButton-primary"] * {
    color: $limetext !important;
}
.stApp .stButton > button:hover * , .stApp button[data-testid^="stBaseButton"]:hover * { color: $limetext !important; }

/* ---------- Notifikasi (toast) ---------- */
[data-testid="stToast"] { background: $card !important; border: 1px solid $border !important; }
[data-testid="stToast"] * { color: $text !important; }

/* ---------- Grafik ---------- */
.stApp div[data-testid="stPlotlyChart"] {
    background: $card;
    border: 1px solid $border;
    border-radius: 24px;
    overflow: hidden;
    box-shadow: $shadow;
}

/* ---------- Komponen kartu ---------- */
.top-nav {
    display: flex; justify-content: space-between; align-items: center;
    background: $card; padding: 12px 22px; border-radius: 28px;
    border: 1px solid $border; box-shadow: $shadow;
}
.brand-pill {
    display: inline-flex; align-items: center; gap: 12px;
    background: $lime; padding: 6px 20px 6px 8px; border-radius: 50px; font-size: 0.95rem;
}
.brand-icon-circle {
    width: 36px; height: 36px; border-radius: 50%; background: #141518;
    display: flex; align-items: center; justify-content: center; font-size: 1.1rem;
}
.stApp .brand-pill, .stApp .brand-pill * { color: $limetext !important; }
.stApp .brand-pill .brand-icon-circle, .stApp .brand-pill .brand-icon-circle * { color: $lime !important; }

.date-pill {
    display: inline-flex; align-items: center; gap: 8px;
    background: $pill; color: $text; padding: 8px 16px; border-radius: 50px;
    font-size: 0.88rem; font-weight: 600; border: 1px solid $border;
}
.pill-dark { background: $text; color: $card; border-color: $text; }
.pill-ok, .pill-bad {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 16px; border-radius: 50px; font-size: 0.88rem; font-weight: 700;
}
.pill-ok { background: $lime; }
.pill-bad { background: $bad; }
.stApp .pill-ok, .stApp .pill-ok *, .stApp .pill-bad, .stApp .pill-bad * { color: $limetext !important; }

.section-title { font-size: 2.0rem; font-weight: 700; line-height: 1.2; color: $text; margin: 0; }
.section-subtitle { font-size: 1.0rem; color: $text2; margin-top: 6px; margin-bottom: 18px; }
.sec-title { font-size: 1.35rem; font-weight: 700; color: $text; }
.sec-sub { font-size: 0.92rem; color: $text2; margin-top: 2px; margin-bottom: 12px; }

.bento-card {
    background: $card; border-radius: 28px; padding: 24px;
    border: 1px solid $border; box-sizing: border-box; box-shadow: $shadow;
}
.bento-card-blue {
    background: $blue; border-radius: 28px; padding: 24px;
    border: 1px solid $border; box-sizing: border-box; box-shadow: $shadow;
}
.bento-card-lime {
    background: $lime; border-radius: 28px; padding: 24px; box-sizing: border-box;
}
.stApp .bento-card-lime, .stApp .bento-card-lime * { color: $limetext !important; }
.stat-card { min-height: 205px; }
.card-label { font-size: 0.98rem; font-weight: 600; color: $text2; }
.card-note { font-size: 0.86rem; color: $text2; }
.metric-big { font-size: 3.1rem; font-weight: 700; line-height: 1.05; letter-spacing: -1.5px; margin: 12px 0 10px 0; }
.metric-unit { font-size: 1.4rem; font-weight: 500; }

.lime-badge {
    background: $lime; padding: 3px 10px; border-radius: 50px;
    font-size: 0.74rem; font-weight: 700; display: inline-flex; align-items: center; gap: 4px;
}
.white-badge {
    background: #FFFFFF; padding: 5px 14px; border-radius: 50px;
    font-size: 0.82rem; font-weight: 700; display: inline-flex; align-items: center; gap: 6px;
}
.stApp .lime-badge, .stApp .lime-badge *, .stApp .white-badge, .stApp .white-badge * { color: $limetext !important; }

.kv {
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    padding: 9px 0; border-top: 1px solid $border; font-size: 0.9rem;
}
.kv span { color: $text2; }
.step { display: flex; gap: 12px; align-items: flex-start; margin-top: 12px; font-size: 0.92rem; line-height: 1.45; }
.step-no {
    flex: 0 0 26px; width: 26px; height: 26px; border-radius: 50%; background: $lime;
    display: inline-flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.8rem;
}
.stApp .step-no { color: $limetext !important; }

/* ---------- Tabel data ---------- */
.table-wrap { overflow-x: auto; }
.stApp table.data-table { width: 100%; border-collapse: collapse; margin: 0; }
.stApp .data-table th {
    text-align: left; font-size: 0.8rem; font-weight: 700; color: $text2 !important;
    padding: 10px 14px; border: none !important; border-bottom: 1px solid $border !important;
    background: transparent !important; white-space: nowrap;
}
.stApp .data-table td {
    padding: 13px 14px; font-size: 0.95rem; color: $text !important;
    border: none !important; border-bottom: 1px solid $border !important;
    background: transparent !important; white-space: nowrap;
}
.stApp .data-table tr:last-child td { border-bottom: none !important; }
.stApp .data-table tr.latest td { background: $rowhi !important; }
.stApp .data-table tr.latest td:first-child { border-radius: 14px 0 0 14px; }
.stApp .data-table tr.latest td:last-child { border-radius: 0 14px 14px 0; }
.stApp .data-table .lime-badge, .stApp .data-table .lime-badge * { color: $limetext !important; }
.stApp .data-table .lime-badge { background: $lime !important; margin-left: 8px; }

/* ---------- Isian & pilihan ---------- */
.stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"], .stApp [data-baseweb="base-input"] {
    background-color: $pill !important; border-color: $border !important; border-radius: 16px !important;
}
.stApp [data-baseweb="select"] *, .stApp input { color: $text !important; }
div[data-baseweb="popover"] ul { background: $card !important; }
div[data-baseweb="popover"] li, div[data-baseweb="popover"] li * { color: $text !important; }
div[data-baseweb="popover"] li:hover { background: $pill !important; }
[data-testid="stForm"] { border: 1px solid $border !important; border-radius: 22px !important; }
.stApp button[data-testid="stBaseButton-primaryFormSubmit"] {
    background-color: $lime !important; border-color: $lime !important; color: $limetext !important;
}
.stApp button[data-testid="stBaseButton-primaryFormSubmit"] * { color: $limetext !important; }

/* ---------- Status gizi ---------- */
.bento-card-warn { background: $warn; border-radius: 28px; padding: 24px; box-sizing: border-box; }
.bento-card-bad { background: $bad; border-radius: 28px; padding: 24px; box-sizing: border-box; }
.stApp .bento-card-warn, .stApp .bento-card-warn *, .stApp .bento-card-bad, .stApp .bento-card-bad * {
    color: $limetext !important;
}
.metric-mid { font-size: 2.2rem; font-weight: 700; line-height: 1.1; letter-spacing: -1px; margin: 12px 0 10px 0; }
.metric-unit-sm { font-size: 1rem; font-weight: 500; }
.verdict { font-size: 1.45rem; font-weight: 700; line-height: 1.25; margin: 12px 0 8px 0; }
.st-badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px;
    border-radius: 50px; font-size: 0.8rem; font-weight: 700;
}
.st-badge.ok { background: $lime; }
.st-badge.warn { background: $warn; }
.st-badge.bad { background: $bad; }
.st-badge.na { background: $pill; border: 1px solid $border; }
.stApp .st-badge.ok, .stApp .st-badge.ok *, .stApp .st-badge.warn, .stApp .st-badge.warn *,
.stApp .st-badge.bad, .stApp .st-badge.bad * { color: $limetext !important; }
.stApp .st-badge.na, .stApp .st-badge.na * { color: $text2 !important; }
</style>
""")

render_html(CSS.substitute(
    scheme="dark" if is_dark else "light",
    bg=c_bg_main, card=c_bg_card, blue=c_bg_soft_blue, pill=c_bg_pill,
    text=c_text_primary, text2=c_text_secondary, border=c_border,
    lime=c_lime, limetext=c_lime_text, rowhi=c_row_hi, shadow=c_shadow, bad=c_bad, warn=c_warn,
))

# ============================================================
# PENGOLAHAN DATA
# ============================================================
if not DB_PATH.exists():
    st.error(
        f"File database bot tidak ditemukan: {DB_PATH}\n\n"
        "Letakkan babygrowth.db di folder yang sama dengan dashboard.py, atau isi variabel lingkungan "
        "BABYGROWTH_DB dengan path lengkap file tersebut (harus sama dengan DB_PATH yang dipakai bot.py)."
    )
    st.stop()

try:
    profiles = load_children()
    df_all, n_belum, belum_terbaru = load_measurements()
except sqlite3.Error as e:
    st.error(f"Database bot belum bisa dibaca ({e}). Jalankan bot.py sekali agar tabelnya dibuat, lalu muat ulang.")
    st.stop()

ids = list(profiles)
if "pending_select" in st.session_state:                      # setelah menambah bayi baru
    st.session_state.selected_child = st.session_state.pop("pending_select")
if ids and st.session_state.get("selected_child") not in ids:
    st.session_state.selected_child = df_all.iloc[-1]["child_id"] if len(df_all) else ids[0]

alat_online = store.online()
uptime_sec = store.uptime or 0

# Beri tahu bila ada data baru yang masuk (untuk bayi mana pun)
if len(df_all):
    last_row = df_all.iloc[-1]
    latest_key = f"{last_row['measurement_id']}|{last_row['timestamp']}"
    prev_key = st.session_state.get("last_seen_key")
    if prev_key is None:
        st.session_state.last_seen_key = latest_key
    elif prev_key != latest_key:
        st.session_state.last_seen_key = latest_key
        st.toast(
            f"Data baru masuk ({label_bayi(last_row['child_id'], profiles)}): "
            f"{fnum(last_row['weight_kg'], 2, ' kg')}, {fnum(last_row['height_cm'], 1, ' cm')}",
            icon="✅",
        )

# ============================================================
# BARIS ATAS: IDENTITAS, TANGGAL, GANTI TEMA
# ============================================================
now = wib_now()
tanggal_str = f"{HARI[now.weekday()]}, {now.day} {BULAN[now.month - 1]} {now.year}"

nav_c1, nav_c2 = st.columns([9.3, 2.0])
with nav_c1:
    render_html(f"""
    <div class="top-nav">
        <div class="brand-pill">
            <div class="brand-icon-circle"><i class="bi bi-heart-pulse-fill"></i></div>
            <div>
                <div style="line-height:1.15; font-weight:700;">BabyGrowth</div>
                <div style="font-size:0.74rem; opacity:0.85;">Pantau tumbuh kembang bayi</div>
            </div>
        </div>
        <div class="date-pill"><i class="bi bi-calendar3"></i><span>{tanggal_str}</span></div>
    </div>
    """)
with nav_c2:
    st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
    if st.button("Mode Terang" if is_dark else "Mode Gelap", use_container_width=True):
        st.session_state.dark_mode = not st.session_state.dark_mode
        st.rerun()

st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

# ============================================================
# PILIH BAYI & PROFIL
# ============================================================
def render_profil_form(kode, prof):
    with st.expander("Ubah profil bayi"):
        with st.form(f"form_profil_{kode}"):
            nama = st.text_input("Nama bayi", value=prof.get("nama", ""), key=f"nama_{kode}",
                                 placeholder="mis. Aisyah Putri")
            sex_now = prof.get("sex")
            sex = st.radio(
                "Jenis kelamin", ["L", "P"],
                index=["L", "P"].index(sex_now) if sex_now in ("L", "P") else None,
                format_func=who.SEX_LABEL.get, horizontal=True, key=f"sex_{kode}",
            )
            try:
                lahir_now = date.fromisoformat(prof.get("lahir"))
            except (TypeError, ValueError):
                lahir_now = None
            hari_ini = wib_now().date()
            batas = hari_ini - timedelta(days=MAX_UMUR_HARI)
            lahir = st.date_input(
                "Tanggal lahir", value=lahir_now, key=f"lahir_{kode}", format="DD/MM/YYYY",
                min_value=min(batas, lahir_now) if lahir_now else batas, max_value=hari_ini,
            )
            simpan = st.form_submit_button("Simpan profil", type="primary")
        if simpan:
            if not NAMA_RE.fullmatch(nama.strip()):
                st.error("Nama hanya boleh huruf, angka, spasi, titik, koma, tanda hubung (maks 40 karakter).")
            elif sex not in ("L", "P"):
                st.error("Pilih jenis kelamin dulu.")
            elif lahir is None:
                st.error("Pilih tanggal lahir dulu.")
            elif simpan_anak(nama.strip(), sex, lahir.isoformat(), prof["id"]):
                st.toast("Profil bayi disimpan (juga berlaku di bot Telegram)", icon="✅")
                st.rerun()
            else:
                st.error("Profil tidak bisa disimpan ke database bot.")


def render_tambah_bayi(buka=False):
    with st.expander("Tambah bayi baru", expanded=buka):
        n = st.session_state.get("baru_n", 0)          # ganti nomor setiap sukses, supaya isian kosong lagi
        hari_ini = wib_now().date()
        with st.form(f"form_bayi_baru_{n}"):
            nama_baru = st.text_input("Nama bayi", key=f"baru_nama_{n}", placeholder="mis. Aisyah Putri",
                                      help="Kode bayi (mis. A002) dibuat otomatis, sama seperti di bot Telegram.")
            sex_baru = st.radio("Jenis kelamin", ["L", "P"], index=None, format_func=who.SEX_LABEL.get,
                                horizontal=True, key=f"baru_sex_{n}")
            lahir_baru = st.date_input("Tanggal lahir", value=None, key=f"baru_lahir_{n}", format="DD/MM/YYYY",
                                       min_value=hari_ini - timedelta(days=MAX_UMUR_HARI), max_value=hari_ini)
            tambah = st.form_submit_button("Tambah bayi", type="primary")
        if tambah:
            if not NAMA_RE.fullmatch(nama_baru.strip()):
                st.error("Nama hanya boleh huruf, angka, spasi, titik, koma, tanda hubung (maks 40 karakter).")
            elif sex_baru not in ("L", "P"):
                st.error("Pilih jenis kelamin dulu.")
            elif lahir_baru is None:
                st.error("Pilih tanggal lahir dulu.")
            else:
                kb = simpan_anak(nama_baru.strip(), sex_baru, lahir_baru.isoformat())
                if kb:
                    st.session_state["pending_select"] = kb
                    st.session_state["baru_n"] = n + 1
                    st.toast(f"Bayi {kb} ditambahkan (juga muncul di bot Telegram)", icon="✅")
                    st.rerun()
                else:
                    st.error("Bayi tidak bisa disimpan ke database bot.")


if not ids:
    st.info("Belum ada bayi terdaftar. Tambahkan di bawah ini, atau lewat bot Telegram (menu Data Anak).")
    render_tambah_bayi(buka=True)
    st.stop()

sel_c1, sel_c2 = st.columns([4.2, 7.8], gap="medium")
with sel_c1:
    kode = st.selectbox(
        "Pilih data bayi", ids, key="selected_child",
        format_func=lambda k: label_bayi(k, profiles),
        help="Daftar bayi diambil dari bot Telegram. Kode (mis. A001) sama dengan yang tampil di bot.",
    )

prof = profiles.get(kode) or {}
profil_ok = baca_profil(prof)
df = df_all[df_all["child_id"] == kode].reset_index(drop=True)

with sel_c2:
    pills = []
    if prof.get("nama"):
        pills.append(f'<div class="date-pill pill-dark"><i class="bi bi-person-heart"></i><span>{escape(prof["nama"])}</span></div>')
    pills.append(f'<div class="date-pill"><i class="bi bi-upc-scan"></i><span>{escape(kode)}</span></div>')
    if profil_ok:
        sex_p, lahir_p = profil_ok
        tgl_acuan = df.iloc[-1]["ts_wib"].date() if len(df) and not pd.isna(df.iloc[-1]["ts_wib"]) else wib_now().date()
        umur_p = who.umur_bulan(lahir_p, tgl_acuan)
        pills.append(f'<div class="date-pill"><i class="bi bi-gender-ambiguous"></i><span>{who.SEX_LABEL[sex_p]}</span></div>')
        pills.append(f'<div class="date-pill"><i class="bi bi-cake2"></i><span>Lahir {lahir_p.day} {BULAN[lahir_p.month - 1]} {lahir_p.year}</span></div>')
        if umur_p >= 0:
            pills.append(f'<div class="date-pill"><i class="bi bi-hourglass-split"></i><span>Umur {who.format_umur(umur_p)}</span></div>')
    render_html(f'<div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:30px;">{"".join(pills)}</div>')

ex_a, ex_b = st.columns(2, gap="medium")
with ex_a:
    render_profil_form(kode, prof)
with ex_b:
    render_tambah_bayi()

if n_belum:
    ket = ""
    if belum_terbaru:
        ket = f" (terbaru: {fnum(belum_terbaru['w'], 2, ' kg')}, {fnum(belum_terbaru['h'], 1, ' cm')}, {fmt_waktu(belum_terbaru['ts'])} WIB)"
    st.info(
        f"Ada {n_belum} data dari alat yang belum dikaitkan ke bayi mana pun{ket}. "
        "Kaitkan lewat bot Telegram: menu Catat Pengukuran, pilih bayi, lalu Kaitkan."
    )

if df.empty:
    st.info("Bayi ini belum punya pengukuran. Data muncul di sini setelah dikaitkan lewat bot Telegram (menu Catat Pengukuran).")
    st.stop()

df["bmi"] = (df["weight_kg"] / (df["height_cm"] / 100) ** 2).where(df["height_cm"] > 0).round(2)
df["w_diff"] = df["weight_kg"].diff()
df["h_diff"] = df["height_cm"].diff()

latest = df.iloc[-1]
ada_prev = len(df) > 1
w_now = nz(latest["weight_kg"])
w_txt, w_icon = teks_selisih(latest["w_diff"], "kg", 2, ada_prev)
h_txt, h_icon = teks_selisih(latest["h_diff"], "cm", 1, ada_prev)
total_rec = len(df)
child_id = escape(label_bayi(kode, profiles))

st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

# ============================================================
# JUDUL & STATUS
# ============================================================
head_l, head_r = st.columns([6.2, 5.8])
with head_l:
    render_html("""
    <div class="section-title">Pemantauan Pertumbuhan Bayi</div>
    <div class="section-subtitle">Catatan berat dan panjang badan bayi dari alat ukur</div>
    """)
with head_r:
    refresh_txt = "Data diperbarui otomatis" if st.session_state.auto_refresh else "Perbarui data manual"
    refresh_icon = "bi-arrow-repeat" if st.session_state.auto_refresh else "bi-pause-circle"
    pill_cls = "pill-ok" if alat_online else "pill-bad"
    alat_txt = "Alat terhubung" if alat_online else "Alat tidak terhubung"
    render_html(f"""
    <div style="display:flex; justify-content:flex-end; align-items:center; gap:10px; flex-wrap:wrap; margin-top:6px;">
        <div class="date-pill"><i class="bi {refresh_icon}"></i><span>{refresh_txt}</span></div>
        <div class="{pill_cls}"><i class="bi bi-circle-fill" style="font-size:0.55rem;"></i><span>{alat_txt}</span></div>
    </div>
    """)

# ============================================================
# BARIS 1: BERAT, PANJANG, BMI
# ============================================================
c1, c2, c3 = st.columns(3, gap="medium")
with c1:
    render_html(f"""
    <div class="bento-card stat-card">
        <div class="card-label"><i class="bi bi-speedometer2"></i> &nbsp;Berat Badan</div>
        <div class="metric-big">{w_now:.2f}<span class="metric-unit"> kg</span></div>
        <div class="card-note"><i class="bi {w_icon}"></i> {w_txt}</div>
    </div>
    """)
with c2:
    render_html(f"""
    <div class="bento-card-blue stat-card">
        <div class="card-label"><i class="bi bi-rulers"></i> &nbsp;Panjang Badan</div>
        <div class="metric-big">{fnum(latest["height_cm"], 1)}<span class="metric-unit"> cm</span></div>
        <div class="card-note"><i class="bi {h_icon}"></i> {h_txt}</div>
    </div>
    """)
with c3:
    render_html(f"""
    <div class="bento-card-lime stat-card">
        <div style="font-size:0.98rem; font-weight:600;"><i class="bi bi-calculator"></i> &nbsp;BMI (Indeks Massa Tubuh)</div>
        <div class="metric-big">{fnum(latest["bmi"], 2)}</div>
        <div style="font-size:0.86rem;">Dihitung dari berat dan panjang badan &bull; Bayi: <strong>{child_id}</strong></div>
        <div class="white-badge" style="margin-top:12px;"><i class="bi bi-clock"></i> Diukur {fmt_waktu(latest['ts_wib'])} WIB</div>
    </div>
    """)

st.markdown("<div style='height:22px;'></div>", unsafe_allow_html=True)

# ============================================================
# STATUS GIZI & PERTUMBUHAN (WHO 2006 + Permenkes 2/2020)
# ============================================================
df_ukur = df[df["source"] != "tare-calibrated"]          # abaikan data "nolkan timbangan"
ref = df_ukur.iloc[-1] if len(df_ukur) else latest
hasil = nilai_baris(ref, prof)

render_html(f"""
<div class="sec-title">Status Gizi dan Pertumbuhan</div>
<div class="sec-sub">Dinilai dari pengukuran terakhir ({fmt_waktu(ref['ts_wib'])} WIB) dengan standar WHO 2006 dan Permenkes No. 2 Tahun 2020.</div>
""")

pesan = None
if hasil is None:
    pesan = ("Profil bayi belum lengkap",
             "Isi nama, jenis kelamin, dan tanggal lahir pada bagian profil di atas agar status gizi bisa dihitung.")
elif hasil["state"] == "umur_negatif":
    pesan = ("Tanggal lahir tidak sesuai",
             "Tanggal lahir lebih baru daripada tanggal pengukuran. Periksa kembali tanggal lahir di profil.")
elif hasil["state"] == "di_luar_rentang":
    pesan = ("Umur di luar rentang tabel",
             f"Umur bayi {who.format_umur(hasil['age_months'])}. Tabel standar yang dipakai baru mencakup umur 0 sampai 24 bulan.")

if pesan:
    render_html(f"""
    <div class="bento-card">
        <div class="card-label"><i class="bi bi-info-circle"></i> &nbsp;{pesan[0]}</div>
        <div class="card-note" style="margin-top:10px;">{pesan[1]}</div>
    </div>
    """)
else:
    ring = hasil["ringkasan"]
    kelas = {"ok": "bento-card-lime", "warn": "bento-card-warn", "bad": "bento-card-bad"}.get(ring["level"], "bento-card")
    ikon = {"ok": "bi-check-circle-fill", "warn": "bi-exclamation-triangle-fill",
            "bad": "bi-exclamation-octagon-fill"}.get(ring["level"], "bi-info-circle")
    umur_txt = who.format_umur(hasil["umur_bulan"])
    saran = ("Pertahankan pemantauan rutin setiap bulan." if ring["level"] == "ok"
             else "Sebaiknya konsultasikan ke bidan, dokter, atau ahli gizi.")

    IND = [
        ("bbu", "Berat sesuai umur", "bi-speedometer2", "kg", 2, ref["weight_kg"]),
        ("pbu", "Panjang sesuai umur", "bi-rulers", "cm", 1, ref["height_cm"]),
        ("bbpb", "Berat sesuai panjang", "bi-activity", "kg", 2, ref["weight_kg"]),
    ]
    k0, k1, k2, k3 = st.columns([1.45, 1, 1, 1], gap="medium")
    with k0:
        render_html(f"""
        <div class="{kelas} stat-card">
            <div style="font-size:0.98rem; font-weight:600;"><i class="bi {ikon}"></i> &nbsp;Kesimpulan</div>
            <div class="verdict">{escape(ring['title'])}</div>
            <div style="font-size:0.88rem; line-height:1.45;">{escape(ring['detail'])}<br>{saran}</div>
            <div class="white-badge" style="margin-top:12px;"><i class="bi bi-hourglass-split"></i> Umur saat diukur {umur_txt}</div>
        </div>
        """)
    for kolom, (kd, judul, ic, unit, dec, ukur) in zip([k1, k2, k3], IND):
        ind = hasil[kd]
        lvl = LEVEL_CLASS.get(ind["level"], "na")
        nilai = "-" if pd.isna(ukur) else f"{ukur:.{dec}f}"
        with kolom:
            render_html(f"""
            <div class="bento-card stat-card">
                <div class="card-label"><i class="bi {ic}"></i> &nbsp;{judul}</div>
                <div class="metric-mid">{nilai}<span class="metric-unit-sm"> {unit}</span></div>
                <div class="st-badge {lvl}">{escape(ind['label'])}</div>
            </div>
            """)
    st.caption("Hasil ini hanya alat bantu skrining, bukan diagnosis. Konfirmasikan ke tenaga kesehatan.")

st.markdown("<div style='height:22px;'></div>", unsafe_allow_html=True)

# ============================================================
# BARIS 2: GRAFIK PERKEMBANGAN + STATUS ALAT & PETUNJUK
# ============================================================
g_left, g_right = st.columns([7.2, 4.8], gap="medium")

with g_left:
    metric_map = {
        "KG": ("weight_kg", "kg", "Berat Badan", 2),
        "CM": ("height_cm", "cm", "Panjang Badan", 1),
        "BMI": ("bmi", "", "BMI", 2),
    }
    y_col, unit, title, dec = metric_map[st.session_state.active_metric]
    recent = df.tail(12).reset_index(drop=True)

    render_html(f"""
    <div class="sec-title">Grafik Perkembangan</div>
    <div class="sec-sub">Menampilkan {len(recent)} pengukuran terakhir. Pilih yang ingin dilihat:</div>
    """)

    b1, b2, b3 = st.columns(3)
    for col_ui, key, label in zip([b1, b2, b3], ["KG", "CM", "BMI"], ["Berat Badan", "Panjang Badan", "BMI"]):
        with col_ui:
            aktif = st.session_state.active_metric == key
            if st.button(label, key=f"metric_{key}", use_container_width=True,
                         type="primary" if aktif else "secondary"):
                st.session_state.active_metric = key
                st.rerun()

    n = len(recent)
    xs = list(range(n))
    vals = recent[y_col].tolist()
    tick_txt = ["-" if pd.isna(t) else t.strftime("%d/%m<br>%H:%M") for t in recent["ts_wib"]]
    hover_txt = [fmt_waktu(t) + " WIB" for t in recent["ts_wib"]]
    sizes = [9] * n
    sizes[-1] = 15

    fig = go.Figure(go.Scatter(
        x=xs, y=vals,
        mode="lines+markers+text",
        line=dict(color=c_accent, width=3),
        marker=dict(size=sizes, color=c_accent, line=dict(width=2, color=c_bg_card)),
        text=["" if pd.isna(v) else f"{v:.{dec}f}" for v in vals],
        textposition="top center",
        textfont=dict(color=c_text_primary, size=12),
        customdata=hover_txt,
        hovertemplate=f"%{{customdata}}<br>{title}: %{{y:.{dec}f}} {unit}<extra></extra>",
        cliponaxis=False,
    ))

    valid = recent[y_col].dropna()
    y_range = None
    if len(valid):
        lo, hi = float(valid.min()), float(valid.max())
        pad = max((hi - lo) * 0.3, hi * 0.02, 0.1)
        y_range = [lo - pad, hi + pad * 1.4]

    fig.update_layout(
        height=340,
        margin=dict(l=55, r=30, t=30, b=50),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Plus Jakarta Sans, sans-serif", color=c_text_secondary),
        hoverlabel=dict(bgcolor=c_bg_card, bordercolor=c_border, font=dict(color=c_text_primary, size=13)),
        showlegend=False,
        xaxis=dict(
            tickmode="array", tickvals=xs, ticktext=tick_txt,
            range=[-0.5, max(n - 0.5, 0.5)],
            showgrid=False, zeroline=False, showline=False,
            tickfont=dict(color=c_text_secondary, size=11),
        ),
        yaxis=dict(
            showgrid=True, gridcolor=c_border, zeroline=False, showline=False,
            ticksuffix=f" {unit}" if unit else "",
            tickfont=dict(color=c_text_secondary, size=11),
            range=y_range,
        ),
    )
    # theme=None: pakai warna kita sendiri, bukan warna tema bawaan Streamlit
    st.plotly_chart(fig, use_container_width=True, theme=None, config={"displayModeBar": False})

with g_right:
    durasi_txt = durasi(uptime_sec)
    render_html(f"""
    <div class="bento-card">
        <div class="card-label"><i class="bi bi-broadcast"></i> &nbsp;Status Alat</div>
        <div style="margin:14px 0 8px 0;">
            <span class="{pill_cls}"><i class="bi bi-circle-fill" style="font-size:0.55rem;"></i>{alat_txt}</span>
        </div>
        <div class="kv"><span>Menyala selama</span><strong>{durasi_txt}</strong></div>
        <div class="kv"><span>Kabar terakhir dari alat</span><strong>{escape(str(store.last_seen))}{"" if store.last_seen == "-" else " WIB"}</strong></div>
        <div class="kv"><span>Pengukuran tercatat (bayi ini)</span><strong>{total_rec}</strong></div>
    </div>
    """)
    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
    render_html("""
    <div class="bento-card">
        <div class="card-label"><i class="bi bi-info-circle"></i> &nbsp;Cara Menggunakan Alat</div>
        <div class="step"><span class="step-no">1</span><span>Letakkan bayi dengan tenang di atas alat ukur.</span></div>
        <div class="step"><span class="step-no">2</span><span>Tekan tombol <strong>sekali (singkat)</strong> untuk mengirim hasil ke bot Telegram.</span></div>
        <div class="step"><span class="step-no">3</span><span>Di Telegram: <strong>Catat Pengukuran</strong>, pilih bayi, lalu <strong>Kaitkan</strong>. Data langsung tampil di layar ini.</span></div>
        <div class="step"><span class="step-no">4</span><span>Untuk menolkan timbangan, <strong>tahan tombol 1,5 detik</strong> sebelum bayi diletakkan.</span></div>
    </div>
    """)

st.markdown("<div style='height:26px;'></div>", unsafe_allow_html=True)

# ============================================================
# BARIS 3: DATA TERBARU DARI ALAT
# ============================================================
catatan = (
    "Pengukuran baru dari alat muncul otomatis di baris paling atas."
    if st.session_state.auto_refresh
    else "Nyalakan &ldquo;Perbarui data otomatis&rdquo; di panel samping agar data baru langsung muncul."
)
render_html(f"""
<div class="sec-title">Data Terbaru dari Alat</div>
<div class="sec-sub">10 pengukuran terakhir yang sudah dikaitkan di bot Telegram. {catatan}</div>
""")

tabel_rows = []
for i, (_, r) in enumerate(df.tail(10).iloc[::-1].iterrows()):
    badge = '<span class="lime-badge">Terbaru</span>' if i == 0 else ""
    cls = ' class="latest"' if i == 0 else ""
    w_change = "-" if pd.isna(r["w_diff"]) else f"{r['w_diff']:+.2f} kg"
    h_row = nilai_baris(r, prof)
    if h_row is None or h_row["state"] != "ok":
        status_cell = '<span class="st-badge na">-</span>'
    else:
        rg = h_row["ringkasan"]
        status_cell = f'<span class="st-badge {LEVEL_CLASS.get(rg["level"], "na")}">{escape(rg["singkat"])}</span>'
    tabel_rows.append(
        f"<tr{cls}>"
        f"<td>{fmt_waktu(r['ts_wib'])} WIB{badge}</td>"
        f"<td><strong>{fnum(r['weight_kg'], 2)}</strong> kg</td>"
        f"<td><strong>{fnum(r['height_cm'], 1)}</strong> cm{' <span class=\"st-badge na\">manual</span>' if r['height_manual'] else ''}</td>"
        f"<td>{fnum(r['bmi'], 2)}</td>"
        f"<td>{w_change}</td>"
        f"<td>{status_cell}</td>"
        f"</tr>"
    )

render_html(f"""
<div class="bento-card">
    <div class="table-wrap">
        <table class="data-table">
            <thead><tr>
                <th>Waktu Pengukuran</th><th>Berat Badan</th><th>Panjang Badan</th><th>BMI</th><th>Perubahan Berat</th><th>Status Gizi</th>
            </tr></thead>
            <tbody>{''.join(tabel_rows)}</tbody>
        </table>
    </div>
</div>
""")