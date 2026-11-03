# who_standards.py - Acuan WHO Child Growth Standards (usia 0-24 bulan)
#
# DUA METODE z-score, dipilih otomatis:
#   1) LMS PENUH (resmi WHO)  : bila folder who_lms/ berisi 4 file CSV tabel resmi WHO
#        lhfa_boys.csv, lhfa_girls.csv   (length/height-for-age, kolom Month,L,M,S)
#        wfa_boys.csv,  wfa_girls.csv    (weight-for-age,        kolom Month,L,M,S)
#      Rumus: z = ((x/M)^L - 1) / (L*S)   (L=0: z = ln(x/M)/S)
#      Untuk berat/umur, z di luar +-3 SD memakai penyesuaian resmi WHO.
#      Cara mengisi: lihat who_lms/PETUNJUK.txt
#   2) INTERPOLASI (cadangan) : bila file CSV belum ada. Memakai tabel -3 SD/-2 SD/median
#      di bawah (diketik manual, dibulatkan 1 desimal) -> z-score hanya PERKIRAAN.
#
# Hasil assess() menyertakan kunci "method" ("LMS" atau "interpolasi") agar bot/laporan
# selalu jujur soal metode yang dipakai.
#
# Kategori mengikuti ambang WHO / Permenkes 2/2020:
#   < -3 SD = sangat pendek / sangat kurang,  < -2 SD = pendek / kurang.
# Sumber resmi: https://www.who.int/tools/child-growth-standards/standards

import csv
import logging
import math
import os

log = logging.getLogger("babygrowth.who")

# ---- Tabel cadangan: (-3 SD, -2 SD, median) per bulan usia, indeks 0..24 ----
#   HFA = panjang badan menurut umur (cm), WFA = berat badan menurut umur (kg)

HFA = {
    "L": [
        (44.2, 46.1, 49.9), (48.9, 50.8, 54.7), (52.4, 54.4, 58.4), (55.3, 57.3, 61.4),
        (57.6, 59.7, 63.9), (59.6, 61.7, 65.9), (61.2, 63.3, 67.6), (62.7, 64.8, 69.2),
        (64.0, 66.2, 70.6), (65.2, 67.5, 72.0), (66.4, 68.7, 73.3), (67.6, 69.9, 74.5),
        (68.6, 71.0, 75.7), (69.6, 72.1, 76.9), (70.6, 73.1, 78.0), (71.6, 74.1, 79.1),
        (72.5, 75.0, 80.2), (73.3, 76.0, 81.2), (74.2, 76.9, 82.3), (75.0, 77.7, 83.2),
        (75.8, 78.6, 84.2), (76.5, 79.4, 85.1), (77.2, 80.2, 86.0), (78.0, 81.0, 86.9),
        (78.7, 81.7, 87.8),
    ],
    "P": [
        (43.6, 45.4, 49.1), (47.8, 49.8, 53.7), (51.0, 53.0, 57.1), (53.5, 55.6, 59.8),
        (55.6, 57.8, 62.1), (57.4, 59.6, 64.0), (58.9, 61.2, 65.7), (60.3, 62.7, 67.3),
        (61.7, 64.0, 68.7), (62.9, 65.3, 70.1), (64.1, 66.5, 71.5), (65.2, 67.7, 72.8),
        (66.3, 68.9, 74.0), (67.3, 70.0, 75.2), (68.3, 71.0, 76.4), (69.3, 72.0, 77.5),
        (70.2, 73.0, 78.6), (71.1, 74.0, 79.7), (72.0, 74.9, 80.7), (72.8, 75.8, 81.7),
        (73.7, 76.7, 82.7), (74.5, 77.5, 83.7), (75.2, 78.4, 84.6), (76.0, 79.2, 85.5),
        (76.7, 80.0, 86.4),
    ],
}

WFA = {
    "L": [
        (2.0, 2.5, 3.3), (2.7, 3.4, 4.5), (3.4, 4.3, 5.6), (4.0, 5.0, 6.4),
        (4.4, 5.6, 7.0), (4.8, 6.0, 7.5), (5.1, 6.4, 7.9), (5.3, 6.7, 8.3),
        (5.6, 6.9, 8.6), (5.8, 7.1, 8.9), (5.9, 7.4, 9.2), (6.1, 7.6, 9.4),
        (6.3, 7.7, 9.6), (6.4, 7.9, 9.9), (6.6, 8.1, 10.1), (6.7, 8.3, 10.3),
        (6.9, 8.4, 10.5), (7.0, 8.6, 10.7), (7.2, 8.8, 10.9), (7.3, 8.9, 11.1),
        (7.5, 9.1, 11.3), (7.6, 9.2, 11.5), (7.8, 9.4, 11.8), (7.9, 9.5, 12.0),
        (8.1, 9.7, 12.2),
    ],
    "P": [
        (2.0, 2.4, 3.2), (2.7, 3.2, 4.2), (3.3, 3.9, 5.1), (3.9, 4.5, 5.8),
        (4.4, 5.0, 6.4), (4.8, 5.4, 6.9), (5.1, 5.7, 7.3), (5.3, 6.0, 7.6),
        (5.6, 6.3, 7.9), (5.8, 6.5, 8.2), (5.9, 6.7, 8.5), (6.1, 6.9, 8.7),
        (6.3, 7.0, 8.9), (6.4, 7.2, 9.2), (6.6, 7.4, 9.4), (6.7, 7.6, 9.6),
        (6.9, 7.7, 9.8), (7.0, 7.9, 10.0), (7.2, 8.1, 10.2), (7.3, 8.2, 10.4),
        (7.5, 8.4, 10.6), (7.7, 8.6, 10.9), (7.8, 8.7, 11.1), (8.0, 8.9, 11.3),
        (8.2, 9.0, 11.5),
    ],
}

MAX_MONTHS = 24
LMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "who_lms")
LMS_FILES = {
    ("HFA", "L"): "lhfa_boys.csv", ("HFA", "P"): "lhfa_girls.csv",
    ("WFA", "L"): "wfa_boys.csv", ("WFA", "P"): "wfa_girls.csv",
}
_FALLBACK = {"HFA": HFA, "WFA": WFA}


# ============================================================
# PEMUAT TABEL LMS (CSV dari WHO)
# ============================================================
def _num(s):
    return float(str(s).strip().replace(",", "."))


def _load_lms_file(path):
    """Baca CSV (pemisah , ; atau tab). Return list 25 tuple (L, M, S) atau None."""
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    delim = max(",;\t", key=lambda d: lines[0].count(d))
    rows = list(csv.reader(lines, delimiter=delim))
    hdr_i = None
    for i, r in enumerate(rows):
        low = [c.strip().lower() for c in r]
        if "l" in low and "m" in low and "s" in low and ("month" in low or "age" in low):
            hdr_i = i
            break
    if hdr_i is None:
        return None
    low = [c.strip().lower() for c in rows[hdr_i]]
    ia = low.index("month") if "month" in low else low.index("age")
    il, im, is_ = low.index("l"), low.index("m"), low.index("s")
    by_month = {}
    for r in rows[hdr_i + 1:]:
        try:
            mth = int(round(_num(r[ia])))
            L, M, S = _num(r[il]), _num(r[im]), _num(r[is_])
        except (ValueError, IndexError):
            continue
        if 0 <= mth <= MAX_MONTHS and M > 0 and S > 0 and math.isfinite(L):
            by_month[mth] = (L, M, S)
    if any(m not in by_month for m in range(MAX_MONTHS + 1)):
        return None
    return [by_month[m] for m in range(MAX_MONTHS + 1)]


def _load_all():
    out = {}
    for key, fname in LMS_FILES.items():
        path = os.path.join(LMS_DIR, fname)
        if not os.path.isfile(path):
            continue
        try:
            data = _load_lms_file(path)
        except Exception as e:  # file rusak jangan membuat bot mati
            log.warning("Gagal membaca %s: %s", fname, e)
            continue
        if data is None:
            log.warning("%s tidak valid (butuh kolom Month,L,M,S untuk bulan 0-24); dilewati", fname)
        else:
            out[key] = data
    return out


LMS = _load_all()


def method_summary():
    n = len(LMS)
    if n == len(LMS_FILES):
        return "LMS penuh (tabel resmi WHO dari who_lms/)"
    if n == 0:
        return "interpolasi (who_lms/ kosong; z-score hanya perkiraan)"
    return f"campuran: {n}/4 tabel LMS terbaca, sisanya interpolasi"


# ============================================================
# RUMUS LMS
# ============================================================
def lms_z(x, L, M, S):
    if L == 0:
        return math.log(x / M) / S
    return ((x / M) ** L - 1.0) / (L * S)


def lms_x(z, L, M, S):
    """Nilai pada z tertentu (kebalikan lms_z)."""
    if L == 0:
        return M * math.exp(S * z)
    base = 1.0 + L * S * z
    return M * base ** (1.0 / L) if base > 0 else float("nan")


def _lms_at(rows, age_m):
    """Interpolasi linear L, M, S antar bulan."""
    i = min(int(age_m), MAX_MONTHS - 1)
    f = min(age_m - i, 1.0)
    a, b = rows[i], rows[i + 1]
    return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))


def _z_lms(ind, x, L, M, S):
    z = lms_z(x, L, M, S)
    if ind == "WFA" and abs(z) > 3:
        # Penyesuaian resmi WHO untuk berat/umur di luar +-3 SD (linear dari selisih 2-3 SD)
        k = 3 if z > 0 else -3
        s3, s2 = lms_x(k, L, M, S), lms_x(k * 2 // 3, L, M, S)
        z = k + (x - s3) / abs(s3 - s2)
    return z


# ============================================================
# CADANGAN: INTERPOLASI -3 SD / -2 SD / MEDIAN
# ============================================================
def _ref(table, sex, age_m):
    """Interpolasi linear antar bulan -> (sd3, sd2, median)."""
    rows = table[sex]
    i = min(int(age_m), MAX_MONTHS - 1)
    f = min(age_m - i, 1.0)
    a, b = rows[i], rows[i + 1]
    return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))


def _z(v, sd3, sd2, med):
    """Perkiraan z-score dari titik -3 SD, -2 SD, median."""
    if v < sd2:
        return -2.0 - (sd2 - v) / (sd2 - sd3)
    return -2.0 + 2.0 * (v - sd2) / (med - sd2)


def _status(v, sd3, sd2):
    return "severe" if v < sd3 else "low" if v < sd2 else "normal"


# ============================================================
# API
# ============================================================
def _evaluate(ind, sex, age_m, v):
    """Return (dict hasil, metode)."""
    if (ind, sex) in LMS:
        L, M, S = _lms_at(LMS[(ind, sex)], age_m)
        sd3, sd2 = lms_x(-3, L, M, S), lms_x(-2, L, M, S)
        return {"value": v, "sd3": sd3, "sd2": sd2, "median": M,
                "z": _z_lms(ind, v, L, M, S), "status": _status(v, sd3, sd2)}, "LMS"
    sd3, sd2, med = _ref(_FALLBACK[ind], sex, age_m)
    return {"value": v, "sd3": sd3, "sd2": sd2, "median": med,
            "z": _z(v, sd3, sd2, med), "status": _status(v, sd3, sd2)}, "interpolasi"


def curve(ind, sex, z, ages):
    """Nilai kurva pada z-score tertentu untuk daftar usia (bulan). Dipakai untuk grafik.

    Mode interpolasi hanya mendukung z = -3, -2, 0 (sesuai tabel cadangan).
    """
    out = []
    for a in ages:
        a = max(0.0, min(float(a), float(MAX_MONTHS)))
        if (ind, sex) in LMS:
            L, M, S = _lms_at(LMS[(ind, sex)], a)
            out.append(lms_x(z, L, M, S))
        else:
            s3, s2, med = _ref(_FALLBACK[ind], sex, a)
            out.append({-3: s3, -2: s2, 0: med}[z])
    return out


def assess(sex, age_months, weight_kg=None, height_cm=None):
    """Return None jika di luar cakupan tabel, selain itu dict hasil.

    need_review = True bila tinggi ATAU berat di bawah -2 SD.
    out["method"] = "LMS" bila semua nilai dihitung dengan tabel LMS resmi.
    """
    if sex not in ("L", "P") or age_months is None or not 0 <= age_months <= MAX_MONTHS:
        return None
    out = {"age_m": age_months, "height": None, "weight": None,
           "need_review": False, "method": "LMS"}
    methods = []
    for key, ind, val in (("height", "HFA", height_cm), ("weight", "WFA", weight_kg)):
        if val is not None:
            out[key], m = _evaluate(ind, sex, age_months, val)
            methods.append(m)
    if any(m != "LMS" for m in methods) or not methods:
        out["method"] = "interpolasi"
    out["need_review"] = any(
        out[k] and out[k]["status"] != "normal" for k in ("height", "weight")
    )
    return out
