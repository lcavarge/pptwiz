from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx, os, uvicorn, asyncio, aiofiles, mimetypes, tempfile, subprocess, datetime, traceback
from dotenv import load_dotenv
from typing import Optional
from PyPDF2 import PdfReader
from docx import Document

# ───────────────────────── util log ───────────────────────────────
def log(step: str, details: Optional[dict] = None):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds")
    if details:
        flat = " ".join(f"{k}={v}" for k, v in details.items())
        print(f"[{ts}] {step} | {flat}")
    else:
        print(f"[{ts}] {step}")

load_dotenv()
app = FastAPI()

# health-check para Render
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return Response(status_code=200)

# ───────────────── env vars ─────────────────
SLACK_BOT_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SLIDESPEAK_API_KEY= os.getenv("SLIDESPEAK_API_KEY")
HEADERS_SLACK = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
HEADERS_SS    = {"Content-Type": "application/json", "X-API-Key": SLIDESPEAK_API_KEY}
HTTPX_TIMEOUT = httpx.Timeout(60.0, connect=30.0)

# ───────────────── util arquivos ────────────
async def download_file_from_slack(url:str)->Optional[str]:
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        r=await c.get(url,headers=HEADERS_SLACK)
        if r.status_code==200:
            ext=mimetypes.guess_extension(r.headers.get("Content-Type","")) or ".tmp"
            tmp=tempfile.NamedTemporaryFile(delete=False,suffix=ext);
            tmp.write(r.content); tmp.close(); return tmp.name
    return None

async def extract_text(path:str)->str:
    if path.endswith(".pdf"):
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    if path.endswith(".docx"):
        return "\n".join(p.text for p in Document(path).paragraphs)
    if path.endswith(".txt"):
        async with aiofiles.open(path,"r",encoding="utf-8") as f: return await f.read()
    if path.endswith((".mp3",".m4a",".wav",".ogg")):
        subprocess.run(["whisper",path,"--language","Portuguese","--model","base","--output_format","txt"],capture_output=True)
        txt=path.rsplit(".",1)[0]+".txt"
        if os.path.exists(txt):
            async with aiofiles.open(txt,"r",encoding="utf-8") as f: return await f.read()
    return ""

# ───────────────── SlideSpeak ───────────────
async def gerar_apresentacao(texto:str,slides:int=5)->str:
    payload={"plain_text":texto,"length":slides,"template":"default","language":"ORIGINAL","fetch_images":True,"tone":"default","verbosity":"standard"}
    log("SlideSpeak|send",{"chars":len(texto)})
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        r=await c.post("https://api.slidespeak.co/api/v1/presentation/generate",headers=HEADERS_SS,json=payload)
        r.raise_for_status(); task_id=r.json().get("task_id")
        if not task_id: return "Erro: task_id vazio"
        log("SlideSpeak|task",{"id":task_id})
        while True:
            try:
                s=await c.get(f"https://api.slidespeak.co/api/v1/task_status/{task_id}",headers={"X-API-Key":SLIDESPEAK_API_KEY},timeout=HTTPX_TIMEOUT)
                s.raise_for_status(); data=s.json()
                if data["task_status"]=="SUCCESS":
                    url=data["task_result"]["url"]; log("SlideSpeak|done",{"url":url}); return url
                if data["task_status"]=="FAILED":
                    return "Não foi possível gerar a apresentação."
            except httpx.ReadTimeout:
                pass
            await asyncio.sleep(4)

# ───────────────── Slack events ─────────────
@@app.post("/slack/events")
async def slack_events(req: Request):
    payload = await req.json()
    ev_id = payload.get("event_id")
    # ── Deduplicação: ignore se já processado nos últimos 5 min ──
    now = datetime.datetime.utcnow().timestamp()
    to_delete = [k for k,v in processed.items() if now - v > 300]
    for k in to_delete: processed.pop(k, None)
    if ev_id in processed:
        log("Slack|duplicado",{"event_id":ev_id}); return {"ok":True}
    processed[ev_id] = now

    log("Slack|payload",{"type":payload.get("type"),"id":ev_id})

    # handshake ...(req:Request):
    payload=await req.json(); log("Slack|payload",{"type":payload.get("type")})
    if payload.get("type")=="url_verification":
        return Response(content=f'{{"challenge":"{payload["challenge"]}"}}',media_type="application/json")

    event=payload.get("event",{})
    if "bot_id" in event: return {"ok":True}

    channel=event.get("channel")
    if event.get("channel_type")=="im":
        channel=event.get("user")  # responde em DM via user_id

    text=event.get("text","")
    if event.get("files"):
        ftmp=await download_file_from_slack(event["files"][0]["url_private_download"])
        if ftmp:
            text=await extract_text(ftmp); log("Slack|file",{"chars":len(text)})

    log("Slack|cmd",{"channel":channel,"chars":len(text)})
    try:
        link=await gerar_apresentacao(text)
    except Exception as e:
        log("SlideSpeak|err",{"err":str(e)}); traceback.print_exc(); link="Erro ao gerar apresentação."

    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as c:
        resp=await c.post("https://slack.com/api/chat.postMessage",headers=HEADERS_SLACK,json={"channel":channel,"text":f"Aqui está sua apresentação: {link}"})
        log("Slack|send",resp.json())
    return {"ok":True}

# ────────────────────────── main ───────────
if __name__=="__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=int(os.getenv("PORT",10000)))
