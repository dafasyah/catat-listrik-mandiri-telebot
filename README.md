# Catat Mandiri Telegram Bot

Bot Telegram untuk catat nomor meter listrik mandiri, plus ringkasan, grafik, dan deteksi lonjakan anomali.

## Fitur
- Catat pembacaan meter: `/catat <angka> [dd-mm-yyyy [h:i]]`
- Riwayat 10 terakhir: `/riwayat`
- Total pemakaian: `/total`
- Ringkasan bulan: `/summary [mm-yyyy]`
- Rekap mingguan: `/mingguan`
- Rekap bulanan: `/bulanan`
- Grafik tren: `/grafik [mm-yyyy]`
- Deteksi anomali lonjakan: `/cek_anomali`
- Reminder harian: `/reminder <jam>` atau `/reminder off`
- Estimasi konsumsi peralatan rumah: `/estimasi [jam/hari]`
- Simpan & bandingkan tagihan bulanan: `/tagihan <mm-yyyy> <jumlah> [stand_awal-stand_akhir]`, `/bandingkan`
- Manajemen daftar peralatan: `/barang_tambah`, `/barang_hapus`, `/barang_list`
- Pencarian fleksibel: `/cari <dd-mm-yyyy|mm-yyyy|yyyy>`
- Tarif PLN 2026 + personal golongan: `/tarif` dan `/golongan <kode>`
- AI khusus listrik dengan guardrails: `/ai <pertanyaan>`
- Kirim angka saja untuk catat cepat

Data disimpan lokal di SQLite (`meter.db`), per-user Telegram.

## Disklaimer
Bot ini tidak afiliasi dengan PT PLN (Persero). Data tarif yang digunakan di dalam bot mengacu pada tarif listrik PLN Triwulan II 2026 yang bersumber dari pengumuman resmi. Untuk informasi resmi dan mutakhir, silakan merujuk ke situs PLN atau Kementerian Energi dan Sumber Daya Mineral (ESDM).

**Catatan estimasi biaya:** Estimasi tagihan di bot dihitung dari tarif dasar + PPN 10%. Tagihan resmi mungkin ada biaya admin/pemeliharaan kecil lain, jadi angka bot bisa sedikit lebih rendah dari tagihan asli.

## 1. Clone repo ini

```bash
git clone https://github.com/dafasyah/catat-listrik-mandiri-telebot.git
cd catat-listrik-mandiri-telebot
```

## 2. Copy file env dan isi kunci

```bash
cp .env.example .env
```

Isi file `.env` dengan value kamu sendiri:

- `BOT_TOKEN` — dapat dari [@BotFather](https://t.me/BotFather) Telegram
- `OPENROUTER_API_KEY` — opsional, untuk fitur AI. Jika tidak diisi, fitur AI nonaktif
- `AI_MODEL` — opsional, default: `openrouter/free`

Contoh `.env`:

```env
BOT_TOKEN=123456:ABC-DEF...
OPENROUTER_API_KEY=sk-or-...
AI_MODEL=openrouter/free
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

Kebutuhan: Python 3.9+.

## 4. Jalankan bot

```bash
python bot.py
```

Buka Telegram, cari username bot kamu, kirim `/start`.

## 5. Deploy ke VPS (opsional)

Untuk menjalankan 24 jam tanpa laptop menyala, deploy ke VPS gratisan seperti Render.com, Fly.io, atau Railway:
- Upload repo GitHub ini
- Set environment variables sesuai `.env`
- Start command: `python bot.py`
- File `meter.db` akan dibuat otomatis di direktori kerja

Catatan:
- Jangan upload `.env` dan `meter.db` ke GitHub (sudah diabaikan oleh `.gitignore`)
- API key aman karena disimpan di environment variables server/VPS

## Command cepat

| Command | Kegunaan |
|---|---|
| `/start` | Tampilkan menu command |
| `/help` | Bantuan command |
| `/info` | Info user dan tarif personal |
| `/catat <angka> [dd-mm-yyyy [h:i]]` | Simpan pembacaan meter |
| `/riwayat` | 10 pembacaan terakhir |
| `/total` | Perkiraan pemakaian kWh |
| `/summary [mm-yyyy]` | Ringkasan per hari dalam sebulan |
| `/mingguan` | Rekap 7 hari terakhir |
| `/bulanan` | Rekap bulan ini |
| `/grafik [mm-yyyy]` | Grafik tren pemakaian |
| `/cek_anomali` | Deteksi lonjakan meter |
| `/reminder <jam / off>` | Set reminder harian |
| `/cari <dd-mm-yyyy / mm-yyyy / yyyy>` | Cari catatan |
| `/tarif` | Daftar tarif PLN 2026 |
| `/golongan <kode>` | Atur tarif personal |
| `/ai <pertanyaan>` | Tanya AI khusus listrik |
| `/barang_tambah <nama> <watt> <volt> [qty] [jam/hari]` | Tambah peralatan rumah |
| `/barang_hapus <id>` | Hapus peralatan |
| `/barang_list` | Lihat daftar peralatan |
| `/estimasi [jam/hari]` | Estimasi konsumsi dari peralatan |
| `/tagihan <mm-yyyy> <jumlah> [stand_awal-stand_akhir]` | Simpan tagihan bulanan |
| `/bandingkan [mm-yyyy mm-yyyy]` | Bandingkan tagihan (kosong: awal vs akhir) |
| `/akurasi` | Cek akurasi estimasi bot vs tagihan asli |

## Tarif Golongan Tersedia

`subsidi_450_va`, `subsidi_900_va`, `r1_900_va`, `r1_1300_va`, `r1_2200_va`, `r2_3500_va`, `r2_5500_va`, `r3_6600_va`, `b2_6600_va`, `p1_6600_va`
