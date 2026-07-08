import os
import io
import sqlite3
import logging
import re
import requests
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter.db")
DEFAULT_AI_MODEL = os.getenv("AI_MODEL", "openrouter/free")

PLN_TARIFF_2026 = {
    "subsidi_450_va": {"daya": "450 VA", "golongan": "Subsidi", "tarif": 415},
    "subsidi_900_va": {"daya": "900 VA", "golongan": "Subsidi", "tarif": 605},
    "r1_900_va": {"daya": "900 VA", "golongan": "R-1/TR (RTM)", "tarif": 1352},
    "r1_1300_va": {"daya": "1.300 VA", "golongan": "R-1/TR", "tarif": 1444.70},
    "r1_2200_va": {"daya": "2.200 VA", "golongan": "R-1/TR", "tarif": 1444.70},
    "r2_3500_va": {"daya": "3.500 VA", "golongan": "R-2/TR", "tarif": 1699.53},
    "r2_5500_va": {"daya": "5.500 VA", "golongan": "R-2/TR", "tarif": 1699.53},
    "r3_6600_va": {"daya": "6.600 VA", "golongan": "R-3/TR", "tarif": 1699.53},
    "b2_6600_va": {"daya": "6.600 VA", "golongan": "B-2/TR (Bisnis)", "tarif": 1444.70},
    "p1_6600_va": {"daya": "6.600 VA", "golongan": "P-1/TR (Pemerintah)", "tarif": 1699.53},
}
PPN_RATE = 0.10

if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable belum di-set!")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS readings (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (user_id, date)
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            tariff TEXT,
            reminder_time TEXT,
            reminder_enabled INTEGER DEFAULT 0,
            last_anomaly_alert_date TEXT,
            default_voltage REAL
        );
        CREATE TABLE IF NOT EXISTS appliances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            watt REAL NOT NULL,
            volt REAL NOT NULL,
            qty INTEGER DEFAULT 1,
            hours_per_day REAL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_user_readings(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT date, value FROM readings WHERE user_id = ? ORDER BY date ASC", (user_id,))
    rows = [{"date": r["date"], "value": r["value"]} for r in cur.fetchall()]
    conn.close()
    return rows


def save_user_reading(user_id: int, value: float, tanggal: str | None = None):
    target_date = tanggal or date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO readings (user_id, date, value)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET value=excluded.value
        """,
        (user_id, target_date, value),
    )
    conn.commit()
    conn.close()


def get_user_tariff(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT tariff FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["tariff"] if row else None


def set_user_tariff(user_id: int, tariff_id: str):
    if tariff_id not in PLN_TARIFF_2026:
        return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, tariff)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET tariff=excluded.tariff
        """,
        (user_id, tariff_id),
    )
    conn.commit()
    conn.close()
    return True


def get_user_reminder(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT reminder_time, reminder_enabled FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row["reminder_enabled"]:
        return None
    return row["reminder_time"]


def set_user_reminder(user_id: int, time_str: str | None):
    conn = get_conn()
    cur = conn.cursor()
    if time_str is None:
        cur.execute(
            "UPDATE users SET reminder_enabled=0, reminder_time=NULL WHERE user_id=?",
            (user_id,),
        )
    else:
        cur.execute(
            """
            INSERT INTO users (user_id, reminder_time, reminder_enabled)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET reminder_time=excluded.reminder_time, reminder_enabled=1
            """,
            (user_id, time_str),
        )
    conn.commit()
    conn.close()


def get_last_anomaly_alert_date(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_anomaly_alert_date FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["last_anomaly_alert_date"] if row else None


def set_last_anomaly_alert_date(user_id: int, alert_date: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_anomaly_alert_date=? WHERE user_id=?",
        (alert_date, user_id),
    )
    conn.commit()
    conn.close()


def calculate_est_cost(kwh: float, tariff_id: str | None = None) -> tuple[float, float, float]:
    if not tariff_id or tariff_id not in PLN_TARIFF_2026:
        return kwh, None, None
    base_tariff = PLN_TARIFF_2026[tariff_id]["tarif"]
    subtotal = kwh * base_tariff
    ppn = subtotal * PPN_RATE
    total = subtotal + ppn
    return round(total), round(base_tariff, 2), round(ppn, 2)


def add_appliance(user_id: int, name: str, watt: float, volt: float, qty: int, hours_per_day: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO appliances (user_id, name, watt, volt, qty, hours_per_day)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, float(watt), float(volt), int(qty), float(hours_per_day)),
    )
    conn.commit()
    conn.close()


def remove_appliance(user_id: int, appliance_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM appliances WHERE id=? AND user_id=?", (appliance_id, user_id))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


def list_appliances(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, watt, volt, qty, hours_per_day FROM appliances WHERE user_id=? ORDER BY id ASC",
        (user_id,),
    )
    rows = [
        {
            "id": r["id"],
            "name": r["name"],
            "watt": r["watt"],
            "volt": r["volt"],
            "qty": r["qty"],
            "hours_per_day": r["hours_per_day"],
        }
        for r in cur.fetchall()
    ]
    conn.close()
    return rows


def estimate_daily_kwh(user_id: int, hours_per_day_override: float | None = None):
    rows = list_appliances(user_id)
    total = 0.0
    for r in rows:
        h = hours_per_day_override if hours_per_day_override is not None else r["hours_per_day"]
        total += r["watt"] * r["qty"] * h / 1000
    return round(total, 2), rows



def parse_summary_query(args):
    if not args:
        now = date.today()
        return f"{now.year:04d}-{now.month:02d}"
    text = args[0].strip()
    if re.fullmatch(r"\d{2}-\d{4}", text):
        m, y = text.split("-")
        return f"{y}-{int(m):02d}"
    if re.fullmatch(r"\d{4}", text):
        return text
    return None


def parse_custom_date_time(value: str) -> str | None:
    m = re.fullmatch(r"(\d{2}-\d{2}-\d{4})(?:\s+(\d{1,2})(?::(\d{2}))?)?", value)
    if not m:
        return None
    tanggal_str = m.group(1)
    jam_str = m.group(2)
    menit_str = m.group(3) or "00"
    try:
        if jam_str is not None:
            dt = datetime.strptime(f"{tanggal_str} {int(jam_str)}:{menit_str}", "%d-%m-%Y %H:%M")
            return dt.date().isoformat()
        dt = datetime.strptime(tanggal_str, "%d-%m-%Y")
        return dt.date().isoformat()
    except ValueError:
        return None


def _electricity_related(text: str) -> bool:
    keywords = [
        "listrik", "kwh", "meter", "pln", "tarif", "daya", "hemat",
        "tagihan", "konsumsi", "golongan", "subsidi", "va", "watt",
        "kabel", "lampu", "pembangkit", "transmisi", "distribusi",
        "pembacaan", "angka", "meteran", "sambungan", "token",
        "prepaid", "postpaid", "tegangan", "arus", "booster",
        "ehp", "estimasi", "biaya", "ril", "pulsa", "sumber",
        "beban", "puncak", "hemat"
    ]
    lowered = text.lower()
    return any(k in lowered for k in keywords)


def _ask_openrouter(prompt: str) -> str:
    if not OPENROUTER_KEY:
        return "Fitur AI belum aktif: OPENROUTER_API_KEY belum di-set."
    system_prompt = (
        "Kamu adalah asisten bot catat meter listrik. "
        "Jawab HANYA topik terkait: listrik, kWh, meter, tarif PLN, hemat listrik, "
        "cara baca meter, tagihan listrik, dan konsumsi listrik. "
        "Jika pertanyaan di luar topik, jawab singkat: 'Maaf, saya cuma bisa bantu soal listrik.' "
        "Jawab singkat, jelas, dan praktis."
    )
    payload = {
        "model": DEFAULT_AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content.strip()
            return "AI tidak memberikan jawaban."
        if resp.status_code == 404:
            return "Gagal memanggil AI: model tidak ditemukan (404). Cek pengaturan AI_MODEL."
        return f"Gagal memanggil AI (HTTP {resp.status_code})."
    except Exception as e:
        return f"Error memanggil AI: {e}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Halo! Bot catat meter listrik.\n\n"
        "Command:\n"
        "/catat <angka> [dd-mm-yyyy [h:i]] - simpan pembacaan meter\n"
        "/riwayat - 10 pembacaan terakhir\n"
        "/total - perkiraan pemakaian kWh\n"
        "/summary [mm-yyyy] - ringkasan per hari dalam sebulan\n"
        "/mingguan - rekap 7 hari terakhir\n"
        "/bulanan - rekap bulan ini\n"
        "/grafik [mm-yyyy] - grafik tren pemakaian\n"
        "/cek_anomali - deteksi lonjakan meter\n"
        "/reminder <jam|off> - set reminder harian\n"
        "/barang_tambah <nama> <watt> <volt> [qty] [jam/hari] - tambah peralatan\n"
        "/barang_hapus <id> - hapus peralatan\n"
        "/barang_list - daftar peralatan\n"
        "/estimasi [jam/hari] - estimasi konsumsi dari daftar peralatan\n"
        "/cari <query> - cari catatan\n"
        "/tarif - info tarif PLN 2026\n"
        "/golongan <kode> - atur tarif personal\n"
        "/ai <pertanyaan> - tanya AI khusus listrik\n"
        "/help - bantuan\n"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Command yang tersedia:\n"
        "/catat <angka> [dd-mm-yyyy [h:i]]\n"
        "/catat <angka> 05-07-2026 14:30 - catat dengan tanggal & jam\n"
        "/riwayat - 10 pembacaan terakhir\n"
        "/total - lihat pemakaian\n"
        "/summary [mm-yyyy] - ringkasan per hari dalam sebulan\n"
        "/mingguan - rekap 7 hari terakhir\n"
        "/bulanan - rekap bulan ini + estimasi tagihan\n"
        "/grafik [mm-yyyy] - grafik tren pemakaian\n"
        "/cek_anomali - deteksi lonjakan meter\n"
        "/reminder <jam|off> - contoh: /reminder 20:00 atau /reminder off\n"
        "/barang_tambah <nama> <watt> <volt> [qty] [jam/hari] - tambah peralatan rumah\n"
        "/barang_hapus <id> - hapus peralatan berdasarkan id\n"
        "/barang_list - lihat daftar peralatan\n"
        "/estimasi [jam/hari] - estimasi konsumsi kWh dari peralatan yang tercatat\n"
        "/cari <dd-mm-yyyy|mm-yyyy|yyyy> - cari catatan\n"
        "/tarif - daftar tarif PLN 2026\n"
        "/golongan <kode> - atur tarif personal\n"
        "/ai <pertanyaan> - tanya AI khusus listrik\n"
        "/info - info user\n"
    )


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tariff = get_user_tariff(user.id)
    tariff_info = PLN_TARIFF_2026.get(tariff, {}).get("golongan", "Belum diatur") if tariff else "Belum diatur"
    reminder = get_user_reminder(user.id)
    reminder_info = f"Reminder: {reminder}" if reminder else "Reminder: tidak aktif"
    await update.message.reply_text(
        f"User: {user.first_name} (id: {user.id})\n"
        f"Tarif personal: {tariff_info}\n"
        f"{reminder_info}"
    )


async def catat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format salah. Contoh: /catat 1234,5 atau /catat 1234,5 05-07-2026 14:30")
        return

    raw = " ".join(context.args).replace(",", ".")
    custom_date = None
    parts = raw.rsplit(" ", 2)
    if len(parts) > 1:
        candidate = parts[-2] if len(parts) == 3 else parts[-1]
        candidate_re = re.fullmatch(r"\d{2}-\d{2}-\d{4}(?:\s+\d{1,2}(?::\d{2})?)?", candidate.replace(".", "-"))
        if candidate_re:
            custom_date = parse_custom_date_time(candidate)
            if custom_date:
                if len(parts) == 3:
                    raw = parts[0]
                else:
                    raw = raw[: raw.rfind(" ")].strip()

    if not re.fullmatch(r"\d+(\.\d+)?", raw):
        await update.message.reply_text("Angka meter tidak valid. Contoh: /catat 1234,5")
        return

    value = float(raw)
    save_user_reading(update.effective_user.id, value, custom_date)
    used_date = custom_date or date.today().isoformat()

    # Check anomaly
    entries = get_user_readings(update.effective_user.id)
    sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))
    idx = next((i for i, e in enumerate(sorted_entries) if e["date"] == used_date), None)
    if idx is not None and idx > 0:
        prev = sorted_entries[idx - 1]["value"]
        diff = round(value - prev, 2)
        if diff < 0:
            await update.message.reply_text(
                f"Suksek catat meter {value} untuk tanggal {used_date}\n"
                f"Catatan: nilai meter turun {abs(diff)} kWh dari catatan sebelumnya ({prev}). "
                f"Cek apakah pembacaan awal sudah benar."
            )
            return
        last7 = [e["value"] for e in sorted_entries[max(0, idx - 7): idx]]
        if len(last7) >= 3:
            diffs = [sorted_entries[i]["value"] - sorted_entries[i - 1]["value"] for i in range(max(1, idx - 7), idx)]
            if diffs:
                avg = sum(diffs) / len(diffs)
                if avg > 0 and diff > avg * 3:
                    today_iso = date.today().isoformat()
                    last_alert = get_last_anomaly_alert_date(update.effective_user.id)
                    if last_alert != today_iso:
                        set_last_anomaly_alert_date(update.effective_user.id, today_iso)
                        await update.message.reply_text(
                            f"Suksek catat meter {value} untuk tanggal {used_date}\n"
                            f"PERINGATAN: lonjakan {diff} kWh jauh di atas rata-rata {round(avg, 2)} kWh "
                            f"(>{round(avg * 3, 2)} kWh). Cek apakah ada peralatan tambahan atau kesalahan pembacaan."
                        )
                        return

    await update.message.reply_text(f"Suksek catat meter {value} untuk tanggal {used_date}")


async def riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_readings(user_id)
    if not entries:
        await update.message.reply_text("Belum ada catatan meter.")
        return

    lines = ["Riwayat 10 terakhir:"]
    for e in entries[-10:]:
        lines.append(f"- {e.get('date')}: {e.get('value')}")
    await update.message.reply_text("\n".join(lines))


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_readings(user_id)
    if not entries:
        await update.message.reply_text("Belum ada catatan meter.")
        return

    sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))
    if len(sorted_entries) < 2:
        await update.message.reply_text("Butuh minimal 2 catatan untuk hitung pemakaian.")
        return

    first = sorted_entries[0]
    last = sorted_entries[-1]
    usage = round(last["value"] - first["value"], 2)
    days = max((datetime.fromisoformat(last["date"]) - datetime.fromisoformat(first["date"])).days, 1)
    avg_per_day = round(usage / days, 2)
    est_monthly = round(avg_per_day * 30, 2)

    tariff_id = get_user_tariff(user_id)
    line = ""
    if tariff_id and tariff_id in PLN_TARIFF_2026:
        est_cost, base_tariff, _ = calculate_est_cost(est_monthly, tariff_id)
        line = f"Estimasi tagihan: Rp {est_cost:,} (tarif dasar Rp {base_tariff:,.2f}/kWh + PPN 10%)\n"

    text = (
        f"Periode: {first['date']} s/d {last['date']} ({days} hari)\n"
        f"Pemakaian: {usage} kWh\n"
        f"Rata-rata/hari: {avg_per_day} kWh\n"
        f"Estimasi bulanan: {est_monthly} kWh\n"
        f"{line}"
    )
    await update.message.reply_text(text)


async def cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Format pencarian:\n"
            "/cari <dd-mm-yyyy> - tanggal spesifik\n"
            "/cari <mm-yyyy> - bulan spesifik\n"
            "/cari <yyyy> - tahun spesifik"
        )
        return

    query = context.args[0].strip()
    entries = get_user_readings(update.effective_user.id)
    filtered = []

    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", query):
        try:
            dt = datetime.strptime(query, "%d-%m-%Y").date().isoformat()
        except ValueError:
            dt = None
        if dt:
            filtered = [e for e in entries if e.get("date") == dt]
    elif re.fullmatch(r"\d{2}-\d{4}", query):
        m, y = query.split("-")
        prefix = f"{y}-{int(m):02d}-"
        filtered = [e for e in entries if e.get("date", "").startswith(prefix)]
    elif re.fullmatch(r"\d{4}", query):
        filtered = [e for e in entries if e.get("date", "").startswith(f"{query}-")]
    else:
        await update.message.reply_text("Format tidak dikenali. Pakai dd-mm-yyyy, mm-yyyy, atau yyyy.")
        return

    if not filtered:
        await update.message.reply_text("Tidak ada catatan ditemukan.")
        return

    lines = [f'Hasil pencarian "{query}" ({len(filtered)} catatan):']
    for e in filtered:
        lines.append(f"- {e.get('date')}: {e.get('value')}")
    await update.message.reply_text("\n".join(lines))


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_readings(user_id)
    if not entries:
        await update.message.reply_text("Belum ada catatan meter.")
        return

    target = parse_summary_query(context.args)
    if not target:
        await update.message.reply_text(
            "Format salah.\n"
            "/summary - bulan ini\n"
            "/summary mm-yyyy - contoh: /summary 07-2026"
        )
        return

    filtered = [e for e in entries if e.get("date", "").startswith(f"{target}-")]
    if not filtered:
        await update.message.reply_text("Tidak ada catatan untuk periode tersebut.")
        return

    filtered_sorted = sorted(filtered, key=lambda x: x.get("date", ""))

    day_map = {}
    for e in filtered_sorted:
        d = e.get("date", "")
        if d:
            day_map[d] = e.get("value")

    days = sorted(day_map.items())
    if len(days) < 2:
        await update.message.reply_text("Butuh minimal 2 hari berbeda untuk hitung ringkasan bulan.")
        return

    rows = []
    prev_value = days[0][1]
    for d, value in days[1:]:
        usage = round(value - prev_value, 2)
        rows.append((d[8:10], d[:7], usage))
        prev_value = value

    lines = [f"Ringkasan bulan: {target} ({len(rows)} hari)"]
    total = 0
    for day, month, usage in rows:
        lines.append(f"- {day} {month}: {usage} kWh")
        total += usage

    avg = round(total / len(rows), 2)
    tariff_id = get_user_tariff(user_id)
    if tariff_id and tariff_id in PLN_TARIFF_2026:
        est_cost, base_tariff, _ = calculate_est_cost(total, tariff_id)
        lines.append(f"\nTotal: {round(total, 2)} kWh")
        lines.append(f"Rata-rata/hari: {avg} kWh")
        lines.append(f"Estimasi tagihan: Rp {est_cost:,} (tarif dasar Rp {base_tariff:,.2f}/kWh + PPN 10%)")
    else:
        lines.append(f"\nTotal: {round(total, 2)} kWh")
        lines.append(f"Rata-rata/hari: {avg} kWh")
        lines.append("Set /golongan untuk estimasi tagihan.")

    await update.message.reply_text("\n".join(lines))


async def _rekap_entries(entries, days: int, title: str):
    if not entries:
        return "Belum ada catatan meter."

    sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))
    subset = sorted_entries[-days:]
    if len(subset) < 2:
        return "Butuh minimal 2 catatan untuk hitung rekap."

    first = subset[0]
    last = subset[-1]
    usage = round(last["value"] - first["value"], 2)
    ddays = max((datetime.fromisoformat(last["date"]) - datetime.fromisoformat(first["date"])).days, 1)
    avg = round(usage / ddays, 2)

    tariff_id = None
    # Cannot get user_id from entries here, so we skip tariff unless passed
    line = ""
    return (
        f"{title}\n"
        f"Periode: {first['date']} s/d {last['date']} ({ddays} hari)\n"
        f"Pemakaian: {usage} kWh\n"
        f"Rata-rata/hari: {avg} kWh\n"
        f"{line}"
    )


async def mingguan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = get_user_readings(update.effective_user.id)
    text = await _rekap_entries(entries, 7, "Rekap 7 hari terakhir:")
    if text.startswith("Belum") or text.startswith("Butuh"):
        await update.message.reply_text(text)
        return

    tariff_id = get_user_tariff(update.effective_user.id)
    if tariff_id and tariff_id in PLN_TARIFF_2026:
        lines = text.split("\n")
        usage = float(lines[2].split(": ")[1].split(" ")[0])
        est_cost, base_tariff, _ = calculate_est_cost(usage, tariff_id)
        text += f"Estimasi tagihan: Rp {est_cost:,} (tarif dasar Rp {base_tariff:,.2f}/kWh + PPN 10%)\n"
    await update.message.reply_text(text)


async def bulanan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = parse_summary_query([])
    entries = get_user_readings(update.effective_user.id)
    filtered = [e for e in entries if e.get("date", "").startswith(f"{target}-")]
    sorted_entries = sorted(filtered, key=lambda x: x.get("date", ""))
    if not sorted_entries or len(sorted_entries) < 2:
        await update.message.reply_text("Data belum cukup untuk rekap bulan ini.")
        return

    first = sorted_entries[0]
    last = sorted_entries[-1]
    usage = round(last["value"] - first["value"], 2)
    ddays = max((datetime.fromisoformat(last["date"]) - datetime.fromisoformat(first["date"])).days, 1)
    avg = round(usage / ddays, 2)

    tariff_id = get_user_tariff(update.effective_user.id)
    extra = ""
    if tariff_id and tariff_id in PLN_TARIFF_2026:
        est_cost, base_tariff, _ = calculate_est_cost(usage, tariff_id)
        extra = f"Estimasi tagihan: Rp {est_cost:,} (tarif dasar Rp {base_tariff:,.2f}/kWh + PPN 10%)\n"

    await update.message.reply_text(
        f"Rekap bulan: {target}\n"
        f"Pemakaian: {usage} kWh\n"
        f"Rata-rata/hari: {avg} kWh\n"
        f"{extra}"
    )


async def cek_anomali(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = get_user_readings(update.effective_user.id)
    if not entries:
        await update.message.reply_text("Belum ada catatan meter.")
        return

    sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))
    alerts = []
    for i in range(1, len(sorted_entries)):
        prev = sorted_entries[i - 1]
        curr = sorted_entries[i]
        diff = round(curr["value"] - prev["value"], 2)
        if diff < 0:
            alerts.append(f"- {curr['date']}: meter turun {abs(diff)} kWh dari {prev['value']} ke {curr['value']}")
            continue
        window = [sorted_entries[j]["value"] - sorted_entries[j - 1]["value"] for j in range(max(1, i - 7), i)]
        diffs = [sorted_entries[j]["value"] - sorted_entries[j - 1]["value"] for j in range(max(1, i - 7), i)]
        if diffs:
            avg = sum(diffs) / len(diffs)
            if avg > 0 and diff > avg * 3:
                alerts.append(
                    f"- {curr['date']}: lonjakan {diff} kWh (>{round(avg * 3, 2)} kWh, rata-rata {round(avg, 2)})"
                )

    if not alerts:
        await update.message.reply_text("Tidak ada anomali lonjakan terdeteksi.")
    else:
        lines = [f"Anomali terdeteksi ({len(alerts)}):"]
        lines.extend(alerts)
        await update.message.reply_text("\n".join(lines))


async def reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or context.args[0].strip().lower() in ("off", "0", "no"):
        set_user_reminder(user_id, None)
        await update.message.reply_text("Reminder dimatikan.")
        return

    raw = context.args[0].strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", raw):
        await update.message.reply_text("Format salah. Contoh: /reminder 20:00 atau /reminder off")
        return

    set_user_reminder(user_id, raw)
    await update.message.reply_text(f"Reminder di-set jam {raw} setiap hari.")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.chat_id
    await context.bot.send_message(
        chat_id=user_id,
        text="Pengingat: jangan lupa catat meter listrik hari ini ⚡",
    )


async def set_reminder_job(app: Application, user_id: int, time_str: str):
    existing = app.job_queue.get_jobs_by_name(f"reminder_{user_id}")
    for j in existing:
        j.schedule_removal()

    h, m = map(int, time_str.split(":"))
    wib = ZoneInfo("Asia/Jakarta")
    app.job_queue.run_daily(
        reminder_job,
        time=dtime(hour=h, minute=m, tzinfo=wib),
        chat_id=user_id,
        name=f"reminder_{user_id}",
    )


async def grafik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_readings(user_id)
    if not entries:
        await update.message.reply_text("Belum ada catatan meter.")
        return

    target = parse_summary_query(context.args) or (date.today().strftime("%Y-%m"))
    filtered = [e for e in entries if e.get("date", "").startswith(f"{target}-")]
    if len(filtered) < 2:
        await update.message.reply_text("Data belum cukup untuk buat grafik.")
        return

    day_map = {}
    for e in filtered:
        d = e.get("date", "")
        if d:
            day_map[d] = e.get("value")
    days = sorted(day_map.items())
    xs = [datetime.fromisoformat(d) for d, _ in days]
    ys = [v for _, v in days]

    plt.figure(figsize=(8, 4))
    plt.plot(xs, ys, marker="o")
    plt.title(f"Tren Meter {target}")
    plt.xlabel("Tanggal")
    plt.ylabel("Nilai Meter")
    plt.grid(True)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close()
    buf.seek(0)

    await update.message.reply_photo(photo=InputFile(buf, filename="grafik.png"), caption=f"Grafik tren {target}")


async def tarif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Tarif listrik PLN 2026 ( Triwulan II ) per kWh:\n"]
    for k, v in PLN_TARIFF_2026.items():
        lines.append(f"- {v['golongan']} {v['daya']}: Rp {v['tarif']:,.2f}")
    lines.append("\nGunakan /golongan <kode> untuk set tarif personal.")
    lines.append("Contoh: /golongan r1_900_va")
    await update.message.reply_text("\n".join(lines))


async def golongan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Pilih kode golongan:\n"
            "subsidi_450_va\n"
            "subsidi_900_va\n"
            "r1_900_va\n"
            "r1_1300_va\n"
            "r1_2200_va\n"
            "r2_3500_va\n"
            "r2_5500_va\n"
            "r3_6600_va\n"
            "b2_6600_va\n"
            "p1_6600_va\n\n"
            "Contoh: /golongan r1_900_va"
        )
        return

    kode = context.args[0].strip().lower()
    if kode not in PLN_TARIFF_2026:
        await update.message.reply_text("Kode tidak valid. Gunakan /golongan untuk lihat daftar.")
        return

    user_id = update.effective_user.id
    ok = set_user_tariff(user_id, kode)
    if ok:
        info = PLN_TARIFF_2026[kode]
        await update.message.reply_text(
            f"Tarif personal diset:\n"
            f"Golongan: {info['golongan']}\n"
            f"Daya: {info['daya']}\n"
            f"Tarif: Rp {info['tarif']:,.2f}/kWh"
        )
    else:
        await update.message.reply_text("Gagal menyimpan tarif.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    cleaned = text.replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", cleaned):
        value = float(cleaned)
        save_user_reading(update.effective_user.id, value)
        await update.message.reply_text(f"Suksek catat meter {value} untuk tanggal {date.today().isoformat()}")
        return

    await update.message.reply_text(
        "Untuk catat meter, kirim angka saja atau pake /catat <angka>.\n"
        "Gunakan /help untuk lihat command lain."
    )


async def barang_tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 3:
        await update.message.reply_text(
            "Format: /barang_tambah <nama> <watt> <volt> [qty] [jam/hari]\n"
            "Contoh: /barang_tambah kulkas 150 220 1 24"
        )
        return

    name = context.args[0]
    watt_raw = context.args[1].replace(",", ".")
    volt_raw = context.args[2].replace(",", ".")
    qty_raw = context.args[3] if len(context.args) > 3 else "1"
    hours_raw = context.args[4] if len(context.args) > 4 else "1"

    if not re.fullmatch(r"\d+(\.\d+)?", watt_raw):
        await update.message.reply_text("Watt harus angka. Contoh: /barang_tambah kulkas 150 220")
        return
    if not re.fullmatch(r"\d+(\.\d+)?", volt_raw):
        await update.message.reply_text("Volt harus angka. Contoh: /barang_tambah kulkas 150 220")
        return
    if not re.fullmatch(r"\d+", qty_raw):
        await update.message.reply_text("Qty harus angka bulat. Contoh: /barang_tambah lampu 10 220 2 8")
        return
    if not re.fullmatch(r"\d+(\.\d+)?", hours_raw):
        await update.message.reply_text("Jam/hari harus angka. Contoh: /barang_tambah lampu 10 220 2 8")
        return

    add_appliance(user_id, name, float(watt_raw), float(volt_raw), int(qty_raw), float(hours_raw))
    await update.message.reply_text(
        f"Peralatan disimpan: {name}\n"
        f"Watt: {watt_raw} W\n"
        f"Volt: {volt_raw} V\n"
        f"Qty: {qty_raw}\n"
        f"Jam/hari: {hours_raw}"
    )


async def barang_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not re.fullmatch(r"\d+", context.args[0]):
        await update.message.reply_text("Format: /barang_hapus <id>\nGunakan /barang_list untuk lihat id.")
        return

    appliance_id = int(context.args[0])
    if remove_appliance(user_id, appliance_id):
        await update.message.reply_text(f"Peralatan id {appliance_id} dihapus.")
    else:
        await update.message.reply_text("Id tidak ditemukan atau bukan milik Anda.")


async def barang_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = list_appliances(user_id)
    if not rows:
        await update.message.reply_text("Belum ada peralatan. Pakai /barang_tambah <nama> <watt> <volt> [qty] [jam/hari]")
        return

    lines = [f"Daftar peralatan ({len(rows)}):"]
    total_watt = 0.0
    for r in rows:
        lines.append(
            f"- id {r['id']}: {r['name']} | {r['watt']}W | {r['volt']}V | qty {r['qty']} | {r['hours_per_day']} jam/hari"
        )
        total_watt += r["watt"] * r["qty"]
    lines.append(f"\nTotal watt peralatan: {round(total_watt, 2)} W")
    await update.message.reply_text("\n".join(lines))


async def estimasi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    hours_override = None
    if context.args:
        h_raw = context.args[0].replace(",", ".")
        if re.fullmatch(r"\d+(\.\d+)?", h_raw):
            hours_override = float(h_raw)

    daily_kwh, rows = estimate_daily_kwh(user_id, hours_override)
    if not rows:
        await update.message.reply_text("Belum ada peralatan. Pakai /barang_tambah <nama> <watt> <volt> [qty] [jam/hari]")
        return

    tariff_id = get_user_tariff(user_id)
    est_cost = None
    if tariff_id and tariff_id in PLN_TARIFF_2026:
        est_cost, base_tariff, _ = calculate_est_cost(daily_kwh * 30, tariff_id)

    lines = [f"Estimasi konsumsi ({len(rows)} peralatan):\n"]
    for r in rows:
        h = hours_override if hours_override is not None else r["hours_per_day"]
        kwh = round(r["watt"] * r["qty"] * h / 1000, 2)
        lines.append(f"- {r['name']}: {kwh} kWh/hari ({r['watt']}W x {r['qty']} x {h} jam)")
    lines.append(f"\nTotal estimasi: {daily_kwh} kWh/hari")
    if est_cost is not None:
        lines.append(f"Estimasi biaya bulanan: Rp {est_cost:,}")
    else:
        lines.append("Set /golongan untuk estimasi biaya bulanan.")

    await update.message.reply_text("\n".join(lines))


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Format: /ai <pertanyaan>\n"
            "Contoh: /ai apa itu golongan R-1/TR PLN?\n"
            "Contoh: /ai cara hemat listrik di rumah"
        )
        return

    prompt = " ".join(context.args).strip()
    if not _electricity_related(prompt):
        await update.message.reply_text("Maaf, saya cuma bisa bantu soal listrik.")
        return

    reply = _ask_openrouter(prompt)
    if not reply:
        reply = "AI tidak memberikan jawaban."
    await update.message.reply_text(reply)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)


def main():
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # Restore reminder jobs
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, reminder_time FROM users WHERE reminder_enabled=1 AND reminder_time IS NOT NULL")
    rows = cur.fetchall()
    conn.close()
    for row in rows:
        if re.fullmatch(r"\d{1,2}:\d{2}", row["reminder_time"]):
            try:
                set_reminder_job(app, row["user_id"], row["reminder_time"])
            except Exception as e:
                logger.error("Failed to restore reminder for %s: %s", row["user_id"], e)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("catat", catat))
    app.add_handler(CommandHandler("riwayat", riwayat))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("mingguan", mingguan))
    app.add_handler(CommandHandler("bulanan", bulanan))
    app.add_handler(CommandHandler("grafik", grafik))
    app.add_handler(CommandHandler("cek_anomali", cek_anomali))
    app.add_handler(CommandHandler("reminder", reminder_cmd))
    app.add_handler(CommandHandler("cari", cari))
    app.add_handler(CommandHandler("tarif", tarif))
    app.add_handler(CommandHandler("golongan", golongan))
    app.add_handler(CommandHandler("barang_tambah", barang_tambah))
    app.add_handler(CommandHandler("barang_hapus", barang_hapus))
    app.add_handler(CommandHandler("barang_list", barang_list))
    app.add_handler(CommandHandler("estimasi", estimasi_cmd))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print("Bot siap jalan (polling mode)")
    print("Buka Telegram, kirim /start ke bot Anda")
    print("Ctrl+C untuk berhenti\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
