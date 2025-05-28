from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
import httpx, os, uvicorn, asyncio, aiofiles, mimetypes, tempfile, subprocess
from dotenv import load_dotenv
from typing import Optional
from PyPDF2 import PdfReader
from docx import Document

load_dotenv()

app = FastAPI()

# ───────────────────────────────────────────────────────────
SLACK_BOT_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGN_SECRET = os.getenv("SLACK_SIGNING_SECRET")      # reservado p/ futura verificação
SLIDESPEAK_API_KEY= os.getenv("SLIDESPEAK_API_KEY")

HEADERS_SLACK = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
HEADERS_SS    = {
    "Content-Type": "application/json",
    "X-API-Key": SLIDESPEAK_API_KEY
}
# ───────────────────────────────────────────────────────────

@app.get("/")                # ← root para o health-check do Render
def root():
    return {"ok": True}

# ---------- utilidades p/ arquivos ---------------------------------
async def download_file_from_slack(url: str) -> Optional[str]:
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=HEADERS_SLACK)
        if r.status_code == 200:
            ext = mimetypes.guess_extension(r.headers.get("Content-Type", "")) or ".tmp"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(r.content); tmp.close()
            return tmp.name
    return None

async def extract_text(path: str) -> str:
    if path.endswith(".pdf"):
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    if path.endswith(".docx"):
        return "\n".join(p.text for p in Document(path).paragraphs)
    if path.endswith(".txt"):
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return await f.read()
    if path.endswith((".mp3",".m4a",".wav",".ogg")):
        subprocess.run(["whisper", path, "--language", "Portuguese",
                        "--model", "base", "--output_format", "txt"], capture_output=True)
        txt = path.rsplit(".",1)[0]+".txt"
        if os.path.exists(txt):
            async with aiofiles.open(txt,"r",encoding="utf-8") as f: return await f.read()
    return ""

# ---------- SlideSpeak ---------------------------------------------
async def gerar_apresentacao(texto:str, slides:int=5) -> str:
    payload = {
        "plain_text": texto, "length": slides, "template": "default",
        "language": "ORIGINAL", "fetch_images": True,
        "tone": "default", "verbosity": "standard"
    }
    async with httpx.AsyncClient() as c:
        r = await c.post("https://api.slidespeak.co/api/v1/presentation/generate",
                         headers=HEADERS_SS, json=payload)
        r.raise_for_status()
        task_id = r.json().get("task_id")
        # polling
        while True:
            s = await c.get(f"https://api.slidespeak.co/api/v1/task_status/{task_id}",
                            headers={"X-API-Key": SLIDESPEAK_API_KEY})
            s.raise_for_status()
            data = s.json()
            if data["task_status"]=="SUCCESS":
                return data["task_result"]["url"]
            if data["task_status"]=="FAILED":
                return "Não foi possível gerar a apresentação."
            await asyncio.sleep(4)

# ---------- Slack events -------------------------------------------
@app.post("/slack/events")
async def slack_events(req: Request):
    payload = await req.json()

    # handshake
    if payload.get("type")=="url_verification":
        return Response(content=f'{{"challenge":"{payload["challenge"]}"}}',
                        media_type="application/json")

    event = payload.get("event", {})
    if "bot_id" in event:                          # ignora mensagens do próprio bot
        return {"ok":True}

    if event.get("type")=="app_home_opened":
        user = event["user"]
        async with httpx.AsyncClient() as c:
            await c.post("https://slack.com/api/views.publish", headers=HEADERS_SLACK,
                         json={"user_id":user,
                               "view":{"type":"home","blocks":[
                                   {"type":"section","text":{"type":"mrkdwn",
                                   "text":"Bem-vindo! Envie um texto, áudio ou arquivo e eu gero sua apresentação ☺"}}
                               ]}})
        return {"ok":True}

    # -------- mensagem / arquivo -----------------------------------
    channel = event["channel"]
    content = event.get("text","")
    if event.get("files"):
        furl = event["files"][0]["url_private_download"]
        ftmp = await download_file_from_slack(furl)
        if ftmp:
            content = await extract_text(ftmp)

    link = await gerar_apresentacao(content)

    async with httpx.AsyncClient() as c:
        await c.post("https://slack.com/api/chat.postMessage",
                     headers=HEADERS_SLACK,
                     json={"channel":channel,
                           "text":f"Aqui está sua apresentação: {link}"})
    return {"ok":True}

# ---------- start ---------------------------------------------------
if __name__=="__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 10000)))
