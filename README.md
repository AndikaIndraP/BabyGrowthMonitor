# BabyGrowth Monitor (ESP32 + MicroPython + MQTT + Telegram Bot)

Prototipe pemantauan pertumbuhan bayi/balita untuk proyek kuliah. **Berat** diukur dengan **load cell + HX711**
dan **panjang/tinggi** dengan **sensor ultrasonik HC-SR04**, keduanya **disimulasikan** di Wokwi. Semua interaksi pengguna lewat **Telegram Bot** (tanpa web dashboard).

> ⚠️ **Bukan alat diagnosis medis.** Data berat dan tinggi berasal dari simulasi (load cell + ultrasonik di Wokwi), bukan alat
> ukur sungguhan. Bot membandingkan data dengan acuan **WHO Child Growth Standards (0-24 bulan)** sebagai
> *pemeriksaan awal*: **perlu diperiksa** atau tidak. Ini bukan diagnosis stunting; diagnosis oleh tenaga kesehatan.

## Arsitektur

```
Wokwi: Load cell+HX711 + HC-SR04 + Tombol + LCD → ESP32 (main.py)
        │  MQTT (broker.hivemq.com:1883)
        ▼
Komputer: bot.py = MQTT client + SQLite (babygrowth.db) + Telegram Bot
        ▼
Pengguna lewat Telegram
```

| File | Dijalankan di | Fungsi |
|---|---|---|
| `firmware/main.py` | Wokwi (ESP32) | Baca HX711/load cell (berat, DT=16, SCK=17) dan HC-SR04 (panjang, TRIG=5, ECHO=18), tampil di LCD, kirim MQTT saat tombol ditekan, tare dengan tahan tombol, heartbeat |
| `firmware/hx711.py` | Wokwi (ESP32) | Driver HX711 (tanpa library luar) |
| `firmware/i2c_lcd.py` | Wokwi (ESP32) | Driver LCD I2C |
| `firmware/diagram.json` | Wokwi | Rangkaian |
| `bot.py` | Komputer/server | Telegram Bot, MQTT client, SQLite, pengingat, pemeriksaan WHO, **peringatan otomatis** |
| `who_standards.py` | Komputer/server | Mesin z-score WHO (**LMS penuh** bila `who_lms/` terisi, interpolasi bila kosong). **Harus satu folder dengan `bot.py`** |
| `who_lms/` | Komputer/server | Tabel LMS resmi WHO (4 file CSV, lihat `who_lms/PETUNJUK.txt`) |
| `run.bat` | Komputer (Windows) | Satu klik: siapkan Python env, pasang dependency, jalankan bot |
| `requirements.txt` | Komputer | Dependency backend |
| `dashboard.py` + `requirements-dashboard.txt` | Komputer | (Opsional) dashboard web Streamlit, baca `babygrowth.db` milik bot dan data MQTT. Jalankan: `pip install -r requirements-dashboard.txt` lalu `streamlit run dashboard.py` |
| `.env` | Komputer | Dibuat otomatis (token + user ID). `.env.example` adalah contoh opsi lanjutan |

`babygrowth.db` dibuat otomatis saat `bot.py` pertama dijalankan.

## 1. Buat bot Telegram (sekali saja)

Di Telegram, chat ke **@BotFather** → kirim `/newbot` → ikuti instruksi → **salin token**.
(Jangan bagikan token ke siapa pun. Jika bocor, kirim `/revoke` ke @BotFather.)

## 2. Jalankan backend di komputer (Windows)

Butuh Python 3.9+ ([python.org](https://www.python.org/downloads/), centang *Add Python to PATH*).

1. Ekstrak folder proyek, lalu **klik dua kali `run.bat`**.
   Pertama kali, `run.bat` membuat lingkungan Python dan memasang dependency otomatis.
2. Saat diminta, **tempel token** dari BotFather lalu tekan Enter. Token disimpan sendiri ke file `.env`.
3. Terminal menampilkan **KODE pairing** 6 digit. Buka bot Anda di Telegram, tekan **Start**, lalu **kirim kode itu**.
   Akun Anda otomatis terdaftar sebagai satu-satunya pengguna yang diizinkan (tersimpan di `.env`).
4. Kirim `/menu`. Selanjutnya cukup klik `run.bat` lagi, token dan akun tidak perlu diisi ulang.

Log yang diharapkan: `Database siap`, `MQTT terhubung, subscribe: ...`, `Bot berjalan`.
Biarkan jendela terminal tetap terbuka selama demo.

<details>
<summary>Mac/Linux atau cara manual</summary>

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```
Opsional: salin `.env.example` menjadi `.env` untuk mengubah broker, topic, zona waktu, dll.
</details>

## 3. Siapkan Wokwi (ESP32)

1. wokwi.com → New Project → ESP32 → MicroPython.
2. Salin isi `firmware/diagram.json` dan `firmware/main.py`, lalu buat dua file baru di Wokwi: `i2c_lcd.py` dan `hx711.py` (isi dari folder `firmware/`).
3. `TOPIC_BASE` di `main.py` sudah `babygrowth/babygrowth123/device01` dan cocok dengan default bot (tidak perlu diubah).
4. Klik **Start Simulation**. Serial Monitor harus menampilkan `MQTT terhubung`.
5. **Kalibrasi load cell (sekali).** Di `main.py`, `HX_SCALE = None` membuat firmware **tidak mengirim data** dan LCD
   menampilkan `KALIBRASI raw=...`. Biarkan timbangan kosong saat start (otomatis tare), lalu geser slider berat load cell ke beban
   acuan (mis. 10 kg), catat nilai `raw`, dan isi `HX_SCALE = raw / 10`. Simpan, jalankan ulang. Tanpa kalibrasi, berat tidak bermakna.
6. **Panjang badan:** atur slider jarak HC-SR04 (mis. 68 cm). Rancangan fisiknya: sensor di papan kepala menghadap papan kaki yang
   digeser menyentuh telapak kaki bayi, sehingga panjang = jarak (+ `HEIGHT_OFFSET_CM`). Di luar 30-130 cm dianggap tidak terbaca.
7. **Tombol hijau:** tekan singkat = kirim; tahan ≥ 1,5 detik = **tare**. Alas kosong (< 0,5 kg) tidak dikirim.

> Perangkat fisik: ECHO HC-SR04 berlogika 5 V, ESP32 hanya toleran 3,3 V. Pakai pembagi tegangan (mis. 1 kΩ + 2 kΩ) pada pin ECHO.
> Load cell 20-50 kg dengan rangka 4 titik memerlukan kalibrasi dengan beban acuan sungguhan; bayi yang bergerak membuat pembacaan berfluktuasi.

### Pengaturan broker, port, Wi-Fi, kredensial

| Pengaturan | ESP32 (`main.py`) | Bot (`.env`) |
|---|---|---|
| Wi-Fi | `WIFI_SSID` / `WIFI_PASSWORD` (Wokwi: `Wokwi-GUEST`, tanpa password) | - |
| Broker | `MQTT_BROKER` | `MQTT_BROKER` |
| Port | `MQTT_PORT` (1883) | `MQTT_PORT` (1883) |
| Topic | `TOPIC_BASE` | `MQTT_TOPIC_BASE` |
| Username/password broker | tidak dipakai (broker publik) | `MQTT_USERNAME`, `MQTT_PASSWORD` |

Token Telegram dan password **hanya** di `.env` (dibuat otomatis). Jangan menaruhnya di kode atau repositori publik
(tambahkan `.env` dan `babygrowth.db` ke `.gitignore`).

## 4. Cara memakai (menu Telegram)

Kirim `/menu` ke bot Anda.

- **👶 Data Anak**: tambah anak (nama/kode, tanggal lahir, jenis kelamin), lihat data berdasarkan ID (A001, A002, ...).
- **⚖️ Catat Pengukuran**: pilih anak → bot menampilkan berat **dan tinggi** terbaru dari ESP32 → cukup tekan "Kaitkan" (atau "Buang") → hasil WHO langsung tampil. **Jika di bawah -2 SD, bot langsung mengirim 🚨 peringatan otomatis** (teks) ke semua akun terdaftar, tanpa perlu membuka menu. Tombol "Koreksi tinggi" untuk input manual bila perlu (ditandai manual).
- **📈 Tren Pertumbuhan**: riwayat berat per tanggal, perubahan vs pengukuran sebelumnya, tinggi (jika ada).
- **🔔 Pengingat**: tambah/ubah/batalkan jadwal (sekali, harian, mingguan). Aktif hanya saat `bot.py` berjalan.
- **🚩 Perlu Ditinjau**: daftar penanda beserta alasan; tombol "Diperiksa" untuk menandai sudah diverifikasi petugas.
- **📋 Ringkasan** dan **📡 Status Perangkat**: gambaran data dan kondisi ESP32/MQTT.
- **❓ Bantuan**.

### Pemeriksaan awal acuan WHO (0-24 bulan)

| Indikator | Kondisi | Hasil |
|---|---|---|
| Tinggi/panjang menurut umur | < -2 SD | Pendek, **indikasi stunting**, perlu diperiksa |
| Tinggi/panjang menurut umur | < -3 SD | Sangat pendek, perlu diperiksa |
| Berat menurut umur | < -2 SD / < -3 SD | Berat kurang / sangat kurang, perlu diperiksa |

Bot menampilkan batas -2 SD (cm/kg) untuk usia dan jenis kelamin anak tersebut. **Metode z-score tertulis di setiap hasil:**
bila 4 file CSV di `who_lms/` terisi (lihat `who_lms/PETUNJUK.txt`) bot memakai rumus **LMS penuh** WHO; bila belum, bot memakai
**interpolasi** (perkiraan dari titik -3 SD, -2 SD, median). Di atas 24 bulan pemeriksaan WHO tidak dilakukan (tabel belum dimuat). Hasil di bawah -2 SD otomatis masuk **🚩 Perlu Ditinjau**.
Tabel cadangan di `who_standards.py` diketik manual dan belum terverifikasi; untuk presentasi/laporan, isi `who_lms/` dengan tabel resmi WHO.

### Aturan penanda "Perlu Ditinjau" (teknis)

| Penanda | Aturan |
|---|---|
| Belum dikaitkan ke anak | Pengukuran masuk dari MQTT tetapi belum dikonfirmasi |
| Tinggi belum diisi | Pengukuran sudah dikaitkan tetapi tinggi kosong |
| Di bawah -2 SD WHO | Tinggi atau berat di bawah batas -2 SD untuk usia & jenis kelamin |
| Waktu perangkat tidak tersedia | NTP gagal / timestamp tidak wajar |
| Di luar rentang simulasi | Berat < 2 kg atau > 20 kg |
| Perubahan tidak wajar | Selisih ≥ 2 kg dalam ≤ 7 hari, atau turun ≥ 10% dari pengukuran sebelumnya |

Ambang ini hanya untuk memeriksa ulang pengukuran, bukan batas medis.

## 5. Pengujian end-to-end (urutan demo)

1. Jalankan `python bot.py` (terminal tetap terbuka).
2. Jalankan simulasi Wokwi sampai `MQTT terhubung`.
3. Di Telegram: `/menu` → **📡 Status Perangkat** → ESP32 harus 🟢 ONLINE (maks ±10 detik setelah start).
4. **👶 Data Anak → ➕ Tambah Anak**: nama `Bayi A`, tanggal lahir `2026-03-15`, jenis kelamin → ID `A001`.
5. Di Wokwi: geser slider load cell (mis. ±8 kg) dan jarak HC-SR04 (mis. ±68 cm), lihat `B:.. T:..` di LCD, tekan **tombol hijau** → LCD "Terkirim #1".
6. Di Telegram: **⚖️ Catat Pengukuran** → pilih `A001` → berat + tinggi muncul → **✅ Ya, kaitkan** → hasil WHO tampil (perlu diperiksa / tidak).
7. Ulangi langkah 5-6 dua-tiga kali dengan berat berbeda (selisih kecil dulu, mis. 8.2 → 8.5 → 8.9 kg).
8. **📈 Tren Pertumbuhan** → `A001`: riwayat, selisih, dan ringkasan tampil.
9. Uji WHO: atur jarak HC-SR04 ke nilai rendah (mis. 60 cm untuk usia ±8 bulan) lalu kaitkan → muncul "PERLU DIPERIKSA" **dan pesan 🚨 PERINGATAN masuk otomatis**. Uji penanda lain: kirim berat yang berubah drastis (mis. 8.9 → 14 kg) lalu kaitkan → muncul di **🚩 Perlu Ditinjau** beserta alasan. Tekan **✅ Diperiksa** → hilang dari daftar.
10. **🔔 Pengingat → ➕ Tambah**: pilih jam 2 menit dari sekarang, "Sekali" → pengingat masuk sesuai jadwal dan tidak terkirim ulang.
11. **📋 Ringkasan** → jumlah anak, pengukuran, terbaru, jadwal, dan hasil perlu ditinjau.
12. Stop simulasi Wokwi, tunggu ±45-60 detik → **📡 Status Perangkat** berubah 🔴 OFFLINE.
13. Uji keamanan: dari akun Telegram lain, bot harus menolak ("Akses ditolak").

## Pemecahan masalah

| Gejala | Penyebab / solusi |
|---|---|
| Terminal meminta token lagi | Token di `.env` kosong/salah format; tempel ulang token dari BotFather |
| Bot membalas "kirim kode pairing" | Kirim kode 6 digit yang tampil di terminal (kode berganti jika salah 5 kali) |
| Bot menjawab "Akses ditolak" ke Anda | Akun belum terdaftar. Hapus baris `ALLOWED_USER_IDS=` di `.env`, jalankan ulang, lalu kirim kode pairing baru |
| Status ESP32 selalu OFFLINE | Simulasi belum jalan, atau `TOPIC_BASE` ≠ `MQTT_TOPIC_BASE` |
| "Belum ada data berat baru" | Tombol hijau belum ditekan, atau data sudah pernah dikaitkan/dibuang |
| Pesan MQTT tidak masuk di bot | Cek koneksi internet, port 1883 tidak diblokir jaringan kampus (coba hotspot) |
| LCD "KALIBRASI", data tidak terkirim | `HX_SCALE` masih `None`; kalibrasi (langkah 3.5) |
| Berat selalu 0 / "Alas kosong" | Beban < 0,5 kg, atau tare terjadi saat ada beban; tahan tombol 1,5 detik saat alas kosong |
| LCD `T:  --` | HC-SR04 tidak terbaca atau jarak di luar 30-130 cm; cek kabel TRIG/ECHO |
| "HX711 error" | Kabel DT/SCK/VCC salah, atau `hx711.py` belum dibuat di Wokwi |
| Z-score ditulis "interpolasi" | Folder `who_lms/` belum berisi 4 CSV WHO yang valid (lihat log awal bot) |
| Tab Wokwi di latar belakang → ESP32 sering offline | Browser memperlambat tab tidak aktif; buka Wokwi di jendela terpisah |

## Keterbatasan

- Broker publik (HiveMQ) **tidak privat**. Gunakan hanya data dummy dan topic yang unik.
- Pengingat dan penerimaan data hanya berjalan saat `bot.py` hidup; data yang dikirim saat bot mati tidak tersimpan.
- Wokwi gratis dapat menghentikan simulasi yang terlalu lama; mulai simulasi sebelum presentasi.
- Load cell dan ultrasonik hanya disimulasikan di Wokwi; tidak ada klaim akurasi alat fisik.
- Peringatan otomatis hanya terkirim saat pengukuran dikaitkan, dan hanya selama `bot.py` berjalan.
- Pemeriksaan WHO hanya untuk usia 0-24 bulan dan hanya penapisan awal, bukan diagnosis stunting.
