import asyncio
import json
import uuid

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

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

ROTATION_TIME = 10


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
    """Handles real-time collaborative editing and typing notifications via WebSockets."""
    await manager.connect(session_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("type") == "code":
                redis_client.set(f"session:{session_id}:code", message["content"])

                await manager.broadcast(
                    session_id,
                    json.dumps({"type": "code", "content": message["content"]}),
                )

            elif message.get("type") == "typing":
                await manager.broadcast(
                    session_id,
                    json.dumps({"type": "typing", "username": message["username"]}),
                )

            elif message.get("type") == "stopped_typing":
                await manager.broadcast(
                    session_id,
                    json.dumps(
                        {"type": "stopped_typing", "username": message["username"]}
                    ),
                )

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


@app.websocket("/ws/timer/{session_id}")
async def websocket_timer(session_id: str, websocket: WebSocket):
    await manager.connect(session_id, websocket)

    timer = int(redis_client.get(f"session:{session_id}:timer") or ROTATION_TIME)
    current_driver = redis_client.get(f"session:{session_id}:driver")
    current_navigator = redis_client.get(f"session:{session_id}:navigator")

    try:
        # 🔹 Immediately send current roles to the newly connected client
        await websocket.send_text(
            json.dumps(
                {
                    "type": "roles",
                    "driver": current_driver or "Waiting...",
                    "navigator": current_navigator or "Waiting...",
                }
            )
        )

        while True:
            await asyncio.sleep(1)

            if timer > 0:
                timer -= 1
                redis_client.set(f"session:{session_id}:timer", timer)

            await websocket.send_text(json.dumps({"type": "timer", "time": timer}))

            if timer == 0:
                users = [
                    key
                    for key, value in redis_client.hgetall(
                        f"session:{session_id}:users"
                    ).items()
                    if json.loads(value)["active"]
                ]

                if users:
                    users.sort()
                    current_index = (
                        users.index(current_driver) if current_driver in users else -1
                    )

                    current_driver = users[(current_index + 1) % len(users)]
                    current_navigator = (
                        users[(current_index + 2) % len(users)]
                        if len(users) > 1
                        else None
                    )

                    redis_client.set(f"session:{session_id}:driver", current_driver)
                    redis_client.set(
                        f"session:{session_id}:navigator", current_navigator or ""
                    )

                    await manager.broadcast(
                        session_id,
                        json.dumps(
                            {
                                "type": "roles",
                                "driver": current_driver,
                                "navigator": current_navigator,
                            }
                        ),
                    )

                timer = ROTATION_TIME
                redis_client.set(f"session:{session_id}:timer", timer)

    except WebSocketDisconnect:
        print(f"🔴 WebSocket disconnected for session {session_id}")
        manager.disconnect(session_id, websocket)


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
                participate = message.get("participate", False)

                redis_client.hset(
                    f"session:{session_id}:users",
                    username,
                    json.dumps({"active": participate}),
                )

                users = [
                    key
                    for key, value in redis_client.hgetall(
                        f"session:{session_id}:users"
                    ).items()
                    if json.loads(value)["active"]
                ]

                users.sort()
                current_driver = redis_client.get(f"session:{session_id}:driver")
                current_navigator = redis_client.get(f"session:{session_id}:navigator")

                if users:
                    if not current_driver or current_driver not in users:
                        current_driver = users[0]
                        redis_client.set(f"session:{session_id}:driver", current_driver)

                    if len(users) > 1 and (
                        not current_navigator or current_navigator not in users
                    ):
                        current_navigator = users[1]
                        redis_client.set(
                            f"session:{session_id}:navigator", current_navigator
                        )

                await manager.broadcast(
                    session_id,
                    json.dumps(
                        {
                            "type": "user_list",
                            "users": users,
                            "driver": current_driver,
                            "navigator": current_navigator,
                        }
                    ),
                )

    except WebSocketDisconnect:
        print(f"🔴 WebSocket disconnected for session {session_id}")
        manager.disconnect(session_id, websocket)


async def save_code_to_db():
    """Periodically saves Redis/in-memory code updates to the database only if changes exist."""
    while True:
        await asyncio.sleep(5)

        with Session(engine) as session:
            stmt = select(SessionModel)
            sessions = session.exec(stmt).all()
            any_updates = False

            for session_obj in sessions:
                latest_code = redis_client.get(f"session:{session_obj.id}:code")

                if latest_code and latest_code != session_obj.code:
                    session_obj.code = latest_code
                    session.add(session_obj)
                    any_updates = True

            if any_updates:
                session.commit()


@app.on_event("startup")
async def start_background_tasks():
    """Start the Redis-to-DB sync process only if storage is available."""
    asyncio.create_task(save_code_to_db())
