import os
import logging
import asyncio
import tempfile
import mimetypes

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from docx import Document
import aiofiles

# Load environment variables
tmp = tempfile.gettempdir()
load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLIDESPEAK_API_KEY = os.getenv("SLIDESPEAK_API_KEY")

# Prepare Slack headers
HEADERS_SLACK = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

# Configure logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("pptwiz")

# Initialize FastAPI
app = FastAPI()

async def download_file(url: str) -> str:
    """
    Download a file from Slack to a temporary local file.
    Returns the local file path.
    """
    async with httpx.AsyncClient() as client:
        logger.info("Slack|download file| url=%s", url)
        resp = await client.get(url, headers=HEADERS_SLACK)
        resp.raise_for_status()
        ext = mimetypes.guess_extension(resp.headers.get("Content-Type", "")) or ".bin"
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmp_file.write(resp.content)
        tmp_file.close()
        return tmp_file.name

async def extract_text(file_path: str) -> str:
    """
    Extract text from common file types: PDF, DOCX, TXT.
    """
    logger.info("Extracting text from file: %s", file_path)
    lower = file_path.lower()
    if lower.endswith(".pdf"):
        reader = PdfReader(file_path)
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    if lower.endswith(".docx"):
        doc = Document(file_path)
        return "\n".join(para.text for para in doc.paragraphs)
    if lower.endswith(".txt"):
        async with aiofiles.open(file_path, mode="r", encoding="utf-8") as f:
            return await f.read()
    return ""

async def generate_presentation(content: str, slides: int = 5) -> str:
    """
    Send content to SlideSpeak API to generate a PPT, poll for completion,
    and return the presentation URL.
    """
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": SLIDESPEAK_API_KEY
    }
    payload = {
        "plain_text": content,
        "length": slides,
        "template": "default",
        "language": "ORIGINAL",
        "fetch_images": True,
        "tone": "default",
        "verbosity": "standard"
    }

    async with httpx.AsyncClient() as client:
        logger.info("SlideSpeak|generating| chars=%d slides=%d", len(content), slides)
        response = await client.post(
            "https://api.slidespeak.co/api/v1/presentation/generate",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        task_id = response.json().get("task_id")
        logger.info("SlideSpeak|task created| id=%s", task_id)

        # Poll for status
        while True:
            status_resp = await client.get(
                f"https://api.slidespeak.co/api/v1/task_status/{task_id}",
                headers=headers,
                timeout=60
            )
            status_resp.raise_for_status()
            data = status_resp.json()
            status = data.get("task_status")
            if status == "SUCCESS":
                url = data.get("task_result", {}).get("url", "")
                logger.info("SlideSpeak|success| url=%s", url)
                return url
            if status == "FAILED":
                logger.error("SlideSpeak|failed")
                return ""
            await asyncio.sleep(5)

@app.post("/slack/events")
async def slack_events(request: Request):
    payload = await request.json()
    logger.info("Slack|payload received| type=%s", payload.get("type"))

    # URL verification for Slack Events API
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    event = payload.get("event", {})

    # Ignore messages from bots
    if event.get("bot_id"):
        return {"ok": True}

    # Extract text or file content
    text = event.get("text", "")
    files = event.get("files", [])
    if files:
        try:
            file_url = files[0].get("url_private_download")
            path = await download_file(file_url)
            text = await extract_text(path)
        except Exception as e:
            logger.error("Error extracting file: %s", e)

    # Generate the presentation
    url = await generate_presentation(text)

    # Send response back to Slack
    channel = event.get("channel")
    if url:
        message = f"Aqui está sua apresentação: {url}"
    else:
        message = "Ocorreu um erro ao gerar a apresentação."

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers=HEADERS_SLACK,
            json={"channel": channel, "text": message}
        )
        ok = resp.json().get("ok")
        logger.info("Slack|message sent| ok=%s channel=%s", ok, channel)

    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    logger.info("Starting server on port %d", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port)
