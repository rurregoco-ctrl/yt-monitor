"""Monitor de canales de YouTube.

Revisa los feeds RSS de cada canal, detecta videos nuevos, genera un resumen
con Claude y envía el informe por ntfy (push al iPhone) y WhatsApp (CallMeBot).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests

# ---------------------------------------------------------------- CONFIG ----
# Agrega aquí los canales que faltan (solo el @handle, sin la @).
CHANNELS = [
    "beardfm",
    "Tecnonauta",
    "DanielCoronellPeriodista",
    "la_inteligencia_artificial",
    "chicageek",
    "IdearVlog",
    "HaylsWorld",
    "MarcianoTech",
    "jaimebayly1",
    "LaMMordida",
    "mkbhd",
]

MODEL = "claude-haiku-5-5"
MAX_AGE_HOURS = 48      # ignora videos publicados hace más de esto
SKIP_SHORTS = True      # no notificar YouTube Shorts
STATE_FILE = "state.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
WA_PHONE = os.environ.get("CALLMEBOT_PHONE", "")
WA_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
COOKIES = {"CONSENT": "YES+1"}
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "m": "http://search.yahoo.com/mrss/",
}


# ----------------------------------------------------------------- ESTADO ----
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("channel_ids", {})   # handle -> channel_id
    state.setdefault("initialized", [])   # channel_ids ya "sembrados"
    state.setdefault("seen", [])          # ids de videos ya procesados
    return state


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- YOUTUBE ----
def resolve_channel_id(handle):
    r = requests.get(
        f"https://www.youtube.com/@{handle}",
        headers=HEADERS, cookies=COOKIES, timeout=25,
    )
    r.raise_for_status()
    patterns = [
        r"feeds/videos\.xml\?channel_id=(UC[\w-]{22})",
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'"externalId":"(UC[\w-]{22})"',
    ]
    for p in patterns:
        m = re.search(p, r.text)
        if m:
            return m.group(1)
    raise RuntimeError(f"No pude resolver el ID de @{handle}")


def fetch_feed(channel_id):
    r = requests.get(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        headers=HEADERS, timeout=25,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    videos = []
    for e in root.findall("a:entry", NS):
        desc_el = e.find("m:group/m:description", NS)
        videos.append({
            "id": e.find("yt:videoId", NS).text,
            "title": e.find("a:title", NS).text or "",
            "channel": e.find("a:author/a:name", NS).text or "",
            "published": datetime.fromisoformat(e.find("a:published", NS).text),
            "description": (desc_el.text or "") if desc_el is not None else "",
            "url": e.find("a:link", NS).attrib["href"],
        })
    return videos


def is_short(video_id):
    try:
        r = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            headers=HEADERS, cookies=COOKIES, allow_redirects=False, timeout=15,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def get_transcript(video_id):
    """Devuelve la transcripción o '' si no está disponible."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["es", "en"])
        text = " ".join(s.text for s in fetched)
        return text[:30000]
    except Exception as exc:  # YouTube suele bloquear IPs de la nube
        print(f"  (sin transcripción: {type(exc).__name__})")
        return ""


# ------------------------------------------------------------------ RESUMEN --
def summarize(video, transcript):
    fallback = video["description"].strip().replace("\n", " ")[:280]
    if not ANTHROPIC_KEY:
        return fallback or "Sin resumen disponible."
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        source = transcript if transcript else video["description"]
        basis = "transcripción" if transcript else "descripción"
        prompt = (
            "Eres un asistente que prepara avisos breves de videos nuevos.\n"
            f"Canal: {video['channel']}\nTítulo: {video['title']}\n"
            f"Texto base ({basis} del video):\n{source[:25000]}\n\n"
            "Escribe en español, sin markdown ni encabezados:\n"
            "1) Un resumen de máximo 2 frases.\n"
            "2) Una línea en blanco.\n"
            "3) Hasta 3 puntos clave, cada uno en una línea que empiece con '• '.\n"
            "No inventes datos que no estén en el texto base. Si el texto base es "
            "muy escaso, resume solo lo que se sabe y hazlo corto."
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not transcript:
            text += "\n(Resumen basado solo en la descripción)"
        return text
    except Exception as exc:
        print(f"  (falló el resumen: {type(exc).__name__}: {exc})")
        return fallback or "Sin resumen disponible."


# ------------------------------------------------------------ NOTIFICACIONES -
def notify_ntfy(video, summary):
    if not NTFY_TOPIC:
        return True
    r = requests.post(
        "https://ntfy.sh/",
        json={
            "topic": NTFY_TOPIC,
            "title": f"📺 {video['channel']}: {video['title']}"[:200],
            "message": f"{summary}\n\n▶️ {video['url']}"[:3500],
            "click": video["url"],
            "tags": ["tv"],
        },
        timeout=20,
    )
    return r.ok


def notify_whatsapp(video, summary):
    if not (WA_PHONE and WA_APIKEY):
        return
    text = (
        f"📺 *{video['channel']}*\n"
        f"🎬 {video['title']}\n\n"
        f"{summary}\n\n"
        f"▶️ {video['url']}"
    )
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": WA_PHONE, "text": text[:3500], "apikey": WA_APIKEY},
            timeout=30,
        )
        if not r.ok:
            print(f"  WhatsApp falló: {r.status_code} {r.text[:120]}")
    except requests.RequestException as exc:
        print(f"  WhatsApp falló: {exc}")


# -------------------------------------------------------------------- MAIN ---
def main():
    state = load_state()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    sent = 0

    for handle in CHANNELS:
        try:
            cid = state["channel_ids"].get(handle)
            if not cid:
                cid = resolve_channel_id(handle)
                state["channel_ids"][handle] = cid
            videos = fetch_feed(cid)
        except Exception as exc:
            print(f"[{handle}] error: {exc}")
            continue

        # Primera vez que vemos este canal: solo "sembramos" lo existente
        if cid not in state["initialized"]:
            for v in videos:
                if v["id"] not in state["seen"]:
                    state["seen"].append(v["id"])
            state["initialized"].append(cid)
            print(f"[{handle}] inicializado ({len(videos)} videos existentes)")
            continue

        new = [v for v in videos if v["id"] not in state["seen"]]
        for v in sorted(new, key=lambda x: x["published"]):
            print(f"[{handle}] nuevo: {v['title']}")
            if v["published"] < cutoff or (SKIP_SHORTS and is_short(v["id"])):
                state["seen"].append(v["id"])
                print("  omitido (antiguo o Short)")
                continue
            summary = summarize(v, get_transcript(v["id"]))
            if NTFY_TOPIC and not notify_ntfy(v, summary):
                print("  ntfy falló; se reintenta en la próxima ejecución")
                continue
            notify_whatsapp(v, summary)
            state["seen"].append(v["id"])
            sent += 1

    save_state(state)
    print(f"Listo. Notificaciones enviadas: {sent}")


if __name__ == "__main__":
    sys.exit(main())
