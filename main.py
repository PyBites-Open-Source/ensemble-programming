import asyncio
import json
import uuid
from collections import defaultdict

import httpx
import redis
from decouple import config
from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, create_engine, select

DATABASE_URL = config("DATABASE_URL")
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379")
PISTON_API = "https://emkc.org/api/v2/piston/execute"

engine = create_engine(DATABASE_URL, echo=True)
templates = Jinja2Templates(directory="templates")

try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()  # Test Redis connection
    use_redis = True
except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
    redis_client = None
    use_redis = False
    print("⚠️ Redis not available. Using in-memory storage instead.")

# In-memory storage (used only if Redis is unavailable)
in_memory_code_storage = {}
in_memory_timers = {}
in_memory_users = defaultdict(set)


class SessionModel(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    goal: str
    code: str = ""


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        """Accept WebSocket connections and track active clients."""
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

    def disconnect(self, session_id: str, websocket: WebSocket):
        """Remove disconnected clients from active connections."""
        self.active_connections[session_id].remove(websocket)
        if not self.active_connections[session_id]:
            del self.active_connections[session_id]

    async def broadcast(self, session_id: str, message: str):
        """Send message to all connected clients in a session."""
        for connection in self.active_connections.get(session_id, []):
            await connection.send_text(message)


manager = ConnectionManager()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def init_db():
    SQLModel.metadata.create_all(engine)


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/new-session")
async def new_session(goal: str = Form(...)):
    """Creates a new coding session with a unique ID."""
    with Session(engine) as session:
        new_session = SessionModel(goal=goal)
        session.add(new_session)
        session.commit()
        return JSONResponse(
            content={}, headers={"HX-Redirect": f"/session/{new_session.id}"}
        )


@app.get("/session/{session_id}")
async def session_page(request: Request, session_id: str):
    """Returns the session page with real-time code editor."""
    return templates.TemplateResponse(
        "session.html", {"request": request, "session_id": session_id}
    )


@app.post("/run-code")
async def run_code(code: str = Form(...)):
    """Executes the submitted code using Piston API."""
    payload = {
        "language": "python",
        "version": "3.10.0",
        "files": [{"content": code}],
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(PISTON_API, json=payload)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Code execution failed")
        result = response.json()

        stdout = result.get("run", {}).get("stdout", "")
        stderr = result.get("run", {}).get("stderr", "")
        output = result.get("run", {}).get("output", "")

        return {"stdout": stdout, "stderr": stderr, "output": output}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(session_id: str, websocket: WebSocket):
    """Handles real-time collaborative editing via WebSockets."""
    await manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("type") == "code":
                if use_redis:
                    redis_client.set(f"session:{session_id}:code", message["content"])
                else:
                    in_memory_code_storage[session_id] = message[
                        "content"
                    ]  # Store in memory

                await manager.broadcast(
                    session_id,
                    json.dumps({"type": "code", "content": message["content"]}),
                )

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


@app.websocket("/ws/timer/{session_id}")
async def websocket_timer(session_id: str, websocket: WebSocket):
    await manager.connect(session_id, websocket)
    timer = (
        int(redis_client.get(f"session:{session_id}:timer") or 300)
        if use_redis
        else in_memory_timers.get(f"{session_id}:timer", 300)
    )
    while True:
        await asyncio.sleep(1)
        if timer > 0:
            timer -= 1
            if use_redis:
                redis_client.set(f"session:{session_id}:timer", timer)
            else:
                in_memory_timers[f"{session_id}:timer"] = timer
        await websocket.send_text(json.dumps({"type": "timer", "time": timer}))
        if timer == 0:
            timer = 300  # Reset to 5 minutes
            if use_redis:
                redis_client.set(f"session:{session_id}:timer", timer)
            else:
                in_memory_timers[f"{session_id}:timer"] = timer

            # 🔹 Broadcast the reset timer to ALL clients
            for connection in manager.active_connections.get(session_id, []):
                await connection.send_text(json.dumps({"type": "timer", "time": timer}))


@app.websocket("/ws/users/{session_id}")
async def websocket_users(session_id: str, websocket: WebSocket):
    """Tracks users joining a session and broadcasts user list."""
    await manager.connect(session_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("type") == "join":
                username = message["username"]

                if use_redis:
                    redis_client.sadd(f"session:{session_id}:users", username)
                else:
                    in_memory_users[session_id].add(username)

                if use_redis:
                    users = list(redis_client.smembers(f"session:{session_id}:users"))
                else:
                    users = list(in_memory_users.get(session_id, set()))

                for connection in manager.active_connections.get(session_id, []):
                    print("sending user list", users)
                    await connection.send_text(
                        json.dumps({"type": "user_list", "users": users})
                    )

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


async def save_code_to_db():
    """Periodically saves Redis/in-memory code updates to the database only if changes exist."""
    while True:
        await asyncio.sleep(5)  # Batch save every 5 seconds

        with Session(engine) as session:
            stmt = select(SessionModel)
            sessions = session.exec(stmt).all()
            any_updates = False  # Track if any updates were made

            for session_obj in sessions:
                if use_redis:
                    latest_code = redis_client.get(f"session:{session_obj.id}:code")
                else:
                    latest_code = in_memory_code_storage.get(session_obj.id)

                if latest_code and latest_code != session_obj.code:
                    session_obj.code = latest_code
                    session.add(session_obj)
                    any_updates = True  # Mark as updated

            if any_updates:
                session.commit()  # Only commit if there were changes


@app.on_event("startup")
async def start_background_tasks():
    """Start the Redis-to-DB sync process only if storage is available."""
    if use_redis or in_memory_code_storage:
        asyncio.create_task(save_code_to_db())
