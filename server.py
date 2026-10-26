"""Interactive demo server: live graph visualisation + human-in-the-loop for the claims workflow.

Run:  python server.py   ->  http://localhost:8000
"""

import asyncio
import json
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from google.adk.events._interrupts import extract_event_interrupt_ids
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

from claims_agent.agent import root_agent
from claims_agent.data import POLICIES, REQUIRED_DOCS

load_dotenv()

APP = "claims"
HUMAN_NODES = {"request_docs", "adjuster_review"}
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="adk-insurance-claims-workflow")
runner = InMemoryRunner(agent=root_agent, app_name=APP)
runs: dict[str, dict] = {}


def _kind(node) -> str:
    if node.name in HUMAN_NODES:
        return "human"
    cls = type(node).__name__
    if "Join" in cls:
        return "join"
    if "Function" in cls:
        return "function"
    return "llm"


def _graph() -> dict:
    g = root_agent.graph
    nodes = [{"id": n.name, "kind": "start" if n.name == "__START__" else _kind(n)} for n in g.nodes]
    edges = [{"from": e.from_node.name, "to": e.to_node.name, "route": e.route} for e in g.edges]
    return {"nodes": nodes, "edges": edges}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {"graph": _graph(), "policies": POLICIES, "required_docs": REQUIRED_DOCS}


class NewClaim(BaseModel):
    text: str


class Reply(BaseModel):
    interrupt_id: str
    function_name: str
    response: dict


async def _drive(run_id: str, message: types.Content):
    run = runs[run_id]
    q: asyncio.Queue = run["queue"]
    pending = {}
    try:
        async for event in runner.run_async(user_id="u", session_id=run["session_id"], new_message=message):
            if event.partial:
                continue
            node = getattr(event, "node_name", None) or event.author
            interrupts = extract_event_interrupt_ids(event)
            for fc in event.get_function_calls():
                if fc.id in interrupts:
                    pending[fc.id] = fc
                    await q.put({
                        "type": "input_request",
                        "node": node,
                        "interrupt_id": fc.id,
                        "function_name": fc.name,
                        "message": (fc.args or {}).get("message", ""),
                    })
            text = None
            if event.content and event.content.parts and event.author != "user":
                text = "".join(p.text or "" for p in event.content.parts if p.text).strip() or None
            if event.output is not None or text:
                await q.put({
                    "type": "node",
                    "node": node,
                    "output": event.output,
                    "text": text,
                    "route": event.actions.route if event.actions else None,
                })
    except Exception as e:  # surface failures in the UI instead of hanging
        await q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        await q.put({"type": "done"})
        return
    await q.put({"type": "waiting"} if pending else {"type": "done"})


@app.post("/api/claims")
async def create_claim(body: NewClaim):
    run_id = uuid.uuid4().hex[:8]
    session = await runner.session_service.create_session(app_name=APP, user_id="u")
    runs[run_id] = {"session_id": session.id, "queue": asyncio.Queue()}
    msg = types.Content(role="user", parts=[types.Part(text=body.text)])
    asyncio.create_task(_drive(run_id, msg))
    return {"run_id": run_id}


@app.post("/api/claims/{run_id}/respond")
async def respond(run_id: str, body: Reply):
    part = types.Part(function_response=types.FunctionResponse(
        id=body.interrupt_id, name=body.function_name, response=body.response))
    asyncio.create_task(_drive(run_id, types.Content(role="user", parts=[part])))
    return {"ok": True}


@app.get("/api/claims/{run_id}/events")
async def events(run_id: str):
    q = runs[run_id]["queue"]

    async def gen():
        while True:
            item = await q.get()
            yield f"data: {json.dumps(item, default=str)}\n\n"
            if item["type"] == "done":
                break

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
