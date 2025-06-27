from fastapi import FastAPI, Request
from fastapi.responses import Response
import os, uvicorn, httpx, asyncio, datetime, traceback, pandas as pd
import aiofiles, mimetypes, tempfile, subprocess
from PyPDF2 import PdfReader
from docx import Document
import openai
from typing import Optional

# ───── logging helper ─────────────────────────────────────────────
def log(step: str, d: Optional[dict] = None):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds")
    extras = " ".join(f"{k}={v}" for k, v in (d or {}).items())
    print(f"[{ts}] {step}" + (f" | {extras}" if extras else ""))

# ───── env vars / clients ────────────────────────────────────────
SLACK_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SS_API_KEY    = os.getenv("SLIDESPEAK_API_KEY")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")

HEAD_SLACK = {"Authorization": f"Bearer {SLACK_TOKEN}"}
HEAD_SS    = {"Content-Type": "application/json", "X-API-Key": SS_API_KEY}

openai.api_key = OPENAI_KEY
HTTPX_TIMEOUT  = httpx.Timeout(60.0, connect=30.0)

app = FastAPI()

# health-check
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return Response(status_code=200)

# ───── file helpers ───────────────────────────────────────────────
async def download_slack(url: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        r = await c.get(url, headers=HEAD_SLACK)
        if r.status_code == 200:
            ext = mimetypes.guess_extension(r.headers.get("Content-Type","")) or ".tmp"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(r.content); tmp.close()
            return tmp.name
    return None

async def extract(path: str) -> str:
    if path.endswith(".xlsx"):
        df = pd.read_excel(path, sheet_name=0)          # 1ª aba
        # resumo: salários por área, top-10 etc (exemplo genérico)
        summary = df.describe(include="all").to_markdown()
        return f"*Resumo do Excel:*\n{summary}"
    if path.endswith(".pdf"):
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)[:4000]
    if path.endswith(".docx"):
        return "\n".join(p.text for p in Document(path).paragraphs)[:4000]
    if path.endswith(".txt"):
        async with aiofiles.open(path,"r",encoding="utf-8") as f: return await f.read()[:4000]
    return ""

# ───── ChatGPT prompt ─────────────────────────────────────────────
async def gerar_roteiro(texto:str, pedido:str) -> str:
    system = (
        "Você é um analista. Gere JSON com campos: title, "
        "slides[ {heading, bullets[]} ]. Português formal."
    )
    user = f"Pedido do usuário:\n{pedido}\n\nDados:\n{texto}"
    resp = await openai.ChatCompletion.acreate(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":system},
                  {"role":"user","content":user}],
        response_format={"type":"json_object"}
    )
    return resp.choices[0].message.content   # string JSON

# ───── SlideSpeak ─────────────────────────────────────────────────
async def gerar_ppt(json_content:str) -> str:
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        r = await c.post("https://api.slidespeak.co/api/v1/presentation/generate",
                         headers=HEAD_SS,
                         json={"json_content": json_content})
        r.raise_for_status()
        task_id = r.json()["task_id"]
        while True:
            s = await c.get(f"https://api.slidespeak.co/api/v1/task_status/{task_id}",
                            headers={"X-API-Key": SS_API_KEY})
            d = s.json()
            if d["task_status"]=="SUCCESS":
                return d["task_result"]["url"]
            if d["task_status"]=="FAILED":
                return "Erro ao gerar PPT."
            await asyncio.sleep(4)

# ───── dedup por client_msg_id ────────────────────────────────────
dedup = {}
def is_duplicate(client_id:str) -> bool:
    now = datetime.datetime.utcnow().timestamp()
    # limpa >5 min
    for k,t in list(dedup.items()):
        if now-t>300: dedup.pop(k,None)
    if client_id in dedup: return True
    dedup[client_id] = now
    return False

# ───── Slack events ───────────────────────────────────────────────
@app.post("/slack/events")
async def slack_events(req: Request):
    p = await req.json()

    if p.get("type")=="url_verification":
        return Response(content=f'{{"challenge":"{p["challenge"]}"}}',
                        media_type="application/json")

    ev = p.get("event",{})
    if "bot_id" in ev: return {"ok":True}

    cid = ev.get("client_msg_id") or ev.get("ts")
    if is_duplicate(cid):
        log("dup",{"id":cid}); return {"ok":True}

    channel = ev["user"] if ev.get("channel_type")=="im" else ev["channel"]
    pedido  = ev.get("text","")
    texto   = ""

    if ev.get("files"):
        ftmp = await download_slack(ev["files"][0]["url_private_download"])
        if ftmp: texto = await extract(ftmp)

    log("slack_cmd", {"ch":channel, "chars":len(texto)})

    try:
        roteiro_json = await gerar_roteiro(texto, pedido)
        ppt_url      = await gerar_ppt(roteiro_json)
    except Exception as e:
        traceback.print_exc()
        ppt_url = "Erro interno ao gerar PPT."

    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        resp = await c.post("https://slack.com/api/chat.postMessage",
                            headers=HEAD_SLACK,
                            json={"channel": channel,
                                  "text": f"Aqui está sua apresentação: {ppt_url}"})
        log("slack_send", resp.json())
    return {"ok":True}

# ───── main ───────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT",10000)))
