import asyncio
import uuid

import redis
from decouple import config
import httpx
from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, create_engine, select

DATABASE_URL = config("DATABASE_URL")
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379")
PISTON_API = "https://emkc.org/api/v2/piston/execute"

engine = create_engine(DATABASE_URL, echo=True)
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
templates = Jinja2Templates(directory="templates")


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


app = FastAPI()
manager = ConnectionManager()


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
    return templates.TemplateResponse("session.html", {"request": request, "session_id": session_id})


@app.post("/run-code/")
async def run_code(code: str):
    payload = {
        "language": "python",
        "version": "3.10.0",
        "files": [{"content": code}]
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(PISTON_API, json=payload)
        breakpoint()
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Code execution failed")
        return response.json()


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(session_id: str, websocket: WebSocket):
    """Handles real-time collaborative editing via WebSockets."""
    await manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            redis_client.set(f"session:{session_id}:code", data)  # Store in Redis
            await manager.broadcast(session_id, data)
    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


async def save_code_to_db():
    """Periodically saves Redis code updates to the database only if changes exist."""
    while True:
        await asyncio.sleep(5)  # Wait 5 seconds before checking

        with Session(engine) as session:
            stmt = select(SessionModel)
            sessions = session.exec(stmt).all()
            any_updates = False  # Track if any updates were made

            for session_obj in sessions:
                redis_code = redis_client.get(f"session:{session_obj.id}:code")
                if redis_code and redis_code != session_obj.code:
                    session_obj.code = redis_code
                    session.add(session_obj)
                    any_updates = True  # Mark as updated

            if any_updates:
                session.commit()  # Only commit if there were changes


@app.on_event("startup")
async def start_background_tasks():
    """Start the Redis-to-DB sync process on app startup."""
    asyncio.create_task(save_code_to_db())
