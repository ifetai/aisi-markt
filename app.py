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
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import segno
import streamlit as st
from PIL import Image

ROOT = Path(__file__).parent
CONTENT = ROOT / "content"
ROUND2_PER_PERSON = 5
RED, RED_D, NAVY, AMBER, GREY = "#EB0000", "#C00000", "#1A2B4C", "#F0A800", "#F2F2F2"

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
                images[iid] = {"id": iid, "round": 1, "truth": verdict, "data": prepared(f), "wide": prepared(f, 800, 2.9)}
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
        self.screen_mode = "auto"
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
def get_store():
    return Store()


STORE = get_store()


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
        val = s.get("slider", MAX_MM / 2) if s.moved else None
        st.markdown(f'<p style="font-size:46px;font-weight:700;color:#1A2B4C;text-align:center;margin:0">{fmt(val) if val is not None else "?"} <span style="font-size:22px">mm</span></p>',
                    unsafe_allow_html=True)
        st.slider("Schiebe den Regler auf deine Schätzung (mm)", 0.0, float(MAX_MM), step=0.1,
                  key="slider", format="%.1f", on_change=mark_moved)
        st.button("Schätzung abgeben", type="primary", width="stretch", disabled=not s.moved,
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
# Grossbildschirm
# =====================================================================
VIEWS = ["start", "runde1", "runde2", "augenmass"]
VIEW_SECONDS = 15


def screen():
    st.markdown('<style>.block-container{padding:1.2rem 2rem 0 2rem;max-width:100%;}'
                'h1{font-size:2.6rem !important;}</style>', unsafe_allow_html=True)
    main, side = st.columns([4, 1], gap="large")
    with side:
        url = join_url()
        st.markdown(f'<div style="background:{NAVY};color:#fff;border-radius:6px;padding:18px;text-align:center">'
                    f'<p style="font-size:26px;font-weight:700;margin:0 0 10px">Mach mit!</p></div>', unsafe_allow_html=True)
        st.image(qr_png(url), width="stretch")
        st.caption(url)
        counter()
    with main:
        board()


@st.fragment(run_every="3s")
def counter():
    n = STORE.stats()["participants"]
    st.markdown(f'<p style="font-size:64px;font-weight:700;color:{AMBER};margin:0;text-align:center;line-height:1">{n}</p>'
                f'<p style="text-align:center;margin:0">haben mitgemacht</p>', unsafe_allow_html=True)


@st.fragment(run_every="5s")
def board():
    stt = STORE.stats()
    mode = STORE.screen_mode
    now = int(time.time())
    view = VIEWS[(now // VIEW_SECONDS) % len(VIEWS)] if mode == "auto" else mode

    def head(kicker, title, sub=""):
        st.markdown(f'<p class="kicker" style="font-size:15px">{kicker}</p>', unsafe_allow_html=True)
        st.markdown(f'<h1 style="margin:0 0 4px">{title}</h1>', unsafe_allow_html=True)
        if sub:
            st.markdown(f'<p style="font-size:20px;color:#333;margin:0 0 12px">{sub}</p>', unsafe_allow_html=True)

    if view == "start":
        r1n = sum(x["relevant"] + x["irrelevant"] for x in stt["r1"])
        r2n = sum(len(x["values"]) for x in stt["r2"])
        head("AISI · AI in der Streckeninspektion", "Erkennst du, was ein Defekt ist?",
             "Beurteile echte Risse in Betonschwellen, aufgenommen vom Messzug. Scanne den QR-Code mit deinem Handy.")
        cols = st.columns(3)
        for c, (v, l) in zip(cols, [(stt["participants"], "Personen haben mitgemacht"), (r1n, "Risse beurteilt"),
                                     (r2n, "Rissbreiten geschätzt")]):
            c.markdown(f'<div class="card" style="padding:28px 22px"><p style="font-size:64px;font-weight:700;color:{NAVY};'
                       f'margin:0;line-height:1">{v}</p><p style="font-size:20px;margin:6px 0 0">{l}</p></div>',
                       unsafe_allow_html=True)

    elif view == "runde1":
        head("Runde 1 · Relevant oder nicht?", "So urteilt das Publikum",
             "Je näher an 50 zu 50, desto uneiniger sind wir. Genau solche Bilder machen es auch einem Modell schwer.")
        items = []
        for x in stt["r1"]:
            n = x["relevant"] + x["irrelevant"]
            p = pct(x["relevant"], n)
            items.append({**x, "n": n, "p": p, "agree": max(p, 100 - p) if n else 101})
        items.sort(key=lambda x: x["agree"])
        hot = items[0]["id"] if items and items[0]["n"] >= 3 else None
        for row in range(0, len(items), 3):
            cols = st.columns(3)
            for c, x in zip(cols, items[row:row + 3]):
                with c:
                    st.image(IMAGES[x["id"]]["wide"], width="stretch")
                    color = RED_D if x["truth"] == "relevant" else NAVY
                    badge = (f' <span class="tag" style="background:{AMBER};color:#1F1F1F">Am umstrittensten</span>'
                             if x["id"] == hot else "")
                    bar = split_bar(x["p"], height=26, font=13) if x["n"] else ""
                    st.markdown(f'<div style="margin:-8px 0 10px"><span class="tag" style="background:{color}">'
                                f'Fach: {"relevant" if x["truth"] == "relevant" else "nicht relevant"}</span>{badge}'
                                f'<div style="margin-top:6px">{bar}</div><p style="color:#555;font-size:13px;margin:4px 0 0">'
                                f'{x["n"]} {"Antwort" if x["n"] == 1 else "Antworten"}</p></div>', unsafe_allow_html=True)

    elif view == "runde2":
        items = [x for x in stt["r2"] if x["values"]]
        head("Runde 2 · Wie breit ist der Riss?", "Augenmass gegen Messung",
             "Jeder Balken zeigt, wie viele so geschätzt haben. Rot ist die tatsächliche Rissbreite.")
        if not items:
            st.info("Noch keine Schätzungen. Mach den Anfang!")
            return
        x = items[(now // 8) % len(items)]
        c1, c2 = st.columns([1.2, 1])
        c1.image(IMAGES[x["id"]]["data"], width="stretch")
        mean = sum(x["values"]) / len(x["values"])
        fig = go.Figure(go.Histogram(x=x["values"], xbins=dict(start=0, end=MAX_MM, size=0.25), marker_color=NAVY,
                                     name="Schätzungen"))
        fig.add_vline(x=x["truth"], line_color=RED, line_width=5,
                      annotation_text=f"Rissbreite {fmt(x['truth'])} mm", annotation_font=dict(color=RED_D, size=18))
        fig.add_vline(x=mean, line_color=AMBER, line_width=4, line_dash="dash",
                      annotation_text=f"Publikum {fmt(mean)} mm", annotation_position="bottom right",
                      annotation_font=dict(color="#8A6200", size=16))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=40), plot_bgcolor="white", bargap=0.08,
                          xaxis=dict(range=[0, MAX_MM], title="Schätzung in mm", dtick=1), yaxis=dict(title="Anzahl", gridcolor="#E3E3E3"),
                          font=dict(family="Arial", size=16), showlegend=False)
        c2.plotly_chart(fig, config={"displayModeBar": False, "staticPlot": True}, key=f"h{now}")

    elif view == "augenmass":
        items = [x for x in stt["r2"] if x["values"]]
        errs = [abs(v - x["truth"]) for x in items for v in x["values"]]
        head("Gesamtbild", "Wie gut ist unser Augenmass?",
             f"Im Schnitt liegen wir <b>{fmt(sum(errs) / len(errs))} mm</b> daneben. Auf der Diagonalen wäre jede Schätzung genau richtig."
             if errs else "")
        if not items:
            st.info("Sobald Schätzungen da sind, erscheint hier das Gesamtbild.")
            return
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, MAX_MM], y=[0, MAX_MM], mode="lines", line=dict(color="#1F1F1F", dash="dash", width=2),
                                 name="genau richtig"))
        fig.add_trace(go.Scatter(x=[x["truth"] for x in items for _ in x["values"]], y=[v for x in items for v in x["values"]],
                                 mode="markers", marker=dict(color=NAVY, opacity=0.3, size=9), name="einzelne Schätzung"))
        fig.add_trace(go.Scatter(x=[x["truth"] for x in items], y=[sum(x["values"]) / len(x["values"]) for x in items],
                                 mode="markers", marker=dict(color=RED, size=16, line=dict(color="white", width=2)),
                                 name="Mittelwert pro Riss"))
        fig.update_layout(height=560, margin=dict(l=10, r=10, t=10, b=40), plot_bgcolor="white",
                          xaxis=dict(range=[0, MAX_MM], title="Tatsächliche Rissbreite in mm", dtick=1, gridcolor="#E3E3E3"),
                          yaxis=dict(range=[0, MAX_MM], title="Geschätzt in mm", dtick=1, gridcolor="#E3E3E3"),
                          font=dict(family="Arial", size=16), legend=dict(orientation="h", y=1.08))
        st.plotly_chart(fig, config={"displayModeBar": False, "staticPlot": True}, key=f"a{now}")


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

    st.subheader("Grossbildschirm")
    modes = {"auto": "Automatisch", "start": "Start", "runde1": "Runde 1", "runde2": "Runde 2", "augenmass": "Augenmass"}
    def set_mode():
        STORE.screen_mode = st.session_state.mode

    st.radio("Ansicht", list(modes), index=list(modes).index(STORE.screen_mode), key="mode",
             format_func=modes.get, horizontal=True, on_change=set_mode)
    st.caption(f"Bildschirm: {join_url()}?view=screen (im Browser mit F11 auf Vollbild)")

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
