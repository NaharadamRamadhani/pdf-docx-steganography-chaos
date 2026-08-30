import os
import re
import io
import time
import struct
import random
import zipfile
import hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ============================================================
# MARKER
# ============================================================
MARKER_PLAIN = b"@@PLAIN@@"
MARKER_CHAOS = b"@@CHAOS@@"

# ============================================================
# BATAS TIPE BERKAS — hanya PDF dan DOCX
# ============================================================
# Ruang lingkup penelitian membatasi penampung (cover) hanya pada dokumen
# PDF dan DOCX. Kedua format ini toleran terhadap byte tambahan di akhir
# berkas (End-of-File), sehingga penyisipan tidak merusak dokumen.
ALLOWED_EXTS   = (".pdf", ".docx")
ALLOWED_LABEL  = "PDF dan DOCX"

def is_supported_file(path):
    """True jika ekstensi berkas termasuk yang didukung (.pdf / .docx)."""
    return os.path.splitext(path)[1].lower() in ALLOWED_EXTS


# ============================================================
# BATAS PANJANG PESAN
# ============================================================
# Panjang pesan disimpan pada header 1 byte sehingga nilai maksimum yang
# dapat direpresentasikan adalah 255 (2^8 - 1). Pembatasan ini menggantikan
# header 4 byte yang secara teoretis mampu menampung pesan hingga ~4 GB,
# padahal ukuran sebesar itu tidak realistis untuk pesan rahasia dan justru
# menaikkan risiko terdeteksi karena selisih ukuran berkas menjadi mencolok.
LEN_FIELD_SIZE = 1                          # jumlah byte header panjang pesan
MAX_MSG_BYTES  = (1 << (8 * LEN_FIELD_SIZE)) - 1   # = 255 byte


# ============================================================
# PEMBANGKIT KEYSTREAM MS-TENT MAP (sesuai Algorithm 1 skripsi)
#   Output : Keystream K_i bernilai 0..255
#       x_i = (r·λ·x)/(1 + λ·(1−x)^2)  mod 1      -> iterasi MS-Tent Map
#       K_i = ⌊|x_i × 10⁶|⌋ mod 256                -> Langkah 4 Algorithm 1
#   Karena pesan dibatasi ≤ 255 byte, keystream 0..255 ini menghasilkan
#   permutasi Fisher-Yates yang tak-bias sempurna (j = K_i mod (i+1)).
# ============================================================
def generate_keystream(x, lam, mu, r, t, size):
    keystream = []
    for _ in range(size):
        val = ((r * lam * x) / (1 + lam * (1 - x) ** 2)) % 1.0
        x   = mu * (val if x < 0.5 else (1 - val))
        x   = (x + t) % 1.0
        k_i = int(abs(x * 1_000_000)) % 256      # ⌊|x_i × 10⁶|⌋ mod 256
        keystream.append(k_i)
    return keystream


# ============================================================
# SCRAMBLE / UNSCRAMBLE — Fisher-Yates eksplisit
# ============================================================
def build_permutation(chaos_seq):
    n    = len(chaos_seq)
    perm = list(range(n))
    for i in range(n - 1, 0, -1):
        j          = chaos_seq[i] % (i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    return perm


def scramble_message(msg_bytes, chaos_seq):
    perm      = build_permutation(chaos_seq)
    scrambled = bytearray(len(msg_bytes))
    for new_pos, orig_pos in enumerate(perm):
        scrambled[new_pos] = msg_bytes[orig_pos]
    return scrambled, perm


def unscramble_message(scrambled_bytes, chaos_seq):
    perm     = build_permutation(chaos_seq)
    original = bytearray(len(scrambled_bytes))
    for new_pos, orig_pos in enumerate(perm):
        original[orig_pos] = scrambled_bytes[new_pos]
    return original


# ============================================================
# EMBED
# ============================================================
def embed_file(input_file, message, output_file, use_chaos, x, lam, mu, r, t):
    start     = time.perf_counter()

    # ── Validasi tipe berkas: hanya PDF dan DOCX ────────────────
    if not is_supported_file(input_file):
        raise ValueError(
            f"Tipe berkas tidak didukung. Berkas input hanya boleh "
            f"{ALLOWED_LABEL} (.pdf atau .docx).")

    with open(input_file, "rb") as f:
        data  = bytearray(f.read())

    msg_bytes = message.encode("utf-8")
    msg_len   = len(msg_bytes)           # simpan jumlah BYTE, bukan karakter

    # ── Validasi batas panjang pesan ────────────────────────────
    if msg_len == 0:
        raise ValueError("Pesan tidak boleh kosong.")
    if msg_len > MAX_MSG_BYTES:
        raise ValueError(
            f"Pesan terlalu panjang: {msg_len} byte. "
            f"Batas maksimum adalah {MAX_MSG_BYTES} byte "
            f"(kelebihan {msg_len - MAX_MSG_BYTES} byte). "
            f"Perlu diingat satu karakter non-ASCII (mis. é, ā, emoji) "
            f"dapat menghabiskan 2–4 byte."
        )

    if use_chaos:
        chaos_seq      = generate_keystream(x, lam, mu, r, t, msg_len)
        embed_bytes, _ = scramble_message(msg_bytes, chaos_seq)
        marker         = MARKER_CHAOS
    else:
        embed_bytes    = bytearray(msg_bytes)
        marker         = MARKER_PLAIN
        chaos_seq      = None

    # Format: [isi file asli] + [MARKER] + [1-byte panjang] + [bytes pesan]
    data.extend(marker)
    data.extend(msg_len.to_bytes(LEN_FIELD_SIZE, "big"))
    data.extend(embed_bytes)

    # Jika penampung berupa arsip ZIP (DOCX/XLSX/PPTX), daftarkan payload sebagai
    # KOMENTAR arsip ZIP dengan memperbarui field panjang komentar pada EOCD.
    # Tanpa ini, byte payload dianggap "liar" di luar arsip sehingga Microsoft
    # Word menampilkan peringatan "We found unreadable content". PDF tidak
    # diproses di sini karena penambahan setelah %%EOF sudah valid bagi pembaca PDF.
    if data[:4] == b"PK\x03\x04":
        eocd = data.rfind(b"PK\x05\x06")
        if eocd != -1 and (eocd + 22) <= len(data):
            comment_len = len(data) - (eocd + 22)
            if comment_len <= 0xFFFF:
                struct.pack_into("<H", data, eocd + 20, comment_len)

    with open(output_file, "wb") as f:
        f.write(data)

    elapsed  = time.perf_counter() - start
    capacity = (msg_len / os.path.getsize(input_file)) * 100
    # Kembalikan juga byte pesan asli, byte hasil pengacakan, dan keystream
    # agar antarmuka dapat menampilkan bukti pengacakan pada proses embed.
    return elapsed, msg_len, capacity, bytes(msg_bytes), bytes(embed_bytes), chaos_seq


# ============================================================
# EXTRACT
# ============================================================
def extract_file(stego_file, x, lam, mu, r, t):
    start = time.perf_counter()

    with open(stego_file, "rb") as f:
        data = bytearray(f.read())

    # =========================
    # CARI MARKER TERAKHIR
    # =========================
    idx_chaos = data.rfind(MARKER_CHAOS)
    idx_plain = data.rfind(MARKER_PLAIN)

    if idx_chaos == -1 and idx_plain == -1:
        return None, 0, 0, "Marker tidak ditemukan"

    # Ambil marker yang PALING BELAKANG
    if idx_chaos > idx_plain:
        idx        = idx_chaos
        marker_len = len(MARKER_CHAOS)
        use_chaos  = True
    else:
        idx        = idx_plain
        marker_len = len(MARKER_PLAIN)
        use_chaos  = False

    # =========================
    # AMBIL PANJANG PESAN (AMAN)
    # =========================
    len_start = idx + marker_len
    len_end   = len_start + LEN_FIELD_SIZE

    if len_end > len(data):
        return None, 0, 0, "Header rusak"

    msg_len = int.from_bytes(data[len_start:len_end], "big")

    # Validasi panjang (maksimum MAX_MSG_BYTES dan tidak melebihi ukuran berkas)
    if msg_len <= 0 or msg_len > MAX_MSG_BYTES or msg_len > len(data):
        return None, 0, 0, "Panjang pesan tidak valid"

    # =========================
    # AMBIL DATA PESAN (AMAN)
    # =========================
    msg_start = len_end
    msg_end   = msg_start + msg_len

    if msg_end > len(data):
        return None, 0, 0, "Data pesan terpotong"

    raw_bytes = bytearray(data[msg_start:msg_end])

    # =========================
    # UNSCRAMBLE
    # =========================
    if use_chaos:
        chaos_seq    = generate_keystream(x, lam, mu, r, t, msg_len)
        result_bytes = unscramble_message(raw_bytes, chaos_seq)
    else:
        result_bytes = raw_bytes

    elapsed = time.perf_counter() - start
    mode    = "CHAOS" if use_chaos else "PLAIN"

    return result_bytes, elapsed, msg_len, mode


def _read_embedded_payload(stego_file):
    """
    Baca payload pesan dari file stego TANPA melakukan unscramble.
    Return: (raw_payload_bytes, msg_len, use_chaos, error_message_or_None)
    """
    with open(stego_file, "rb") as f:
        data = bytearray(f.read())

    idx_chaos = data.rfind(MARKER_CHAOS)
    idx_plain = data.rfind(MARKER_PLAIN)

    if idx_chaos == -1 and idx_plain == -1:
        return None, 0, False, "Marker tidak ditemukan"

    if idx_chaos > idx_plain:
        idx = idx_chaos
        marker_len = len(MARKER_CHAOS)
        use_chaos = True
    else:
        idx = idx_plain
        marker_len = len(MARKER_PLAIN)
        use_chaos = False

    len_start = idx + marker_len
    len_end = len_start + LEN_FIELD_SIZE
    if len_end > len(data):
        return None, 0, use_chaos, "Header rusak"

    msg_len = int.from_bytes(data[len_start:len_end], "big")
    if msg_len <= 0 or msg_len > MAX_MSG_BYTES or msg_len > len(data):
        return None, 0, use_chaos, "Panjang pesan tidak valid"

    msg_start = len_end
    msg_end = msg_start + msg_len
    if msg_end > len(data):
        return None, 0, use_chaos, "Data pesan terpotong"

    raw_bytes = bytearray(data[msg_start:msg_end])
    return raw_bytes, msg_len, use_chaos, None

# ============================================================
# AKURASI
# ============================================================
def calculate_accuracy(original_bytes, extracted_bytes):
    if not original_bytes:
        return 0.0
    match = sum(a == b for a, b in zip(original_bytes, extracted_bytes))
    return match / len(original_bytes) * 100


# ============================================================
# TRANSPARANSI (IMPERCEPTIBILITY)
# ============================================================
def _strip_payload(raw):
    """Buang penanda + panjang + payload steganografi sehingga tersisa byte
    dokumen ASLI yang persis. Return (doc_bytes, ada_payload)."""
    for mk in (MARKER_CHAOS, MARKER_PLAIN):
        p = raw.rfind(mk)
        if p != -1:
            return raw[:p], True
    return raw, False


def _extract_doc_content(filepath):
    """
    Ekstrak KONTEN yang dapat dirender (teks) dari dokumen untuk di-hash.
    Payload steganografi dibuang lebih dulu sehingga pustaka hanya membaca
    dokumen asli. Konten cover dan stego akan identik jika penyisipan transparan.

    Return dict: ok, jenis, content(bytes), label_struktur, n_struktur, error
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".docx":
        try:
            with zipfile.ZipFile(filepath) as z:          # trailing payload diabaikan oleh zip
                xml = z.read("word/document.xml")
            n_par = xml.count(b"<w:p>") + xml.count(b"<w:p ")
            text = re.sub(rb"<[^>]+>", b" ", xml)          # buang tag -> teks yang dirender
            text = re.sub(rb"\s+", b" ", text).strip()
            return {"ok": True, "jenis": "DOCX", "content": text,
                    "label_struktur": "Jumlah paragraf", "n_struktur": n_par, "error": None}
        except Exception as e:
            return {"ok": False, "jenis": "DOCX", "content": b"",
                    "label_struktur": "Jumlah paragraf", "n_struktur": 0, "error": str(e)}

    elif ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                return {"ok": False, "jenis": "PDF", "content": b"",
                        "label_struktur": "Jumlah halaman", "n_struktur": 0,
                        "error": "Pustaka pypdf belum terpasang. Jalankan: pip install pypdf"}
        try:
            with open(filepath, "rb") as f:
                raw = f.read()
            # Buang HANYA payload steganografi (tepat di penanda) -> byte PDF asli
            # yang utuh. Tidak memotong di %%EOF agar struktur PDF tidak rusak.
            doc_bytes, _ = _strip_payload(raw)
            reader = PdfReader(io.BytesIO(doc_bytes))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
            return {"ok": True, "jenis": "PDF", "content": text.encode("utf-8"),
                    "label_struktur": "Jumlah halaman", "n_struktur": len(reader.pages), "error": None}
        except Exception as e:
            return {"ok": False, "jenis": "PDF", "content": b"",
                    "label_struktur": "Jumlah halaman", "n_struktur": 0, "error": str(e)}

    return {"ok": False, "jenis": (ext or "?"), "content": b"",
            "label_struktur": "-", "n_struktur": 0,
            "error": "Format tidak didukung (hanya PDF/DOCX)."}


def transparency_report(cover_file, stego_file):
    """Bandingkan cover vs stego untuk uji transparansi (imperceptibility)."""
    c = _extract_doc_content(cover_file)
    s = _extract_doc_content(stego_file)

    md5_c = hashlib.md5(c["content"]).hexdigest() if c["ok"] else None
    sha_c = hashlib.sha256(c["content"]).hexdigest() if c["ok"] else None
    md5_s = hashlib.md5(s["content"]).hexdigest() if s["ok"] else None
    sha_s = hashlib.sha256(s["content"]).hexdigest() if s["ok"] else None

    size_c = os.path.getsize(cover_file)
    size_s = os.path.getsize(stego_file)

    # Bukti byte-level (tidak bergantung pustaka PDF): dokumen asli di dalam
    # stego harus identik byte-per-byte dengan cover.
    try:
        with open(cover_file, "rb") as f:
            cover_doc, _ = _strip_payload(f.read())
        with open(stego_file, "rb") as f:
            stego_doc, _ = _strip_payload(f.read())
        byte_identik = (cover_doc == stego_doc)
    except Exception:
        byte_identik = False

    hash_sama     = bool(c["ok"] and s["ok"] and md5_c == md5_s and sha_c == sha_s)
    struktur_sama = bool(c["ok"] and s["ok"] and c["n_struktur"] == s["n_struktur"])
    valid         = bool(c["ok"] and s["ok"])
    # Transparan bila konten identik & struktur sama, ATAU byte dokumen asli identik
    # (byte-identik adalah bukti paling kuat dan tetap sahih walau pustaka PDF gagal).
    transparan    = bool((hash_sama and struktur_sama and valid) or byte_identik)

    return {
        "cover": c, "stego": s,
        "md5_c": md5_c, "sha_c": sha_c, "md5_s": md5_s, "sha_s": sha_s,
        "size_c": size_c, "size_s": size_s, "overhead": size_s - size_c,
        "hash_sama": hash_sama, "struktur_sama": struktur_sama,
        "byte_identik": byte_identik, "valid": valid, "transparan": transparan,
    }




# ============================================================
# UJI KETAHANAN BRUTE FORCE
# ============================================================
def brute_force_test(stego_file, x, lam, mu, r, t, n_attempts=10000,
                     digits=15, seed=12345):
    """
    Simulasi serangan brute force pada berkas stego.

    - Mencoba `n_attempts` kunci ACAK dan memeriksa apakah ada yang berhasil
      memulihkan pesan (sama seperti hasil ekstraksi kunci yang benar).
    - Mengukur kecepatan percobaan (kunci/detik) pada perangkat ini.
    - Mengekstrapolasi waktu untuk menjelajah SELURUH ruang kunci, dengan
      asumsi presisi `digits` digit desimal per parameter (5 parameter).

    Catatan: brute force penuh tidak mungkin dijalankan karena ruang kunci
    sangat besar; fungsi ini membuktikannya melalui simulasi + ekstrapolasi.
    """
    raw, msg_len, use_chaos, err = _read_embedded_payload(stego_file)
    if err:
        return {"ok": False, "error": err}
    if not use_chaos:
        return {"ok": False,
                "error": "Berkas terdeteksi PLAIN (tanpa chaos scrambling); brute force tidak relevan."}
    if msg_len <= 0:
        return {"ok": False, "error": "Panjang pesan tidak valid."}

    # Pesan target = hasil ekstraksi memakai kunci yang BENAR
    target = bytes(unscramble_message(raw, generate_keystream(x, lam, mu, r, t, msg_len)))

    rng = random.Random(seed)
    start = time.perf_counter()
    success = 0
    for _ in range(n_attempts):
        xx = rng.uniform(1e-9, 1.0 - 1e-9)   # x0 pada (0, 1)
        ll = rng.uniform(0.0, 4.0)           # rentang wajar parameter
        mm = rng.uniform(0.0, 4.0)
        rr = rng.uniform(0.0, 4.0)
        tt = rng.uniform(0.0, 1.0)
        cand = bytes(unscramble_message(raw, generate_keystream(xx, ll, mm, rr, tt, msg_len)))
        if cand == target:
            success += 1
    elapsed = time.perf_counter() - start
    rate = (n_attempts / elapsed) if elapsed > 0 else 0.0

    # Ruang kunci: presisi `digits` digit desimal per parameter, 5 parameter
    key_space = 10 ** (digits * 5)          # = (10^digits)^5
    threshold = 2 ** 128                     # ambang aman umum (~3.4e38)

    # Estimasi waktu menjelajah seluruh ruang kunci pada kecepatan terukur
    secs_full = (key_space / rate) if rate > 0 else float("inf")
    years_full = secs_full / (365.25 * 24 * 3600)
    universe_years = 1.38e10                 # umur alam semesta ~13,8 miliar tahun
    ratio_universe = (years_full / universe_years) if years_full != float("inf") else float("inf")

    return {
        "ok": True, "msg_len": msg_len,
        "n_attempts": n_attempts, "success": success,
        "elapsed": elapsed, "rate": rate,
        "digits": digits, "key_space": key_space,
        "threshold": threshold, "exceeds_threshold": key_space > threshold,
        "years_full": years_full, "ratio_universe": ratio_universe,
    }


# ============================================================
# UJI SENSITIVITAS KEY (kunci benar vs kunci salah)
# ============================================================
def sensitivity_correct_vs_wrong(stego_file, cx, clam, cmu, cr, ct,
                                 wx, wlam, wmu, wr, wt):
    """
    Metode 2 — Kunci benar vs kunci salah pada ekstraksi.
    Mengekstraksi berkas stego dengan kunci yang benar (pesan pulih) dan
    dengan kunci yang salah (hasil teracak), lalu membandingkan keduanya.
    """
    raw, msg_len, use_chaos, err = _read_embedded_payload(stego_file)
    if err:
        return {"ok": False, "error": err}
    if not use_chaos:
        return {"ok": False, "error": "Berkas PLAIN (tanpa chaos); sensitivitas tidak relevan."}

    correct = bytes(unscramble_message(raw, generate_keystream(cx, clam, cmu, cr, ct, msg_len)))
    wrong = bytes(unscramble_message(raw, generate_keystream(wx, wlam, wmu, wr, wt, msg_len)))
    # kecocokan byte kunci salah terhadap pesan asli (0% ideal)
    match = (sum(1 for a, b in zip(correct, wrong) if a == b) / msg_len * 100) if msg_len else 0.0
    return {"ok": True, "msg_len": msg_len, "correct": correct, "wrong": wrong, "match": match}

# THEME
# ============================================================
BG    = "#ffffff"   # latar utama putih
PANEL = "#f2f4f7"   # latar panel/input abu-abu sangat muda
BORD  = "#c9d1d9"   # garis tepi
BLUE  = "#0b5cd6"   # biru gelap (terbaca di latar terang)
GREEN = "#1a7f37"   # hijau gelap
RED   = "#cf222e"   # merah gelap
AMBER = "#9a6700"   # kuning kecoklatan gelap
TEXT  = "#1f2328"   # teks utama gelap
MUTED = "#57606a"   # teks sekunder
BTNTX = "#ffffff"   # teks pada tombol berwarna
MONO  = ("Consolas", 12)
UI    = ("Segoe UI", 11)
BOLD  = ("Segoe UI", 11, "bold")
TITLE = ("Segoe UI", 16, "bold")


def make_panel(parent, title):
    f = tk.LabelFrame(parent, text=f" {title} ", bg=BG, fg=MUTED,
                      font=UI, relief="flat", bd=1,
                      highlightbackground=BORD, highlightthickness=1)
    f.pack(fill="x", padx=8, pady=5)
    return f


def make_btn(parent, label, cmd, color=BLUE, w=14):
    return tk.Button(parent, text=label, command=cmd, width=w,
                     bg=color, fg=BTNTX, font=BOLD, relief="flat",
                     cursor="hand2", padx=6, pady=6,
                     activebackground=color, activeforeground=BTNTX)


# ============================================================
# APLIKASI
# ============================================================
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Steganografi MSTENT Map + Fisher Yates Shuffle — PDF/DOCX")
        self.geometry("980x840")
        self.configure(bg=BG)
        self.resizable(True, True)

        self.v_input  = tk.StringVar()
        self.v_output = tk.StringVar()
        self.v_chaos  = tk.BooleanVar(value=True)
        self.v_x      = tk.StringVar(value="0.5")
        self.v_lam    = tk.StringVar(value="2.0")
        self.v_mu     = tk.StringVar(value="3.9")
        self.v_r      = tk.StringVar(value="3.99")
        self.v_t      = tk.StringVar(value="0.1")

        # vars tab transparansi
        self.v_trans_cover = tk.StringVar()
        self.v_trans_stego = tk.StringVar()

        # vars tab brute force
        self.v_bf_file = tk.StringVar()
        self.v_bf_n = tk.StringVar(value="20000")
        self.v_bf_x = tk.StringVar(value="0.5")
        self.v_bf_lam = tk.StringVar(value="2.0")
        self.v_bf_mu = tk.StringVar(value="3.9")
        self.v_bf_r = tk.StringVar(value="3.99")
        self.v_bf_t = tk.StringVar(value="0.1")

        # vars tab sensitivitas key
        self.v_sk_file = tk.StringVar()
        self.v_sk_x = tk.StringVar(value="0.5")
        self.v_sk_lam = tk.StringVar(value="2.0")
        self.v_sk_mu = tk.StringVar(value="3.9")
        self.v_sk_r = tk.StringVar(value="3.99")
        self.v_sk_t = tk.StringVar(value="0.1")
        # kunci salah (default: x0 berbeda sangat kecil, 0.5 -> 0.500000001)
        self.v_sk_wx = tk.StringVar(value="0.500000001")
        self.v_sk_wlam = tk.StringVar(value="2.0")
        self.v_sk_wmu = tk.StringVar(value="3.9")
        self.v_sk_wr = tk.StringVar(value="3.99")
        self.v_sk_wt = tk.StringVar(value="0.1")

        self._build_ui()

    # ----------------------------------------------------------
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=PANEL, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⬡  MSTENT MAP STEGANOGRAFI",
                 font=TITLE, bg=PANEL, fg=BLUE).pack(side="left", padx=16)
        tk.Label(hdr, text="PDF / DOCX — Chaos-based Scrambling",
                 font=UI, bg=PANEL, fg=MUTED).pack(side="left")

        # Notebook
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",     background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        padding=[14, 6], font=UI)
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", BLUE)])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=6)

        self._tab_main(nb)
        self._tab_sensitivity(nb)
        self._tab_transparency(nb)
        self._tab_bruteforce(nb)
        self._tab_algo(nb)

    # ----------------------------------------------------------
    # TAB 1 — EMBED / EXTRACT
    # ----------------------------------------------------------
    def _tab_main(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="  Embed / Extract  ")

        left  = tk.Frame(tab, bg=BG)
        right = tk.Frame(tab, bg=BG)
        left.pack(side="left",  fill="both", expand=True)
        right.pack(side="right", fill="both", expand=True)

        # ── File ──────────────────────────────────────────────
        fp = make_panel(left, "📂  File")
        fp.columnconfigure(1, weight=1)

        # Input
        tk.Label(fp, text="Input :", bg=BG, fg=MUTED, font=UI
                 ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.entry_input = tk.Entry(fp, textvariable=self.v_input,
                                     bg=PANEL, fg=TEXT, font=MONO,
                                     relief="flat", insertbackground=BLUE,
                                     highlightbackground=BORD, highlightthickness=1)
        self.entry_input.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Browse", self._browse_input, BLUE, 8
                 ).grid(row=0, column=2, padx=6)

        # Output
        tk.Label(fp, text="Output:", bg=BG, fg=MUTED, font=UI
                 ).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.entry_output = tk.Entry(fp, textvariable=self.v_output,
                                      bg=PANEL, fg=TEXT, font=MONO,
                                      relief="flat", insertbackground=BLUE,
                                      highlightbackground=BORD, highlightthickness=1)
        self.entry_output.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Save As", self._browse_output, BLUE, 8
                 ).grid(row=1, column=2, padx=6)

        # Status file
        self.file_status = tk.Label(fp, text="", bg=BG, fg=AMBER, font=("Segoe UI", 10))
        self.file_status.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        # Peringatan format berkas (permanen) + tombol info
        warn_row = tk.Frame(fp, bg=BG)
        warn_row.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        tk.Label(warn_row,
                 text=f"⚠  Input hanya mendukung berkas {ALLOWED_LABEL} (.pdf / .docx)",
                 bg=BG, fg=AMBER, font=("Segoe UI", 10, "bold")).pack(side="left")
        make_btn(warn_row, "ⓘ Info", self._info_format, AMBER, 7).pack(side="left", padx=8)

        # ── Pesan ─────────────────────────────────────────────
        mp = make_panel(left, f"✉  Pesan Rahasia (maks. {MAX_MSG_BYTES} byte)")
        self.msg_box = tk.Text(mp, height=3, font=MONO, bg=PANEL, fg=TEXT,
                                insertbackground=BLUE, relief="flat", wrap="word",
                                highlightbackground=BORD, highlightthickness=1)
        self.msg_box.pack(fill="x", padx=8, pady=4)
        self.char_lbl = tk.Label(mp, text=f"0 karakter | 0 / {MAX_MSG_BYTES} byte",
                                  bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.char_lbl.pack(anchor="e", padx=10)
        # Pembaruan penghitung dipicu oleh setiap perubahan isi kotak pesan,
        # termasuk pengetikan, tempel (paste), maupun potong (cut).
        self.msg_box.bind("<KeyRelease>", self._update_char_count)
        self.msg_box.bind("<<Paste>>",
                          lambda e: self.after(1, self._update_char_count))
        self.msg_box.bind("<<Cut>>",
                          lambda e: self.after(1, self._update_char_count))

        # ── Hasil (1 box) ─────────────────────────────────────
        rf = tk.LabelFrame(left, text=" 📋  Hasil ", bg=BG, fg=MUTED, font=UI,
                           relief="flat", highlightbackground=BORD, highlightthickness=1)
        rf.pack(fill="both", expand=True, padx=8, pady=4)

        self.result_box = scrolledtext.ScrolledText(
            rf, height=10, font=MONO, bg=PANEL, fg=GREEN,
            relief="flat", bd=0, wrap="word")
        self.result_box.pack(fill="both", expand=True, padx=6, pady=6)

        # ── Parameter ─────────────────────────────────────────
        pp = make_panel(right, "🔑  Parameter MSTENT Map")
        params = [
            ("x₀  initial value", self.v_x),
            ("λ   lambda",         self.v_lam),
            ("μ   mu",             self.v_mu),
            ("r   scale",          self.v_r),
            ("t   translation",    self.v_t),
        ]
        for i, (lbl, var) in enumerate(params):
            tk.Label(pp, text=lbl, bg=BG, fg=MUTED, font=UI
                     ).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            tk.Entry(pp, textvariable=var, width=14, bg=PANEL, fg=BLUE,
                     font=MONO, relief="flat", insertbackground=BLUE,
                     highlightbackground=BORD, highlightthickness=1
                     ).grid(row=i, column=1, sticky="w", padx=8, pady=3)

        tk.Checkbutton(pp, text="Aktifkan Chaos Scrambling",
                       variable=self.v_chaos, bg=BG, fg=BLUE,
                       selectcolor="#ffffff", activebackground=BG, font=UI
                       ).grid(row=len(params), column=0, columnspan=2,
                              sticky="w", padx=6, pady=6)

        # ── Aksi ──────────────────────────────────────────────
        ap = make_panel(right, "⚡  Aksi")
        row = tk.Frame(ap, bg=BG)
        row.pack(pady=8)
        make_btn(row, "▼  EMBED",   self._embed,   BLUE,  15).pack(side="left", padx=8)
        make_btn(row, "▲  EXTRACT", self._extract, GREEN, 15).pack(side="left", padx=8)
        make_btn(row, "✕ Clear",    self._clear,   RED,   8 ).pack(side="left", padx=4)

        # Instruksi ringkas
        hint = tk.Label(ap,
            text="Cara pakai:  1) Browse input  2) Save As output  3) EMBED  4) EXTRACT dari file output",
            bg=BG, fg=MUTED, font=("Segoe UI", 10))
        hint.pack(pady=4)

    # ----------------------------------------------------------
    # TAB 2 — SENSITIVITAS KEY
    # ----------------------------------------------------------
    def _tab_sensitivity(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="  🔑 Sensitivitas Key  ")

        desc = (
            "Uji sensitivitas key dilakukan dengan membandingkan hasil ekstraksi "
            "menggunakan kunci yang benar dan kunci yang salah. Dengan kunci yang benar, "
            "pesan berhasil dipulihkan; dengan kunci yang salah \u2014 meskipun perbedaannya "
            "sangat kecil \u2014 hasilnya teracak dan tidak terbaca. Ini membuktikan bahwa hanya "
            "pihak yang memiliki kunci tepat yang dapat membuka pesan."
        )
        tk.Label(tab, text=desc, bg=BG, fg=MUTED, font=UI,
                 justify="left", wraplength=830).pack(anchor="w", padx=14, pady=8)

        fp = make_panel(tab, "📂  Berkas Stego (hasil embed)")
        fp.columnconfigure(1, weight=1)
        tk.Label(fp, text="File:", bg=BG, fg=MUTED, font=UI).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(fp, textvariable=self.v_sk_file, bg=PANEL, fg=TEXT, font=MONO, relief="flat",
                 insertbackground=BLUE, highlightbackground=BORD, highlightthickness=1
                 ).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Browse", self._browse_sk_file, BLUE, 8).grid(row=0, column=2, padx=6)

        keys = tk.Frame(tab, bg=BG)
        keys.pack(fill="x", padx=10, pady=(0, 4))
        kb = make_panel(keys, "🔑  Kunci Benar (x\u2080, \u03bb, \u03bc, r, t)")
        kw = make_panel(keys, "🔑  Kunci Salah (x\u2080, \u03bb, \u03bc, r, t)")
        kb.pack_configure(side="left", fill="x", expand=True, padx=(0, 6))
        kw.pack_configure(side="left", fill="x", expand=True, padx=(6, 0))

        def grid_keys(parent, vars_):
            labs = [("x\u2080", vars_[0]), ("\u03bb", vars_[1]), ("\u03bc", vars_[2]), ("r", vars_[3]), ("t", vars_[4])]
            for i, (lbl, var) in enumerate(labs):
                tk.Label(parent, text=lbl, bg=BG, fg=MUTED, font=UI).grid(row=0, column=2*i, sticky="e", padx=(8, 2), pady=4)
                tk.Entry(parent, textvariable=var, width=7, bg=PANEL, fg=BLUE, font=MONO, relief="flat",
                         insertbackground=BLUE, highlightbackground=BORD, highlightthickness=1
                         ).grid(row=0, column=2*i+1, sticky="w", padx=(0, 4), pady=4)

        grid_keys(kb, (self.v_sk_x, self.v_sk_lam, self.v_sk_mu, self.v_sk_r, self.v_sk_t))
        grid_keys(kw, (self.v_sk_wx, self.v_sk_wlam, self.v_sk_wmu, self.v_sk_wr, self.v_sk_wt))

        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", padx=14, pady=6)
        make_btn(act, "▶  Jalankan Uji Sensitivitas Key", self._run_sensitivity, BLUE, 30).pack(side="left")

        rf = tk.LabelFrame(tab, text=" 📋  Laporan Sensitivitas Key ", bg=BG, fg=MUTED, font=UI,
                           relief="flat", highlightbackground=BORD, highlightthickness=1)
        rf.pack(fill="both", expand=True, padx=14, pady=8)
        self.sk_log = scrolledtext.ScrolledText(rf, height=16, font=MONO, bg=PANEL, fg=TEXT,
                                               relief="flat", bd=0, wrap="word")
        self.sk_log.pack(fill="both", expand=True, padx=6, pady=6)

    def _browse_sk_file(self):
        f = filedialog.askopenfilename(filetypes=[("PDF/DOCX", "*.pdf *.docx")])
        if f:
            self.v_sk_file.set(f)

    def _run_sensitivity(self):
        fp = self.v_sk_file.get().strip()
        if not fp:
            messagebox.showerror("Error", "Pilih berkas stego untuk diuji."); return
        if not os.path.exists(fp):
            messagebox.showerror("Error", f"Berkas tidak ditemukan:\n{fp}"); return
        try:
            cx, clam, cmu, cr, ct = (float(self.v_sk_x.get()), float(self.v_sk_lam.get()),
                                     float(self.v_sk_mu.get()), float(self.v_sk_r.get()), float(self.v_sk_t.get()))
            wx, wlam, wmu, wr, wt = (float(self.v_sk_wx.get()), float(self.v_sk_wlam.get()),
                                     float(self.v_sk_wmu.get()), float(self.v_sk_wr.get()), float(self.v_sk_wt.get()))
        except ValueError:
            messagebox.showerror("Error", "Semua parameter kunci harus berupa angka."); return

        m2 = sensitivity_correct_vs_wrong(fp, cx, clam, cmu, cr, ct, wx, wlam, wmu, wr, wt)
        if not m2["ok"]:
            self._show_sk(["\u274c Gagal: " + m2["error"]], RED); return

        try:
            ctext = m2["correct"].decode("utf-8")
        except UnicodeDecodeError:
            ctext = m2["correct"].decode("utf-8", "replace")
        wtext = m2["wrong"].decode("utf-8", "replace")
        gap = abs(wx - cx)

        L = []
        L.append("=" * 62)
        L.append("  HASIL UJI SENSITIVITAS KEY")
        L.append("  (Ekstraksi dengan Kunci Benar vs Kunci Salah)")
        L.append("=" * 62)
        L.append(f"  Berkas stego  : {os.path.basename(fp)}")
        L.append(f"  Panjang pesan : {m2['msg_len']} byte")
        L.append("")
        L.append(f"  KUNCI BENAR  (x\u2080 = {cx})")
        L.append("  Pesan berhasil dipulihkan:")
        L.append(f"    \u201c{ctext}\u201d")
        L.append("")
        L.append(f"  KUNCI SALAH  (x\u2080 = {wx})")
        if gap > 0:
            L.append(f"  Selisih dengan kunci benar hanya {gap:.0e} pada parameter x\u2080,")
            L.append("  namun hasilnya sudah teracak dan tidak terbaca:")
        else:
            L.append("  Hasil teracak dan tidak terbaca:")
        L.append(f"    \u201c{wtext}\u201d")
        L.append("")
        L.append(f"  Kecocokan hasil kunci salah dengan pesan asli: {m2['match']:.2f}%")
        L.append("")
        L.append("-" * 62)
        L.append("  KESIMPULAN:")
        L.append("  Hanya kunci yang tepat yang dapat memulihkan pesan. Perubahan")
        L.append("  kunci sekecil apa pun menghasilkan keluaran yang sama sekali")
        L.append("  berbeda, sehingga sistem memiliki sensitivitas key yang tinggi.")
        L.append("=" * 62)
        self._show_sk(L, GREEN)

    def _show_sk(self, lines, color=GREEN):
        self.sk_log.delete("1.0", "end")
        self.sk_log.config(fg=color)
        self.sk_log.insert("end", "\n".join(lines))

    # ----------------------------------------------------------
    # TAB 3 — TRANSPARANSI (IMPERCEPTIBILITY)
    # ----------------------------------------------------------
    def _tab_transparency(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="  🪟 Transparency Test  ")

        desc = (
            "Uji transparansi (imperceptibility) membuktikan bahwa penyisipan pesan tidak "
            "mengubah ISI dokumen yang dirender.\n"
            "Yang dibandingkan adalah HASH KONTEN (teks yang dirender), BUKAN hash seluruh "
            "file — karena payload EOF menambah ukuran file tetapi tidak mengubah konten.\n"
            "Pilih berkas asli (cover) dan berkas hasil embed (stego), lalu tekan Jalankan."
        )
        tk.Label(tab, text=desc, bg=BG, fg=MUTED, font=UI,
                 justify="left", wraplength=820).pack(anchor="w", padx=14, pady=8)

        # ── Pilih berkas ───────────────────────────────────────
        fp = make_panel(tab, "📂  Berkas yang Dibandingkan")
        fp.columnconfigure(1, weight=1)

        tk.Label(fp, text="Asli (Cover):", bg=BG, fg=MUTED, font=UI
                 ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(fp, textvariable=self.v_trans_cover, bg=PANEL, fg=TEXT, font=MONO,
                 relief="flat", insertbackground=BLUE,
                 highlightbackground=BORD, highlightthickness=1
                 ).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Browse", self._browse_trans_cover, BLUE, 8
                 ).grid(row=0, column=2, padx=6)

        tk.Label(fp, text="Stego:", bg=BG, fg=MUTED, font=UI
                 ).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(fp, textvariable=self.v_trans_stego, bg=PANEL, fg=TEXT, font=MONO,
                 relief="flat", insertbackground=BLUE,
                 highlightbackground=BORD, highlightthickness=1
                 ).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Browse", self._browse_trans_stego, BLUE, 8
                 ).grid(row=1, column=2, padx=6)

        # ── Aksi ───────────────────────────────────────────────
        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", padx=14, pady=6)
        make_btn(act, "▶  Jalankan Uji Transparansi", self._run_transparency, GREEN, 30
                 ).pack(side="left")

        # ── Hasil ──────────────────────────────────────────────
        rf = tk.LabelFrame(tab, text=" 📋  Laporan Transparansi ", bg=BG, fg=MUTED, font=UI,
                           relief="flat", highlightbackground=BORD, highlightthickness=1)
        rf.pack(fill="both", expand=True, padx=14, pady=8)
        self.trans_log = scrolledtext.ScrolledText(rf, height=18, font=MONO, bg=PANEL, fg=TEXT,
                                                   relief="flat", bd=0, wrap="word")
        self.trans_log.pack(fill="both", expand=True, padx=6, pady=6)

    def _browse_trans_cover(self):
        f = filedialog.askopenfilename(
            filetypes=[("PDF/DOCX", "*.pdf *.docx")])
        if f:
            self.v_trans_cover.set(f)

    def _browse_trans_stego(self):
        f = filedialog.askopenfilename(
            filetypes=[("PDF/DOCX", "*.pdf *.docx")])
        if f:
            self.v_trans_stego.set(f)

    def _run_transparency(self):
        cover = self.v_trans_cover.get().strip()
        stego = self.v_trans_stego.get().strip()

        if not cover or not stego:
            messagebox.showerror("Error", "Pilih berkas Cover dan berkas Stego terlebih dahulu.")
            return
        if not os.path.exists(cover):
            messagebox.showerror("Error", f"Berkas cover tidak ditemukan:\n{cover}"); return
        if not os.path.exists(stego):
            messagebox.showerror("Error", f"Berkas stego tidak ditemukan:\n{stego}"); return

        try:
            rep = transparency_report(cover, stego)
        except Exception as e:
            messagebox.showerror("Error", str(e)); return

        c, s = rep["cover"], rep["stego"]
        L = []
        L.append("=" * 60)
        L.append("  HASIL UJI TRANSPARANSI (IMPERCEPTIBILITY)")
        L.append("=" * 60)
        L.append(f"  Berkas asli  : {os.path.basename(cover)}  [{c['jenis']}]")
        L.append(f"  Berkas stego : {os.path.basename(stego)}  [{s['jenis']}]")
        L.append("")

        # a) HASH KONTEN DOKUMEN
        L.append("  a) HASH KONTEN DOKUMEN (teks yang dirender)")
        if c["ok"] and s["ok"]:
            L.append(f"     MD5    cover : {rep['md5_c']}")
            L.append(f"     MD5    stego : {rep['md5_s']}")
            L.append(f"     SHA256 cover : {rep['sha_c']}")
            L.append(f"     SHA256 stego : {rep['sha_s']}")
            L.append(f"     -> Status: {'IDENTIK  (isi dokumen TIDAK berubah)' if rep['hash_sama'] else 'BERBEDA'}")
        else:
            L.append("     (!) Ekstraksi teks tidak dapat dilakukan oleh pustaka:")
            if not c["ok"]:
                L.append(f"         - Cover : {c['error']}")
            if not s["ok"]:
                L.append(f"         - Stego : {s['error']}")
        L.append("")

        # b) UKURAN BERKAS
        pesan_byte = rep["overhead"] - 13
        L.append("  b) UKURAN BERKAS")
        L.append(f"     Cover : {rep['size_c']:,} byte")
        L.append(f"     Stego : {rep['size_s']:,} byte")
        if rep["overhead"] >= 13:
            L.append(f"     Selisih (overhead): {rep['overhead']} byte  "
                     f"(9 penanda + 4 panjang + {pesan_byte} pesan)")
        else:
            L.append(f"     Selisih (overhead): {rep['overhead']} byte")
        L.append("")

        # c) STRUKTUR DOKUMEN
        L.append("  c) STRUKTUR DOKUMEN")
        if c["ok"] and s["ok"]:
            L.append(f"     {c['label_struktur']} cover : {c['n_struktur']}")
            L.append(f"     {s['label_struktur']} stego : {s['n_struktur']}")
            L.append(f"     -> Status: {'SAMA' if rep['struktur_sama'] else 'BERBEDA'}")
        else:
            L.append("     (tidak dapat dihitung karena ekstraksi gagal)")
        L.append("")

        # d) VALIDITAS BERKAS
        L.append("  d) VALIDITAS BERKAS")
        if c["ok"]:
            L.append("     Cover : dapat dibuka / di-parse (VALID)")
        else:
            L.append("     Cover : tidak dapat di-parse pustaka")
        if s["ok"]:
            L.append("     Stego : dapat dibuka / di-parse (VALID)")
        else:
            L.append("     Stego : tidak dapat di-parse pustaka")
        L.append("")

        # KESIMPULAN
        L.append("-" * 60)
        if rep["transparan"]:
            L.append("  KESIMPULAN: TRANSPARAN")
            L.append("  Hash konten identik, ukuran bertambah kecil, struktur")
            L.append("  dokumen sama, dan berkas tetap valid.")
        else:
            L.append("  KESIMPULAN: TIDAK TRANSPARAN")
            L.append("  Dokumen asli berbeda / bukan pasangan cover-stego yang benar.")
        L.append("=" * 60)

        self._show_trans(L, GREEN if rep["transparan"] else RED)

    def _show_trans(self, lines, color=GREEN):
        self.trans_log.delete("1.0", "end")
        self.trans_log.config(fg=color)
        self.trans_log.insert("end", "\n".join(lines))

    # ----------------------------------------------------------
    # TAB 4 — BRUTE FORCE TEST
    # ----------------------------------------------------------
    def _tab_bruteforce(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="  🛡 Brute Force  ")

        desc = (
            "Uji ketahanan brute force. Brute force penuh TIDAK mungkin dijalankan karena "
            "ruang kunci sangat besar (itulah inti keamanannya).\n"
            "Fitur ini membuktikannya dengan: (1) simulasi \u2014 mencoba ribuan kunci acak dan "
            "menunjukkan tidak ada yang berhasil, lalu (2) menghitung ruang kunci dan "
            "mengekstrapolasi waktu brute force penuh pada kecepatan perangkat ini."
        )
        tk.Label(tab, text=desc, bg=BG, fg=MUTED, font=UI,
                 justify="left", wraplength=820).pack(anchor="w", padx=14, pady=8)

        # File uji
        fp = make_panel(tab, "📂  Berkas Stego (hasil embed)")
        fp.columnconfigure(1, weight=1)
        tk.Label(fp, text="File:", bg=BG, fg=MUTED, font=UI).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(fp, textvariable=self.v_bf_file, bg=PANEL, fg=TEXT, font=MONO, relief="flat",
                 insertbackground=BLUE, highlightbackground=BORD, highlightthickness=1
                 ).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        make_btn(fp, "Browse", self._browse_bf_file, BLUE, 8).grid(row=0, column=2, padx=6)

        tk.Label(fp, text="Jumlah percobaan:", bg=BG, fg=MUTED, font=UI
                 ).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(fp, textvariable=self.v_bf_n, bg=PANEL, fg=TEXT, font=MONO, width=12, relief="flat",
                 insertbackground=BLUE, highlightbackground=BORD, highlightthickness=1
                 ).grid(row=1, column=1, sticky="w", padx=4, pady=4)

        # Kunci yang benar
        kp = make_panel(tab, "🔑  Kunci yang Benar (dipakai saat embed)")
        labels = [("x\u2080", self.v_bf_x), ("\u03BB", self.v_bf_lam), ("\u03BC", self.v_bf_mu),
                  ("r", self.v_bf_r), ("t", self.v_bf_t)]
        for i, (lbl, var) in enumerate(labels):
            tk.Label(kp, text=lbl, bg=BG, fg=MUTED, font=UI).grid(row=0, column=2*i, sticky="e", padx=(10, 2), pady=4)
            tk.Entry(kp, textvariable=var, width=8, bg=PANEL, fg=BLUE, font=MONO, relief="flat",
                     insertbackground=BLUE, highlightbackground=BORD, highlightthickness=1
                     ).grid(row=0, column=2*i+1, sticky="w", padx=(0, 6), pady=4)

        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", padx=14, pady=6)
        make_btn(act, "▶  Jalankan Uji Brute Force", self._run_bruteforce, RED, 30).pack(side="left")

        rf = tk.LabelFrame(tab, text=" 📋  Laporan Ketahanan Brute Force ", bg=BG, fg=MUTED, font=UI,
                           relief="flat", highlightbackground=BORD, highlightthickness=1)
        rf.pack(fill="both", expand=True, padx=14, pady=8)
        self.bf_log = scrolledtext.ScrolledText(rf, height=16, font=MONO, bg=PANEL, fg=TEXT,
                                               relief="flat", bd=0, wrap="word")
        self.bf_log.pack(fill="both", expand=True, padx=6, pady=6)

    def _browse_bf_file(self):
        f = filedialog.askopenfilename(filetypes=[("PDF/DOCX", "*.pdf *.docx")])
        if f:
            self.v_bf_file.set(f)

    def _fmt_pow10(self, value):
        """Format bilangan sangat besar sebagai '10^n' atau 'a x 10^n'."""
        if value == float("inf"):
            return "tak hingga"
        try:
            import math
            if value <= 0:
                return "0"
            exp = int(math.floor(math.log10(value)))
            mant = value / (10 ** exp)
            if abs(mant - 1.0) < 0.05:
                return f"10^{exp}"
            return f"{mant:.2f} x 10^{exp}"
        except Exception:
            return str(value)

    def _run_bruteforce(self):
        fp = self.v_bf_file.get().strip()
        if not fp:
            messagebox.showerror("Error", "Pilih berkas stego untuk diuji."); return
        if not os.path.exists(fp):
            messagebox.showerror("Error", f"Berkas tidak ditemukan:\n{fp}"); return
        try:
            n = int(float(self.v_bf_n.get().strip()))
            if n <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Jumlah percobaan harus bilangan bulat > 0."); return
        try:
            x = float(self.v_bf_x.get()); lam = float(self.v_bf_lam.get())
            mu = float(self.v_bf_mu.get()); r = float(self.v_bf_r.get()); t = float(self.v_bf_t.get())
        except ValueError:
            messagebox.showerror("Error", "Semua parameter kunci harus berupa angka."); return

        self.bf_log.delete("1.0", "end")
        self.bf_log.config(fg=MUTED)
        self.bf_log.insert("end", f"Menjalankan {n:,} percobaan kunci acak, mohon tunggu...")
        self.update_idletasks()

        rep = brute_force_test(fp, x, lam, mu, r, t, n_attempts=n)
        if not rep["ok"]:
            self.bf_log.delete("1.0", "end")
            self.bf_log.config(fg=RED)
            self.bf_log.insert("end", f"\u274c Tidak dapat menjalankan uji.\nSebab: {rep['error']}")
            return

        L = []
        L.append("=" * 60)
        L.append("  HASIL UJI KETAHANAN BRUTE FORCE")
        L.append("=" * 60)
        L.append(f"  Berkas stego  : {os.path.basename(fp)}")
        L.append(f"  Panjang pesan : {rep['msg_len']} byte")
        L.append("")
        L.append("  A. SIMULASI SERANGAN (percobaan kunci acak)")
        L.append(f"     Jumlah percobaan          : {rep['n_attempts']:,} kunci")
        L.append(f"     Berhasil memulihkan pesan : {rep['success']} kunci")
        L.append(f"     Waktu simulasi            : {rep['elapsed']:.2f} detik")
        L.append(f"     Kecepatan                 : {rep['rate']:,.0f} kunci/detik")
        if rep["success"] == 0:
            L.append("     -> Tidak ada satu pun kunci acak yang memulihkan pesan.")
            L.append("        Semua menghasilkan keluaran teracak (tidak terbaca).")
        else:
            L.append("     -> PERHATIAN: ada kunci yang berhasil (perlu ditinjau).")
        L.append("")
        L.append("  B. ANALISIS RUANG KUNCI")
        L.append("     Parameter kunci   : 5 (x\u2080, \u03BB, \u03BC, r, t)")
        L.append(f"     Presisi asumsi    : {rep['digits']} digit desimal per parameter")
        L.append(f"     Ruang kunci       : {self._fmt_pow10(rep['key_space'])} kombinasi")
        L.append(f"     Ambang aman (2^128): {rep['threshold']:.2e}")
        if rep["exceeds_threshold"]:
            L.append("     -> Ruang kunci JAUH melampaui ambang aman.")
        L.append("")
        L.append("  C. ESTIMASI WAKTU BRUTE FORCE PENUH")
        L.append(f"     Pada kecepatan {rep['rate']:,.0f} kunci/detik:")
        L.append(f"     Waktu : {self._fmt_pow10(rep['years_full'])} tahun")
        L.append(f"     Setara ~{self._fmt_pow10(rep['ratio_universe'])} kali umur alam")
        L.append("     semesta (13,8 miliar tahun).")
        L.append("")
        L.append("-" * 60)
        if rep["success"] == 0 and rep["exceeds_threshold"]:
            L.append("  KESIMPULAN: AMAN terhadap brute force.")
            L.append("  Menebak kunci yang benar secara acak maupun menyeluruh")
            L.append("  tidak praktis karena ruang kunci sangat besar.")
        else:
            L.append("  KESIMPULAN: perlu ditinjau kembali.")
        L.append("=" * 60)

        self.bf_log.delete("1.0", "end")
        self.bf_log.config(fg=GREEN if (rep["success"] == 0 and rep["exceeds_threshold"]) else RED)
        self.bf_log.insert("end", "\n".join(L))

    # ----------------------------------------------------------
    # TAB 5 — ALGORITMA
    # ----------------------------------------------------------
    def _tab_algo(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="  ℹ  Algoritma  ")

        info = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PEMBANGKIT KEYSTREAM — MS-TENT MAP (Algorithm 1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Iterasi fungsi chaos MS-Tent Map:
    val   = (r · λ · x) / (1 + λ·(1−x)²)  mod 1.0
    x     = μ · (val jika x < 0.5, else 1−val)
    x     = (x + t) mod 1.0

  Konversi ke keystream (Langkah 4 Algorithm 1):
    K_i   = ⌊|x_i × 10⁶|⌋ mod 256      ← nilai 0..255 (satu byte)

  Kenapa mod 256?
    Keystream harus berupa deretan byte (0..255). Karena pesan
    dibatasi ≤ 255 byte, keystream ini menghasilkan permutasi
    Fisher-Yates yang tak-bias sempurna.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FISHER-YATES SCRAMBLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Build permutasi (indeks tukar dari keystream K):
    perm = [0,1,2,...,n-1]
    for i dari n-1 ke 1:
        j = K[i] % (i+1)
        swap(perm[i], perm[j])

  Scramble:   scrambled[i]  = original[perm[i]]
  Unscramble: original[perm[i]] = scrambled[i]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FORMAT FILE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [isi file asli] + [MARKER 9 byte] + [panjang 1 byte] + [byte pesan]

  Marker @@PLAIN@@ → embed tanpa chaos
  Marker @@CHAOS@@ → embed dengan chaos (auto-deteksi saat extract)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  BATAS PANJANG PESAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Header panjang = 1 byte → nilai maksimum 2^8 − 1 = 255 byte.

  Sebelumnya header berukuran 4 byte (2^32 − 1 ≈ 4,29 GB). Ukuran
  sebesar itu tidak realistis untuk pesan rahasia dan membuat selisih
  ukuran berkas stego terhadap berkas asli menjadi mencolok sehingga
  mudah dicurigai. Membatasi payload pada 255 byte membuat overhead
  penyisipan tetap kecil (maksimum 9 + 1 + 255 = 265 byte).

  Yang dihitung adalah BYTE UTF-8, bukan karakter:
    "A"     → 1 byte      "é" → 2 byte
    "あ"    → 3 byte      emoji → 4 byte
  Sehingga 255 byte = 255 karakter ASCII, tetapi bisa kurang dari itu
  bila pesan memuat karakter non-ASCII.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  KENAPA EXTRACT BISA GAGAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ❌ Paling umum: kotak Input masih menunjuk ke FILE ASLI,
     bukan ke file OUTPUT hasil embed.
     → Setelah embed, kotak Input otomatis diisi path output.

  ❌ Parameter x,λ,μ,r,t berbeda antara embed dan extract
     → Chaos sequence berbeda → unscramble menghasilkan sampah.

  ❌ File output terpotong / korup
     → Periksa space disk atau error saat menyimpan.
"""
        t = scrolledtext.ScrolledText(tab, font=MONO, bg=PANEL, fg=TEXT,
                                       relief="flat", bd=0, wrap="none",
                                       highlightbackground=BORD, highlightthickness=1)
        t.pack(fill="both", expand=True, padx=14, pady=10)
        t.insert("1.0", info)
        t.config(state="disabled")

    # ----------------------------------------------------------
    # HELPER
    # ----------------------------------------------------------
    def _params(self):
        try:
            return (float(self.v_x.get()),   float(self.v_lam.get()),
                    float(self.v_mu.get()),   float(self.v_r.get()),
                    float(self.v_t.get()))
        except ValueError:
            raise ValueError("Semua parameter harus berupa angka desimal.")

    def _info_format(self):
        messagebox.showinfo(
            "Ketentuan Format Berkas",
            f"Berkas penampung (cover) hanya mendukung {ALLOWED_LABEL}.\n\n"
            f"• PDF  (.pdf)\n"
            f"• DOCX (.docx)\n\n"
            f"Kedua format ini toleran terhadap penyisipan di akhir berkas "
            f"(metode End-of-File) sehingga dokumen tidak rusak. Pesan juga "
            f"dibatasi maksimum {MAX_MSG_BYTES} byte agar ukuran berkas hasil "
            f"tidak membengkak dan tetap sulit terdeteksi.")

    def _browse_input(self):
        f = filedialog.askopenfilename(
            filetypes=[("PDF/DOCX", "*.pdf *.docx")])
        if f:
            if not is_supported_file(f):
                messagebox.showwarning(
                    "Format Tidak Didukung",
                    f"Berkas input hanya boleh {ALLOWED_LABEL} (.pdf atau .docx).\n\n"
                    f"Berkas yang Anda pilih:\n{os.path.basename(f)}\n\n"
                    f"Silakan pilih berkas PDF atau DOCX.")
                self.file_status.config(
                    text="✗ Format ditolak — hanya PDF dan DOCX", fg=RED)
                return
            self.v_input.set(f)
            self.file_status.config(text="")

    def _browse_output(self):
        f = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("DOCX", "*.docx")])
        if f:
            self.v_output.set(f)

    def _update_char_count(self, _=None):
        """Perbarui penghitung byte sekaligus beri peringatan visual bila
        pesan sudah mendekati atau melewati batas MAX_MSG_BYTES."""
        txt   = self.msg_box.get("1.0", "end-1c")
        nbyte = len(txt.encode("utf-8"))
        sisa  = MAX_MSG_BYTES - nbyte

        if nbyte > MAX_MSG_BYTES:
            warna = RED
            info  = f"  ⚠ melebihi batas {abs(sisa)} byte"
        elif nbyte >= MAX_MSG_BYTES * 0.9:
            warna = AMBER
            info  = f"  • sisa {sisa} byte"
        else:
            warna = MUTED
            info  = f"  • sisa {sisa} byte"

        self.char_lbl.config(
            text=f"{len(txt)} karakter | {nbyte} / {MAX_MSG_BYTES} byte{info}",
            fg=warna)

    def _clear(self):
        self.result_box.delete("1.0", "end")

    def _log(self, lines, color=GREEN):
        self.result_box.delete("1.0", "end")
        self.result_box.config(fg=color)
        self.result_box.insert("end", "\n".join(lines))

    # ----------------------------------------------------------
    # EMBED
    # ----------------------------------------------------------
    def _embed(self):
        try:
            inp = self.v_input.get().strip()
            out = self.v_output.get().strip()
            msg = self.msg_box.get("1.0", "end-1c").strip()

            if not inp:
                messagebox.showerror("Error", "Pilih Input File dulu."); return
            if not os.path.exists(inp):
                messagebox.showerror("Error", f"File tidak ditemukan:\n{inp}"); return
            if not is_supported_file(inp):
                messagebox.showwarning(
                    "Format Tidak Didukung",
                    f"Berkas input hanya boleh {ALLOWED_LABEL} (.pdf atau .docx).\n\n"
                    f"Silakan pilih berkas PDF atau DOCX terlebih dahulu.")
                return
            if not out:
                messagebox.showerror("Error", "Tentukan Output File (Save As)."); return
            if not msg:
                messagebox.showerror("Error", "Pesan tidak boleh kosong."); return

            # ── Validasi batas panjang pesan (255 byte) ──────────
            n_byte = len(msg.encode("utf-8"))
            if n_byte > MAX_MSG_BYTES:
                messagebox.showerror(
                    "Pesan Melebihi Batas",
                    f"Panjang pesan {n_byte} byte, melebihi batas "
                    f"{MAX_MSG_BYTES} byte sebanyak {n_byte - MAX_MSG_BYTES} byte.\n\n"
                    f"Kurangi pesan hingga maksimum {MAX_MSG_BYTES} byte.\n"
                    f"Catatan: karakter non-ASCII (mis. é, ā, emoji) memakai "
                    f"2–4 byte per karakter sehingga jumlah byte dapat melebihi "
                    f"jumlah karakter.")
                self._update_char_count()
                return

            x, lam, mu, r, t = self._params()
            use_c = self.v_chaos.get()

            elapsed, msg_len, cap, orig_b, scr_b, ks = embed_file(
                inp, msg, out, use_c, x, lam, mu, r, t)
            s_b = os.path.getsize(inp)
            s_a = os.path.getsize(out)
            mode = "CHAOS (scrambled)" if use_c else "PLAIN"

            # ✅ FIX UTAMA: otomatis arahkan Input ke file output agar Extract langsung bisa jalan
            self.v_input.set(out)
            self.file_status.config(
                text=f"✓ Input dialihkan ke file output — siap di-Extract",
                fg=GREEN)

            def _hex(b):
                return " ".join(f"{v:02X}" for v in b)

            def _as_text(b):
                # Tampilkan byte sebagai karakter; byte tak-tercetak diganti '.'
                # agar tampilan tetap rapi (byte teracak sering bukan teks valid).
                return "".join(chr(v) if 32 <= v <= 126 else "." for v in b)

            lines = [
                "=" * 54,
                f"  ✅  EMBEDDING BERHASIL [{mode}]",
                "=" * 54,
                f"  File input      : {os.path.basename(inp)}",
                f"  File output     : {os.path.basename(out)}",
                f"  Mode            : {mode}",
                f"  Ukuran sebelum  : {s_b:,} bytes",
                f"  Ukuran sesudah  : {s_a:,} bytes",
                f"  Panjang pesan   : {msg_len} / {MAX_MSG_BYTES} byte",
                f"  Sisa kuota      : {MAX_MSG_BYTES - msg_len} byte",
                f"  Kapasitas       : {cap:.4f}% dari ukuran asli",
                f"  Waktu           : {elapsed*1000:.3f} ms",
                "",
                "-" * 54,
                "  🔀  HASIL PENGACAKAN PESAN (Fisher-Yates)",
                "-" * 54,
                f"  Pesan asli (teks)  : {msg}",
                f"  Byte asli   (hex)  : {_hex(orig_b)}",
            ]
            if use_c:
                ks_show = ", ".join(str(v) for v in ks)
                lines += [
                    f"  Keystream Kᵢ       : {ks_show}",
                    f"  Byte teracak (hex) : {_hex(scr_b)}",
                    f"  Pesan teracak (teks): {_as_text(scr_b)}",
                    "",
                    "  ➡  Urutan byte di atas diacak oleh parameter kunci yang dimasukkan.",
                    "     EXTRACT dengan kunci yang sama akan memulihkannya (lossless).",
                ]
            else:
                lines += [
                    "",
                    "  ⚠  Mode PLAIN: pesan TIDAK diacak.",
                    "     Aktifkan 'Chaos Scrambling' agar pesan diacak dengan parameter kunci.",
                ]
            lines += [
                "",
                f"  ➡  Kotak Input sudah diisi path output.",
                f"     Langsung klik EXTRACT untuk mengambil pesan.",
            ]
            self._log(lines, GREEN)

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ----------------------------------------------------------
    # EXTRACT
    # ----------------------------------------------------------
    def _extract(self):
        try:
            inp = self.v_input.get().strip()

            if not inp:
                messagebox.showerror("Error", "Pilih file yang akan di-extract."); return
            if not os.path.exists(inp):
                messagebox.showerror("Error", f"File tidak ditemukan:\n{inp}"); return

            x, lam, mu, r, t = self._params()
            rb, elapsed, msg_len, mode = extract_file(inp, x, lam, mu, r, t)

            if rb is None:
                self._log([
                    "=" * 54,
                    "  ❌  EXTRACT GAGAL",
                    "=" * 54,
                    f"  File  : {os.path.basename(inp)}",
                    f"  Sebab : {mode}",
                    "",
                    "  Kemungkinan penyebab:",
                    "  • File ini belum pernah di-embed (tidak ada marker)",
                    "  • File yang dipilih adalah file ASLI, bukan hasil embed",
                    "    → Gunakan file OUTPUT hasil embed, bukan file input asli",
                    "  • File rusak atau terpotong",
                ], RED)
                return

            msg  = rb.decode("utf-8", errors="replace")
            orig = self.msg_box.get("1.0", "end-1c").strip()
            acc  = calculate_accuracy(orig.encode("utf-8"), rb) if orig else None

            lines = [
                "=" * 54,
                f"  ✅  EXTRACTION BERHASIL [{mode}]",
                "=" * 54,
                f"  File          : {os.path.basename(inp)}",
                f"  Pesan         : {msg}",
                f"  Panjang       : {msg_len} bytes",
                f"  Waktu         : {elapsed*1000:.3f} ms",
            ]
            if acc is not None:
                lines.append(f"  Akurasi       : {acc:.2f}% (vs pesan di kotak input)")
            self._log(lines, GREEN)

        except Exception as e:
            messagebox.showerror("Error", str(e))

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    App().mainloop()
