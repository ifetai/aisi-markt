"""Marktstand-App "Erkennst du, was ein Defekt ist?" (AISI) fuer Streamlit Community Cloud.

Ansichten (ueber den URL-Parameter view):
  /                         Handy der Besuchenden (Zugang per QR-Code)
  /?view=screen             Grossbildschirm mit Live-Ergebnissen
  /?view=admin&key=...      Moderation

Die Antworten liegen im Speicher der App, damit alle Ansichten sofort dieselben Zahlen sehen.
Ist Google Sheets in den Secrets eingerichtet, werden sie zusaetzlich dort gesichert und beim
Neustart wieder geladen.
"""

import io
import random
import re
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import segno
import streamlit as st
from PIL import Image

ROOT = Path(__file__).parent
CONTENT = ROOT / "content"
ROUND2_PER_PERSON = 5
RED, RED_D, NAVY, AMBER, GREY, GREEN = "#EB0000", "#C00000", "#1A2B4C", "#F0A800", "#F2F2F2", "#1E7B34"

VIEW = st.query_params.get("view", "play")
st.set_page_config(page_title="Erkennst du, was ein Defekt ist?", page_icon="🔎",
                   layout="wide" if VIEW in ("screen", "admin") else "centered",
                   initial_sidebar_state="collapsed")


def secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


ADMIN_KEY = str(secret("admin_key", "aisi"))


# ---------- Bilder ----------
@st.cache_resource
def load_images():
    """Runde 1: content/runde1/relevant|irrelevant/*.jpg, Runde 2: content/runde2/<Breite>mm_<Nr>.jpg."""
    images, r1, r2 = {}, [], []
    exts = {".jpg", ".jpeg", ".png", ".webp"}

    def prepared(path, max_side=1000, ratio=None):
        im = Image.open(path).convert("RGB")
        if ratio:  # mittig auf einheitliches Seitenverhaeltnis zuschneiden (Raster am Bildschirm)
            w, h = im.size
            if w / h > ratio:
                nw = int(h * ratio)
                im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            else:
                nh = int(w / ratio)
                im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    for verdict in ("relevant", "irrelevant"):
        folder = CONTENT / "runde1" / verdict
        for f in sorted(folder.iterdir()) if folder.is_dir() else []:
            if f.suffix.lower() in exts:
                iid = f"r1-{verdict}-{f.stem}"
                images[iid] = {"id": iid, "round": 1, "truth": verdict, "data": prepared(f), "thumb": prepared(f, 500, 1.4)}
                r1.append(iid)
    folder = CONTENT / "runde2"
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        m = re.match(r"^(\d+(?:[.,]\d+)?)\s*mm", f.name, re.I)
        if f.suffix.lower() in exts and m:
            iid = f"r2-{f.stem}"
            images[iid] = {"id": iid, "round": 2, "truth": float(m.group(1).replace(",", ".")), "data": prepared(f)}
            r2.append(iid)
    r2.sort(key=lambda i: images[i]["truth"])
    max_mm = int(max([6.0] + [images[i]["truth"] for i in r2]) + 0.999)
    return images, r1, r2, max_mm


IMAGES, ROUND1, ROUND2, MAX_MM = load_images()


# ---------- Gemeinsamer Speicher fuer alle Sitzungen ----------
class Store:
    HEADER = ["teilnahme", "bild", "wert", "zeit"]

    def __init__(self):
        self.lock = threading.Lock()
        self.answers, self.seen, self.pending = [], set(), []
        self.reveal = False  # Aufloesung auf dem Grossbildschirm zeigen
        self.sheet_error = None
        self.ws = self._open_sheet()
        if self.ws is not None:
            self._load_from_sheet()
            threading.Thread(target=self._flush_loop, daemon=True).start()

    # Google Sheets ist optional
    def _open_sheet(self):
        creds, sheet_id = secret("gcp_service_account"), secret("sheet_id")
        if not creds or not sheet_id:
            return None
        try:
            import gspread
            gc = gspread.service_account_from_dict(dict(creds))
            sh = gc.open_by_key(sheet_id)
            try:
                return sh.worksheet("antworten")
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet("antworten", rows=1000, cols=4)
                ws.append_row(self.HEADER)
                return ws
        except Exception as e:  # App laeuft auch ohne Sheet weiter
            self.sheet_error = str(e)
            return None

    def _load_from_sheet(self):
        try:
            rows = self.ws.get_all_values()[1:]
        except Exception as e:
            self.sheet_error = str(e)
            return
        for pid, iid, value, ts in (r[:4] for r in rows if len(r) >= 4):
            img = IMAGES.get(iid)
            if not img or f"{pid}|{iid}" in self.seen:
                continue
            v = value if img["round"] == 1 else float(value)
            self.answers.append({"pid": pid, "id": iid, "value": v, "ts": ts})
            self.seen.add(f"{pid}|{iid}")

    def _flush_loop(self):
        # Antworten gesammelt alle paar Sekunden schreiben, das schont das API-Kontingent
        while True:
            time.sleep(4)
            with self.lock:
                batch, self.pending = self.pending, []
            if not batch:
                continue
            try:
                self.ws.append_rows(batch, value_input_option="RAW")
                self.sheet_error = None
            except Exception as e:
                self.sheet_error = str(e)
                with self.lock:
                    self.pending = batch + self.pending

    def add(self, pid, iid, value):
        key = f"{pid}|{iid}"
        with self.lock:
            if key in self.seen:
                return
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.answers.append({"pid": pid, "id": iid, "value": value, "ts": ts})
            self.seen.add(key)
            if self.ws is not None:
                self.pending.append([pid, iid, str(value), ts])

    def stats(self):
        with self.lock:
            answers = list(self.answers)
        r1 = {i: {"id": i, "truth": IMAGES[i]["truth"], "relevant": 0, "irrelevant": 0} for i in ROUND1}
        r2 = {i: {"id": i, "truth": IMAGES[i]["truth"], "values": []} for i in ROUND2}
        people = set()
        for a in answers:
            people.add(a["pid"])
            if a["id"] in r1:
                r1[a["id"]][a["value"]] += 1
            elif a["id"] in r2:
                r2[a["id"]]["values"].append(a["value"])
        return {"participants": len(people), "answers": len(answers), "r1": list(r1.values()), "r2": list(r2.values())}

    def csv(self):
        with self.lock:
            answers = list(self.answers)
        rows = ["teilnahme;bild;runde;antwort;fach_bzw_breite_mm;zeit"]
        for a in answers:
            i = IMAGES[a["id"]]
            rows.append(";".join(str(x) for x in (a["pid"], a["id"], i["round"], a["value"], i["truth"], a["ts"])))
        return "\n".join(rows)

    def reset(self):
        with self.lock:
            self.answers, self.seen, self.pending = [], set(), []
        if self.ws is not None:
            try:
                sh = self.ws.spreadsheet
                self.ws.update_title(f"archiv_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                self.ws = sh.add_worksheet("antworten", rows=1000, cols=4)
                self.ws.append_row(self.HEADER)
            except Exception as e:
                self.sheet_error = str(e)


@st.cache_resource
def get_store(version):
    return Store()


# Version erhoehen, wenn sich die Klasse Store aendert: dann wird der Speicher neu angelegt
STORE = get_store(2)


# ---------- Hilfsfunktionen ----------
def fmt(x):
    return f"{x:.1f}"


def pct(a, n):
    return round(100 * a / n) if n else 0


def split_bar(p, height=30, font=14):
    """Balken relevant (rot) gegen nicht relevant (blau), als ein HTML-Block ohne Einrueckung."""
    left = f"{p}% relevant" if p >= 22 else (f"{p}%" if p >= 12 else "")
    right = f"{100 - p}% nicht" if 100 - p >= 22 else (f"{100 - p}%" if 100 - p >= 12 else "")
    return (f'<div style="display:flex;height:{height}px;border-radius:4px;overflow:hidden;font-size:{font}px;'
            f'font-weight:700;color:#fff;font-family:Arial,sans-serif">'
            f'<div style="width:{p}%;background:{RED_D};display:flex;align-items:center;padding:0 {8 if p else 0}px;white-space:nowrap;overflow:hidden">{left}</div>'
            f'<div style="width:{100 - p}%;background:{NAVY};display:flex;align-items:center;padding:0 {8 if p < 100 else 0}px;white-space:nowrap;overflow:hidden">{right}</div></div>')


def join_url():
    url = secret("app_url")
    if url:
        return str(url)
    try:
        host = st.context.headers.get("Host", "")
    except Exception:
        host = ""
    if not host or host.startswith(("localhost", "127.")):
        return f"http://{host or 'localhost:8501'}/"
    return f"https://{host}/"


@st.cache_data
def qr_png(url):
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=2)
    return buf.getvalue()


BASE_CSS = """
<style>
#MainMenu, header[data-testid="stHeader"], footer, [data-testid="stToolbar"], [data-testid="stStatusWidget"] {display:none !important;}
html, body, [class*="css"] {font-family: Arial, Helvetica, sans-serif;}
.kicker {font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#C00000;margin:0;}
.card {background:#F2F2F2;border-radius:6px;padding:14px 16px;margin:6px 0;}
.big {font-size:40px;font-weight:700;color:#1A2B4C;line-height:1.05;margin:0;}
.mm {font-size:46px;font-weight:700;color:#1A2B4C;text-align:center;margin:0;}
.tag {display:inline-block;color:#fff;font-weight:700;font-size:14px;padding:4px 9px;border-radius:4px;}
</style>
"""
st.markdown(BASE_CSS, unsafe_allow_html=True)


# =====================================================================
# Handy
# =====================================================================
def play():
    st.markdown('<style>.block-container{padding-top:1rem;max-width:560px;}'
                'div.stButton > button{min-height:56px;font-size:19px;font-weight:700;}</style>',
                unsafe_allow_html=True)
    s = st.session_state
    s.setdefault("phase", "start")

    def go_to(phase, **kw):
        s.phase = phase
        for k, v in kw.items():
            s[k] = v

    def new_session():
        picks = []
        n = min(ROUND2_PER_PERSON, len(ROUND2))
        for k in range(n):  # ueber die ganze Spanne verteilt
            lo, hi = (k * len(ROUND2)) // n, ((k + 1) * len(ROUND2)) // n
            picks.append(ROUND2[random.randrange(lo, max(lo + 1, hi))])
        random.shuffle(picks)
        r1 = ROUND1[:]
        random.shuffle(r1)
        go_to("r1intro", pid=str(uuid.uuid4()), round1=r1, round2=picks, i=0, r1=[], r2=[], moved=False)

    def answer_r1(value):
        iid = s.round1[s.i]
        STORE.add(s.pid, iid, value)
        s.r1.append({"id": iid, "value": value, "truth": IMAGES[iid]["truth"]})
        s.phase = "r1fb"

    def next_r1():
        s.i += 1
        s.phase = "r1q" if s.i < len(s.round1) else "r1sum"

    def answer_r2():
        iid = s.round2[s.i]
        value = round(float(s.slider), 1)
        STORE.add(s.pid, iid, value)
        s.r2.append({"id": iid, "value": value, "truth": IMAGES[iid]["truth"]})
        s.phase = "r2fb"

    def next_r2():
        s.i += 1
        s.moved = False
        s.slider = float(MAX_MM) / 2
        s.phase = "r2q" if s.i < len(s.round2) else "end"

    def mark_moved():
        s.moved = True

    def progress(i, n):
        st.caption(f"Bild {i + 1} von {n}")
        st.progress(i / n)

    label = {"relevant": "relevant", "irrelevant": "nicht relevant"}
    ph = s.phase

    if ph == "start":
        st.markdown('<p class="kicker">Mitmachen</p>', unsafe_allow_html=True)
        st.title("Erkennst du, was ein Defekt ist?")
        st.write("Du siehst echte Bilder von Betonschwellen, aufgenommen vom Messzug der SBB. "
                 "In zwei kurzen Runden beurteilst du Risse wie eine Inspektorin oder ein Inspektor.")
        st.button("Los geht’s", type="primary", width="stretch", on_click=new_session)

    elif ph == "r1intro":
        st.markdown('<p class="kicker">Runde 1 von 2</p>', unsafe_allow_html=True)
        st.header("Relevant oder nicht?")
        st.write(f"Du siehst {len(s.round1)} Risse. Entscheide nach deinem Gefühl: Ist der Riss relevant oder nicht?")
        st.caption("Nach jedem Bild siehst du, wie das Fach entschieden hat.")
        st.button("Start", type="primary", width="stretch", on_click=go_to, args=("r1q",), kwargs={"i": 0})

    elif ph == "r1q":
        iid = s.round1[s.i]
        progress(s.i, len(s.round1))
        st.subheader("Ist dieser Riss relevant?")
        st.image(IMAGES[iid]["data"], width="stretch")
        c1, c2 = st.columns(2)
        c1.button("Relevant", type="primary", width="stretch", on_click=answer_r1, args=("relevant",), key=f"y{s.i}")
        c2.button("Nicht relevant", width="stretch", on_click=answer_r1, args=("irrelevant",), key=f"n{s.i}")

    elif ph == "r1fb":
        f = s.r1[-1]
        item = next(x for x in STORE.stats()["r1"] if x["id"] == f["id"])
        n = item["relevant"] + item["irrelevant"]
        progress(s.i, len(s.round1))
        st.image(IMAGES[f["id"]]["data"], width="stretch")
        same = f["value"] == f["truth"]
        color = RED_D if f["truth"] == "relevant" else NAVY
        st.markdown(f'<div class="card"><p style="font-size:20px;font-weight:700;margin:0 0 8px">'
                    f'{"Gleich wie das Fach." if same else "Das Fach sieht es anders."}</p>'
                    f'<p style="margin:0 0 6px">Du: <b>{label[f["value"]]}</b></p>'
                    f'<p style="margin:0">Das Fach: <span class="tag" style="background:{color}">{label[f["truth"]]}</span></p></div>',
                    unsafe_allow_html=True)
        st.markdown(f'<div class="card"><p style="margin:0 0 8px"><b>Das Publikum bisher</b> '
                    f'<span style="color:#555">({n} {"Antwort" if n == 1 else "Antworten"})</span></p>'
                    f'{split_bar(pct(item["relevant"], n))}</div>', unsafe_allow_html=True)
        st.button("Nächstes Bild" if s.i + 1 < len(s.round1) else "Auswertung", type="primary",
                  width="stretch", on_click=next_r1)

    elif ph == "r1sum":
        same = sum(a["value"] == a["truth"] for a in s.r1)
        st.markdown('<p class="kicker">Runde 1 geschafft</p>', unsafe_allow_html=True)
        st.markdown(f'<p style="font-size:40px;font-weight:700;color:#1A2B4C;line-height:1.05;margin:0">{same} von {len(s.r1)}</p>', unsafe_allow_html=True)
        st.write("Bilder hast du gleich beurteilt wie das Fach.")
        st.markdown('<div class="card"><p style="font-size:20px;font-weight:700;margin:0 0 6px">Was macht den Unterschied?</p>'
                    '<p style="margin:0">Ein wichtiges Kriterium ist die <b>Rissbreite</b>. '
                    'Aber wie gut lässt sie sich von Auge schätzen?</p></div>', unsafe_allow_html=True)
        st.button("Runde 2 starten", type="primary", width="stretch", on_click=go_to, args=("r2q",),
                  kwargs={"i": 0, "moved": False, "slider": float(MAX_MM) / 2})

    elif ph == "r2q":
        iid = s.round2[s.i]
        if s.i == 0:
            st.markdown('<p class="kicker">Runde 2 von 2</p>', unsafe_allow_html=True)
        progress(s.i, len(s.round2))
        st.subheader("Wie breit ist dieser Riss?")
        st.image(IMAGES[iid]["data"], width="stretch")
        val = s.get("slider", MAX_MM / 2)
        st.markdown(f'<p style="font-size:46px;font-weight:700;color:#1A2B4C;text-align:center;margin:0">{fmt(val)} <span style="font-size:22px">mm</span></p>',
                    unsafe_allow_html=True)
        st.slider("Schiebe den Regler auf deine Schätzung (mm)", 0.0, float(MAX_MM), step=0.1,
                  key="slider", format="%.1f", on_change=mark_moved)
        st.button("Schätzung abgeben", type="primary", width="stretch",
                  on_click=answer_r2, key=f"ok{s.i}")

    elif ph == "r2fb":
        f = s.r2[-1]
        vals = next(x for x in STORE.stats()["r2"] if x["id"] == f["id"])["values"]
        mean = sum(vals) / len(vals) if vals else f["value"]
        diff = abs(f["value"] - f["truth"])
        progress(s.i, len(s.round2))
        st.image(IMAGES[f["id"]]["data"], width="stretch")
        how = "<b>, genau getroffen!</b>" if diff < 0.05 else f'({fmt(diff)} mm {"zu breit" if f["value"] > f["truth"] else "zu schmal"})'
        st.markdown(f'<div class="card"><p style="margin:0">Rissbreite</p><p style="font-size:40px;font-weight:700;color:#1A2B4C;line-height:1.05;margin:0">{fmt(f["truth"])} mm</p>'
                    f'<p style="margin:8px 0 4px">Deine Schätzung: <b>{fmt(f["value"])} mm</b> {how}</p>'
                    f'<p style="margin:0;color:#555;font-size:15px">Publikum im Schnitt: {fmt(mean)} mm '
                    f'({len(vals)} {"Schätzung" if len(vals) == 1 else "Schätzungen"})</p></div>', unsafe_allow_html=True)
        st.button("Nächster Riss" if s.i + 1 < len(s.round2) else "Meine Bilanz", type="primary",
                  width="stretch", on_click=next_r2)

    elif ph == "end":
        same = sum(a["value"] == a["truth"] for a in s.r1)
        err = sum(abs(a["value"] - a["truth"]) for a in s.r2) / max(1, len(s.r2))
        st.markdown('<p class="kicker">Deine Bilanz</p>', unsafe_allow_html=True)
        st.markdown(f'<div class="card"><p style="margin:0 0 6px">Runde 1: <b>{same} von {len(s.r1)}</b> gleich wie das Fach</p>'
                    f'<p style="margin:0">Runde 2: im Schnitt <b>{fmt(err)} mm</b> daneben</p></div>', unsafe_allow_html=True)
        st.subheader("Genau darum geht es bei AISI.")
        st.write("Wenn schon Menschen unterschiedlich urteilen, lernt ein Modell diese Widersprüche mit. "
                 "Deshalb legen wir die Kriterien gemeinsam mit dem Fach fest und messen die Rissbreite, statt sie zu schätzen.")
        st.caption("Schau auf den Bildschirm am Stand: Dort siehst du, wie alle anderen geurteilt haben.")
        st.button("Nochmals spielen", width="stretch", on_click=go_to, args=("start",))


# =====================================================================
# Grossbildschirm: eine Seite, oben die Zahlen, darunter live beide Runden
# =====================================================================
def screen():
    st.markdown('<style>.block-container{padding:1.2rem 2rem 2rem 2rem;max-width:100%;}</style>', unsafe_allow_html=True)
    top, side = st.columns([5, 1], gap="large")
    with side:
        url = join_url()
        st.markdown(f'<div style="background:{NAVY};color:#fff;border-radius:6px;padding:10px;text-align:center;'
                    f'font-size:22px;font-weight:700">Mach mit!</div>', unsafe_allow_html=True)
        st.image(qr_png(url), width="stretch")
        st.caption("QR-Code scannen und auf dem Handy mitspielen")
    with top:
        st.markdown('<p class="kicker" style="font-size:15px">AISI · AI in der Streckeninspektion</p>'
                    '<h1 style="margin:0 0 4px;font-size:2.6rem">Erkennst du, was ein Defekt ist?</h1>'
                    '<p style="font-size:20px;color:#333;margin:0 0 12px">Beurteile echte Risse in Betonschwellen, '
                    'aufgenommen vom Messzug. Hier siehst du live, wie alle geurteilt haben.</p>', unsafe_allow_html=True)
        kpis()
    results()


def card(value, label):
    return (f'<div class="card" style="padding:20px 22px"><p style="font-size:52px;font-weight:700;color:{NAVY};'
            f'margin:0;line-height:1">{value}</p><p style="font-size:19px;margin:6px 0 0">{label}</p></div>')


@st.fragment(run_every="4s")
def kpis():
    stt = STORE.stats()
    r1n = sum(x["relevant"] + x["irrelevant"] for x in stt["r1"])
    r2n = sum(len(x["values"]) for x in stt["r2"])
    c = st.columns(3)
    c[0].markdown(card(stt["participants"], "Personen haben mitgemacht"), unsafe_allow_html=True)
    c[1].markdown(card(r1n, "Risse beurteilt (Runde 1)"), unsafe_allow_html=True)
    c[2].markdown(card(r2n, "Rissbreiten geschätzt (Runde 2)"), unsafe_allow_html=True)


def section(title, sub):
    st.markdown(f'<h2 style="margin:28px 0 2px;font-size:2rem;border-top:3px solid {NAVY};padding-top:16px">{title}</h2>'
                f'<p style="font-size:19px;color:#333;margin:0 0 12px">{sub}</p>', unsafe_allow_html=True)


def mixed(ids):
    """Feste, gemischte Reihenfolge, damit die Anordnung nichts verraet (nicht nach Fach oder Breite sortiert)."""
    return sorted(ids, key=lambda i: zlib.crc32(("aisi-" + i).encode()))


@st.fragment(run_every="4s")
def results():
    stt = STORE.stats()
    reveal = getattr(STORE, "reveal", False)

    # ---------- Runde 1 ----------
    by_id = {x["id"]: x for x in stt["r1"]}
    items = []
    for k, iid in enumerate(mixed(list(by_id)), start=1):
        x = by_id[iid]
        n = x["relevant"] + x["irrelevant"]
        p = pct(x["relevant"], n)
        majority = None if n == 0 or p == 50 else ("relevant" if p > 50 else "irrelevant")
        items.append({**x, "nr": k, "n": n, "p": p, "majority": majority})
    judged = [x for x in items if x["n"]]
    total = sum(x["n"] for x in judged)
    contested = min(judged, key=lambda x: abs(x["p"] - 50)) if judged else None
    if not total:
        sub = "Noch keine Antworten."
    elif reveal:
        agree_all = sum(x["relevant"] if x["truth"] == "relevant" else x["irrelevant"] for x in judged)
        sub = (f"<b>{pct(agree_all, total)} %</b> aller Urteile stimmen mit dem Fach überein. "
               "Je näher ein Balken an 50 zu 50 ist, desto uneiniger ist das Publikum. Genau solche Bilder machen es auch einem Modell schwer.")
    else:
        sub = ("Je näher ein Balken an 50 zu 50 ist, desto uneiniger ist das Publikum. "
               "Genau solche Bilder machen es auch einem Modell schwer.")
    section("Runde 1 · Ist der Riss relevant?", sub)
    legend = ("Balken = Anteil der Antworten des Publikums. Etikett oben = Urteil des Fachs." if reveal else
              "Balken = Anteil der Antworten des Publikums. Wie das Fach entschieden hat, siehst du auf deinem Handy, nachdem du geantwortet hast.")
    st.markdown(f'<p style="font-size:16px;margin:0 0 8px"><span class="tag" style="background:{RED_D}">relevant</span> '
                f'<span class="tag" style="background:{NAVY}">nicht relevant</span> &nbsp;{legend}</p>', unsafe_allow_html=True)
    cols = st.columns(len(items)) if items else []
    for c, x in zip(cols, items):
        with c:
            st.image(IMAGES[x["id"]]["thumb"], width="stretch")
            if reveal:
                fach = "relevant" if x["truth"] == "relevant" else "nicht relevant"
                color = RED_D if x["truth"] == "relevant" else NAVY
                top = f'<span class="tag" style="background:{color};font-size:12px">Fach: {fach}</span>'
            else:
                top = f'<span class="tag" style="background:#555;font-size:12px">Bild {x["nr"]}</span>'
            verdict = ""
            if x["n"]:
                bar = (f'<div style="display:flex;height:16px;border-radius:3px;overflow:hidden;margin:6px 0 4px">'
                       f'<div style="width:{x["p"]}%;background:{RED_D}"></div><div style="width:{100 - x["p"]}%;background:{NAVY}"></div></div>'
                       f'<p style="margin:0;font-size:14px;line-height:1.35"><b style="color:{RED_D}">{x["p"]} %</b> relevant<br>'
                       f'<b style="color:{NAVY}">{100 - x["p"]} %</b> nicht relevant</p>')
                if reveal:
                    if x["majority"] is None:
                        verdict = '<p style="margin:4px 0 0;font-size:14px;font-weight:700;color:#8A6200">Unentschieden</p>'
                    elif x["majority"] == x["truth"]:
                        verdict = f'<p style="margin:4px 0 0;font-size:14px;font-weight:700;color:{GREEN}">✓ Mehrheit wie Fach</p>'
                    else:
                        verdict = f'<p style="margin:4px 0 0;font-size:14px;font-weight:700;color:{RED_D}">✗ Mehrheit anders als Fach</p>'
            else:
                bar = '<p style="margin:6px 0 0;font-size:14px;color:#555">noch keine Antworten</p>'
            hot = (f'<p style="margin:4px 0 0"><span class="tag" style="background:{AMBER};color:#1F1F1F;font-size:12px">'
                   f'Am umstrittensten</span></p>' if contested and x["id"] == contested["id"] and x["n"] >= 3 else "")
            st.markdown(f'<div style="margin-top:-6px">{top}{bar}{verdict}<p style="margin:2px 0 0;font-size:13px;color:#555">'
                        f'{x["n"]} {"Antwort" if x["n"] == 1 else "Antworten"}</p>{hot}</div>', unsafe_allow_html=True)

    # ---------- Runde 2 ----------
    by_id = {x["id"]: x for x in stt["r2"]}
    r2 = sorted(stt["r2"], key=lambda x: x["truth"]) if reveal else [by_id[i] for i in mixed(list(by_id))]
    n_est = sum(len(x["values"]) for x in r2)
    errs = [abs(v - x["truth"]) for x in r2 for v in x["values"]]
    if not n_est:
        sub = "Noch keine Schätzungen."
    elif reveal:
        sub = (f"Im Schnitt liegen die Schätzungen <b>{fmt(sum(errs) / len(errs))} mm</b> neben der tatsächlichen Breite "
               f"({n_est} Schätzungen). Jede Spalte ist ein Riss: Rot ist die tatsächliche Breite, "
               "die blauen Punkte sind die Schätzungen, der orange Strich ist ihr Mittelwert.")
    else:
        sub = (f"So unterschiedlich schätzen wir ({n_est} Schätzungen). Jede Spalte ist ein Riss: Die blauen Punkte sind die "
               "Schätzungen, der orange Strich ist ihr Mittelwert. Je weiter die Punkte auseinanderliegen, desto uneiniger sind wir. "
               "Die tatsächliche Breite siehst du auf deinem Handy, nachdem du geschätzt hast.")
    section("Runde 2 · Wie breit ist der Riss?", sub)
    if not r2:
        return
    xs = list(range(1, len(r2) + 1))
    labels = [f"Riss {i}<br>{fmt(x['truth'])} mm" if reveal else f"Riss {i}" for i, x in zip(xs, r2)]
    fig = go.Figure()
    if reveal:  # Abstand zwischen Mittelwert und Wahrheit als graue Linie
        for i, x in zip(xs, r2):
            if x["values"]:
                m = sum(x["values"]) / len(x["values"])
                fig.add_trace(go.Scatter(x=[i, i], y=[x["truth"], m], mode="lines", line=dict(color="#B0B0B0", width=3),
                                         showlegend=False, hoverinfo="skip"))
    rng = random.Random(7)
    fig.add_trace(go.Scatter(x=[i + rng.uniform(-0.18, 0.18) for i, x in zip(xs, r2) for _ in x["values"]],
                             y=[v for x in r2 for v in x["values"]], mode="markers", name="Schätzung einer Person",
                             marker=dict(color=NAVY, opacity=0.65, size=10)))
    fig.add_trace(go.Scatter(x=[i for i, x in zip(xs, r2) if x["values"]],
                             y=[sum(x["values"]) / len(x["values"]) for x in r2 if x["values"]],
                             mode="markers", name="Mittelwert Publikum",
                             marker=dict(color=AMBER, symbol="line-ew", size=34, line=dict(color=AMBER, width=5))))
    if reveal:
        fig.add_trace(go.Scatter(x=xs, y=[x["truth"] for x in r2], mode="markers", name="Tatsächliche Rissbreite",
                                 marker=dict(color=RED, symbol="diamond", size=15, line=dict(color="white", width=1.5))))
    fig.update_layout(
        height=520, margin=dict(l=70, r=20, t=50, b=90), plot_bgcolor="white",
        font=dict(family="Arial", size=16, color="#1F1F1F"),
        legend=dict(orientation="h", x=0, y=1.1, font=dict(size=17)),
        xaxis=dict(tickmode="array", tickvals=xs, ticktext=labels, range=[0.4, len(r2) + 0.6],
                   title=dict(text="Die Risse, sortiert nach tatsächlicher Breite" if reveal else "Die Risse", font=dict(size=17)),
                   showgrid=False, tickfont=dict(size=14, color="#1F1F1F")),
        yaxis=dict(range=[0, MAX_MM + 0.2], dtick=1, gridcolor="#E3E3E3", zeroline=False,
                   title=dict(text="Geschätzte Rissbreite in mm" if not reveal else "Rissbreite in mm", font=dict(size=17)),
                   tickfont=dict(size=15, color="#1F1F1F")),
    )
    st.plotly_chart(fig, theme=None, config={"displayModeBar": False, "staticPlot": True}, key="r2chart")


# =====================================================================
# Moderation
# =====================================================================
def admin():
    if st.query_params.get("key", "") != ADMIN_KEY:
        st.error("Moderation: Link mit ?view=admin&key=... öffnen.")
        return
    st.title("AISI Marktstand · Moderation")
    stt = STORE.stats()
    c1, c2, c3 = st.columns(3)
    c1.metric("Teilnahmen", stt["participants"])
    c2.metric("Antworten", stt["answers"])
    c3.metric("Google Sheets", "verbunden" if STORE.ws is not None and not STORE.sheet_error else "nicht aktiv")
    if STORE.sheet_error:
        st.warning(f"Google Sheets: {STORE.sheet_error}")

    st.caption(f"Grossbildschirm: {join_url()}?view=screen (im Browser mit F11 auf Vollbild)")

    def set_reveal():
        STORE.reveal = st.session_state.reveal_toggle

    st.toggle("Auflösung auf dem Grossbildschirm zeigen (Urteil des Fachs und tatsächliche Rissbreiten)",
              value=getattr(STORE, "reveal", False), key="reveal_toggle", on_change=set_reveal)
    st.caption("Standard ist aus: Der Bildschirm zeigt nur, wie das Publikum geurteilt hat, damit niemand die Lösungen abschaut.")

    st.subheader("Runde 1 · Relevant oder nicht?")
    st.dataframe([{"Bild": x["id"], "Fach": x["truth"], "relevant": x["relevant"], "nicht relevant": x["irrelevant"],
                   "Einigkeit": f'{pct(max(x["relevant"], x["irrelevant"]), x["relevant"] + x["irrelevant"])}%'}
                  for x in stt["r1"]], hide_index=True, width="stretch")
    st.subheader("Runde 2 · Rissbreite")
    st.dataframe([{"Bild": x["id"], "Breite (mm)": x["truth"], "Schätzungen": len(x["values"]),
                   "Mittelwert (mm)": round(sum(x["values"]) / len(x["values"]), 1) if x["values"] else None,
                   "Abweichung (mm)": round(sum(abs(v - x["truth"]) for v in x["values"]) / len(x["values"]), 1) if x["values"] else None}
                  for x in stt["r2"]], hide_index=True, width="stretch")

    st.subheader("Daten")
    st.download_button("Antworten als CSV", STORE.csv(), file_name="antworten.csv", mime="text/csv")
    confirm = st.checkbox("Ja, alle Antworten zurücksetzen (im Google Sheet werden sie archiviert)")
    if st.button("Neu starten", type="primary", disabled=not confirm):
        STORE.reset()
        st.success("Zurückgesetzt.")


if VIEW == "screen":
    screen()
elif VIEW == "admin":
    admin()
else:
    play()
