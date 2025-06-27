import os
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import httpx
import openai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SLIDESPEAK_API_KEY = os.getenv("SLIDESPEAK_API_KEY")

if not all([SLACK_BOT_TOKEN, OPENAI_API_KEY, SLIDESPEAK_API_KEY]):
    raise RuntimeError("Missing required environment variables: SLACK_BOT_TOKEN, OPENAI_API_KEY, SLIDESPEAK_API_KEY")

# Set OpenAI API key
openai.api_key = OPENAI_API_KEY

# Create FastAPI app and HTTPX clients
app = FastAPI()
slack_client = httpx.AsyncClient(base_url="https://slack.com/api")
slidespeak_client = httpx.AsyncClient(base_url="https://api.slidespeak.co/api/v1")

async def send_slack_message(channel: str, text: str):
    """
    Send a message to Slack channel.
    """
    resp = await slack_client.post(
        "/chat.postMessage",
        json={"channel": channel, "text": text},
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    )
    data = resp.json()
    if not data.get("ok"):
        # Log error if needed
        print("Slack API error:", data)
    return data

async def gerar_roteiro(texto: str) -> str:
    """
    Generate a slide script using OpenAI ChatCompletion.
    """
    system_prompt = (
        "Você é um assistente que converte instruções em um roteiro para apresentação de slides."
    )
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": texto}
        ],
        temperature=0.7,
    )
    return response.choices[0].message.content

async def gerar_apresentacao(roteiro_json: str, slides: int = 5) -> str:
    """
    Create a presentation via SlideSpeak API and return the download URL.
    """
    headers = {"X-API-Key": SLIDESPEAK_API_KEY}
    # Submit task
    resp = await slidespeak_client.post(
        "/tasks",
        json={"slides": slides, "content": roteiro_json},
        headers=headers
    )
    resp.raise_for_status()
    task = resp.json()
    task_id = task.get("id")

    # Poll for completion
    for _ in range(60):  # up to ~2 minutes
        status_resp = await slidespeak_client.get(
            f"/task_status/{task_id}",
            headers=headers
        )
        status_resp.raise_for_status()
        status = status_resp.json()
        if status.get("status") == "completed":
            return status.get("url")
        await asyncio.sleep(2)

    raise HTTPException(504, "SlideSpeak task timed out")

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint to receive Slack Events API calls.
    """
    payload = await request.json()
    # URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = payload.get("event", {})
    # Ignore bot messages
    if event.get("bot_id"):
        return {"ok": True}

    # Handle user messages
    if event.get("type") == "message" and "text" in event:
        channel = event.get("channel")
        text = event.get("text", "")
        # Process asynchronously
        background_tasks.add_task(process_message, channel, text)

    return {"ok": True}

async def process_message(channel: str, text: str):
    """
    Full pipeline: generate script, create PPT, send back to Slack.
    """
    try:
        roteiro = await gerar_roteiro(text)
        url = await gerar_apresentacao(roteiro)
        await send_slack_message(channel, f"Aqui está sua apresentação: <{url}>")
    except Exception as e:
        await send_slack_message(channel, f"Erro ao gerar apresentação: {e}")
