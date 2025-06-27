from fastapi import FastAPI, Request
from fastapi.responses import Response
import os, uvicorn, httpx, asyncio, datetime, traceback, pandas as pd
import aiofiles, mimetypes, tempfile, subprocess
from PyPDF2 import PdfReader
from docx import Document
import openai
from typing import Optional

# ─────────────────────────── logging helper ──────────────────────
def log(step: str, d: Optional[dict] = None):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds")
    extra = " ".join(f"{k}={v}" for k, v in (d or {}).items())
    print(f"[{ts}] {step}" + (f" | {extra}" if extra else ""))

# ─────────────────────────── env vars / clients ──────────────────
SLACK_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SS_API_KEY    = os.getenv("SLIDESPEAK_API_KEY")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")

HEAD_SLACK = {"Authorization": f"Bearer {SLACK_TOKEN}"}
HEAD_SS    = {"Content-Type": "application/json", "X-API-Key": SS_API_KEY}
openai.api_key = OPENAI_KEY
TIMEOUT = httpx.Timeout(60.0, connect=30.0)

app = FastAPI()

# health-check for Render
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return Response(status_code=200)

# ─────────────────────────── file helpers ────────────────────────
async def download_slack(url: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=HEAD_SLACK)
        if r.status_code == 200:
            ext = mimetypes.guess_extension(r.headers.get("Content-Type", "")) or ".tmp"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(r.content); tmp.close()
            return tmp.name
    return None

async def extract(path: str) -> str:
    if path.endswith(".xlsx"):
        df = pd.read_excel(path, sheet_name=0)
        return df.head(20).to_markdown(index=False)
    if path.endswith(".pdf"):
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)[:4000]
    if path.endswith(".docx"):
        return "\n".join(p.text for p in Document(path).paragraphs)[:4000]
    if path.endswith(".txt"):
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return (await f.read())[:4000]
    return ""

# ─────────────────────────── GPT & SlideSpeak ────────────────────
async def gerar_roteiro(texto: str, pedido: str) -> str:
    system = ("Você é analista. Gere JSON com: title, "
              "slides[ {heading, bullets[]} ]. Responda em JSON.")
    user = f"Pedido do usuário:\n{pedido}\n\nDados:\n{texto}"
    r = await openai.ChatCompletion.acreate(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
        response_format={"type": "json_object"}
    )
    return r.choices[0].message.content

async def slide_url(json_content: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://api.slidespeak.co/api/v1/presentation/generate",
                         headers=HEAD_SS, json={"json_content": json_content})
        task = r.json()["task_id"]
        while True:
            s = await c.get(f"https://api.slidespeak.co/api/v1/task_status/{task}",
                            headers={"X-API-Key": SS_API_KEY})
            d = s.json()
            if d["task_status"] == "SUCCESS":
                return d["task_result"]["url"]
            if d["task_status"] == "FAILED":
                return "Erro ao gerar PPT."
            await asyncio.sleep(4)

# ─────────────────────────── sessions (interactive) ──────────────
sessions = {}  # key -> {pedido, texto}
asked_flag = {}  # track if bot already asked for more details

def sess_key(ev):
    if ev.get("channel_type") == "im":
        return ev["user"]          # DM
    return ev.get("thread_ts") or ev["ts"]

# ─────────────────────────── dedup por client_msg_id ─────────────
dedup = {}
def is_duplicate(cid: str) -> bool:
    now = datetime.datetime.utcnow().timestamp()
    for k, t in list(dedup.items()):
        if now - t > 300:
            dedup.pop(k, None)
    if cid in dedup:
        return True
    dedup[cid] = now
    return False

# ─────────────────────────── Slack events ────────────────────────
@app.post("/slack/events")
async def slack_events(req: Request):
    p = await req.json()

    if p.get("type") == "url_verification":
        return Response(content=f'{{"challenge":"{p["challenge"]}"}}',
                        media_type="application/json")

    ev = p.get("event", {})
    if "bot_id" in ev:
        return {"ok": True}

    # -------- dedup --------
    cid = ev.get("client_msg_id") or ev.get("ts")
    if is_duplicate(cid):
        log("dup", {"id": cid})
        return {"ok": True}

    key = sess_key(ev)
    sess = sessions.setdefault(key, {"pedido": "", "texto": ""})

    # -------- collect pedido / texto --------
    if txt := ev.get("text"):
        sess["pedido"] += " " + txt

    if ev.get("files"):
        ftmp = await download_slack(ev["files"][0]["url_private_download"])
        if ftmp:
            sess["texto"] += "\n" + await extract(ftmp)
            log("file_added", {"chars": len(sess['texto'])})

    channel = ev["user"] if ev.get("channel_type") == "im" else ev["channel"]
    msg_low = ev.get("text", "").lower().strip()

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        # first interaction: ask for more details
        if msg_low not in {"gera", "pronto"} and not asked_flag.get(key):
            await c.post("https://slack.com/api/chat.postMessage",
                         headers=HEAD_SLACK,
                         json={"channel": channel,
                               "thread_ts": ev.get("thread_ts") or ev["ts"],
                               "text": "✅ Recebi! Envie mais detalhes ou arquivos e digite *gera* quando acabar."})
            asked_flag[key] = True
            return {"ok": True}

        # user finished -> generate
        if msg_low in {"gera", "pronto"}:
            await c.post("https://slack.com/api/chat.postMessage",
                         headers=HEAD_SLACK,
                         json={"channel": channel,
                               "thread_ts": key if ev.get("channel_type") != "im" else None,
                               "text": "⏳ Gerando apresentação, aguarde…"})
            try:
                roteiro_json = await gerar_roteiro(sess["texto"], sess["pedido"])
                url = await slide_url(roteiro_json)
            except Exception as e:
                traceback.print_exc()
                url = "Erro ao gerar PPT."

            await c.post("https://slack.com/api/chat.postMessage",
                         headers=HEAD_SLACK,
                         json={"channel": channel,
                               "thread_ts": key if ev.get("channel_type") != "im" else None,
                               "text": f"Aqui está sua apresentação: {url}"})

            # cleanup
            sessions.pop(key, None)
            asked_flag.pop(key, None)

    return {"ok": True}

# ─────────────────────────── main ────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 10000)))
