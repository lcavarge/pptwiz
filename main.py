from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import os
import uvicorn
from dotenv import load_dotenv
import aiofiles
import mimetypes
import tempfile
import subprocess
from typing import Optional
import asyncio

from PyPDF2 import PdfReader
from docx import Document

load_dotenv()

app = FastAPI()

# Health‑check endpoint for Render (accepts GET and HEAD)
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True}


SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLIDESPEAK_API_KEY = os.getenv("SLIDESPEAK_API_KEY")

HEADERS_SLACK = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

async def download_file_from_slack(url: str) -> Optional[str]:
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS_SLACK)
        if response.status_code == 200:
            ext = mimetypes.guess_extension(response.headers.get("Content-Type", "")) or ".tmp"
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            temp_file.write(response.content)
            temp_file.close()
            return temp_file.name
    return None

async def extract_text_from_file(file_path: str) -> str:
    if file_path.endswith(".pdf"):
        reader = PdfReader(file_path)
        return "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
    elif file_path.endswith(".docx"):
        doc = Document(file_path)
        return "\n".join([para.text for para in doc.paragraphs])
    elif file_path.endswith(".txt"):
        async with aiofiles.open(file_path, mode='r', encoding='utf-8') as f:
            return await f.read()
    elif file_path.endswith(('.mp3', '.m4a', '.wav', '.ogg')):
        result = subprocess.run(["whisper", file_path, "--language", "Portuguese", "--model", "base", "--output_format", "txt"], capture_output=True)
        txt_file = file_path.replace(file_path.split('.')[-1], 'txt')
        if os.path.exists(txt_file):
            async with aiofiles.open(txt_file, mode='r', encoding='utf-8') as f:
                return await f.read()
    return ""

async def gerar_apresentacao(conteudo):
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": SLIDESPEAK_API_KEY
    }
    payload = {
        "plain_text": conteudo,
        "length": 5,
        "template": "default",
        "language": "ORIGINAL",
        "fetch_images": True,
        "tone": "default",
        "verbosity": "standard"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post("https://api.slidespeak.co/api/v1/presentation/generate", headers=headers, json=payload)
        response.raise_for_status()
        task_id = response.json().get("task_id")

        while True:
            status_response = await client.get(f"https://api.slidespeak.co/api/v1/task_status/{task_id}", headers={"X-API-Key": SLIDESPEAK_API_KEY})
            status_response.raise_for_status()
            status_data = status_response.json()
            if status_data["task_status"] == "SUCCESS":
                return status_data["task_result"]["url"]
            elif status_data["task_status"] == "FAILED":
                return "Não foi possível gerar a apresentação."
            await asyncio.sleep(5)

@app.post("/slack/events")
async def slack_events(req: Request):
    payload = await req.json()

    if payload.get("type") == "url_verification":
        return JSONResponse(content={"challenge": payload.get("challenge")})

    event = payload.get("event", {})
    if "bot_id" in event:
        return {"ok": True}

    if event.get("type") == "app_home_opened" and event.get("user"):
        user_id = event["user"]
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://slack.com/api/views.publish",
                headers=HEADERS_SLACK,
                json={
                    "user_id": user_id,
                    "view": {
                        "type": "home",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "Bem-vindo! Me envie um texto, áudio ou arquivo e eu crio uma apresentação pra você."
                                }
                            }
                        ]
                    }
                }
            )
        return {"ok": True}

    channel_id = event.get("channel")
    text = event.get("text", "")
    files = event.get("files", [])

    extracted_content = text

    if files:
        file_info = files[0]
        file_url = file_info.get("url_private_download")
        downloaded_file = await download_file_from_slack(file_url)
        if downloaded_file:
            extracted_content = await extract_text_from_file(downloaded_file)

    link = await gerar_apresentacao(extracted_content)

    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers=HEADERS_SLACK,
            json={"channel": channel_id, "text": f"Aqui está sua apresentação: {link}"}
        )

    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
