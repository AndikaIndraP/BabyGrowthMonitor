# charts.py - Grafik kurva pertumbuhan WHO + titik pengukuran anak (PNG, untuk Telegram)
#
# Memakai Figure API matplotlib (bukan pyplot) sehingga aman dipanggil dari thread
# (bot memanggilnya lewat asyncio.to_thread agar event loop Telegram tidak terblokir).
import io

from matplotlib.figure import Figure

import who_standards

AGES = [i * 0.2 for i in range(int(who_standards.MAX_MONTHS / 0.2) + 1)]  # 0..24 bulan

C_MEDIAN, C_SD2, C_SD3, C_CHILD = "#2e7d32", "#ef8f00", "#c62828", "#1565c0"


def _panel(ax, ind, sex, title, ylabel, pts):
    """pts: list (usia_bulan, nilai). Gambar kurva WHO + titik anak pada satu sumbu."""
    sd3 = who_standards.curve(ind, sex, -3, AGES)
    sd2 = who_standards.curve(ind, sex, -2, AGES)
    med = who_standards.curve(ind, sex, 0, AGES)
    lo = min(sd3) - (max(med) - min(sd3)) * 0.04
    ax.fill_between(AGES, lo, sd3, color=C_SD3, alpha=0.10, lw=0)
    ax.fill_between(AGES, sd3, sd2, color=C_SD2, alpha=0.13, lw=0)
    ax.plot(AGES, med, color=C_MEDIAN, lw=2, label="Median")
    ax.plot(AGES, sd2, color=C_SD2, lw=1.6, ls="--", label="-2 SD")
    ax.plot(AGES, sd3, color=C_SD3, lw=1.6, ls="--", label="-3 SD")

    if pts:
        pts = sorted(pts)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=C_CHILD, lw=1.3, alpha=0.7, zorder=3)
        for age, v in pts:
            s2 = who_standards.curve(ind, sex, -2, [age])[0]
            below = v < s2
            ax.scatter([age], [v], s=46, zorder=4, color=C_SD3 if below else C_CHILD,
                       edgecolor="white", linewidth=0.8)
        age, v = pts[-1]
        ax.scatter([age], [v], s=130, zorder=5, facecolor="none", edgecolor="black", linewidth=1.4)
        ax.annotate(f"{v:.1f}", (age, v), textcoords="offset points", xytext=(8, -14),
                    fontsize=9, fontweight="bold")
        ys = [p[1] for p in pts]
        lo = min(lo, min(ys) - 1)
    hi = max(max(med) * 1.12, max((p[1] for p in pts), default=0) + 1)
    ax.set_xlim(0, who_standards.MAX_MONTHS)
    ax.set_ylim(lo, hi)
    ax.set_xticks(range(0, who_standards.MAX_MONTHS + 1, 3))
    ax.set_xlabel("Usia (bulan)")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11, loc="left", fontweight="bold")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="lower right", fontsize=8, framealpha=0.9)


def growth_chart_png(sex, label, points):
    """points: list (usia_bulan, berat_kg|None, tinggi_cm|None). Return io.BytesIO PNG.

    Titik di luar 0-24 bulan tidak digambar (tabel WHO yang dimuat hanya 0-24 bulan).
    """
    inside = [p for p in points if p[0] is not None and 0 <= p[0] <= who_standards.MAX_MONTHS]
    h_pts = [(a, h) for a, w, h in inside if h is not None]
    w_pts = [(a, w) for a, w, h in inside if w is not None]

    fig = Figure(figsize=(7.2, 9.4), dpi=110)
    ax1, ax2 = fig.subplots(2, 1)
    _panel(ax1, "HFA", sex, "Panjang badan menurut umur (PB/U)", "Panjang badan (cm)", h_pts)
    _panel(ax2, "WFA", sex, "Berat badan menurut umur (BB/U)", "Berat badan (kg)", w_pts)

    jk = "Laki-laki" if sex == "L" else "Perempuan"
    fig.suptitle(f"{label}  ({jk})", fontsize=13, fontweight="bold", y=0.985)
    skipped = len(points) - len(inside)
    note = f"Acuan WHO Child Growth Standards 0-24 bulan | z-score: {who_standards.method_summary().split(' (')[0]}"
    if skipped:
        note += f" | {skipped} titik di luar 0-24 bln tidak digambar"
    fig.text(0.5, 0.012, note + "\nPrototipe data simulasi, bukan diagnosis medis.",
             ha="center", fontsize=7.5, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 0.965))

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)
    buf.name = "kurva_pertumbuhan.png"
    return buf
