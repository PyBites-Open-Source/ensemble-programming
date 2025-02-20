import uuid

from decouple import config
from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Field, Session, SQLModel, create_engine, select

DATABASE_URL = config("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=True)


class SessionModel(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    goal: str
    code: str = ""


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

    def disconnect(self, session_id: str, websocket: WebSocket):
        self.active_connections[session_id].remove(websocket)
        if not self.active_connections[session_id]:
            del self.active_connections[session_id]

    async def broadcast(self, session_id: str, message: str):
        for connection in self.active_connections.get(session_id, []):
            await connection.send_text(message)


app = FastAPI()
manager = ConnectionManager()


@app.on_event("startup")
def init_db():
    SQLModel.metadata.create_all(engine)


@app.get("/")
async def home():
    with open("index.html") as f:
        return HTMLResponse(f.read())


@app.post("/new-session")
async def new_session(goal: str = Form(...)):
    with Session(engine) as session:
        new_session = SessionModel(goal=goal)
        session.add(new_session)
        session.commit()
        return JSONResponse(
            content={}, headers={"HX-Redirect": f"/session/{new_session.id}"}
        )


@app.get("/session/{session_id}")
async def session_page(session_id: str):
    with open("session.html") as f:
        return HTMLResponse(f.read().replace("{{ session_id }}", session_id))


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(session_id: str, websocket: WebSocket):
    await manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            with Session(engine) as session:
                stmt = select(SessionModel).where(SessionModel.id == session_id)
                session_obj = session.exec(stmt).first()
                if session_obj:
                    session_obj.code = data
                    session.add(session_obj)
                    session.commit()
            await manager.broadcast(session_id, data)
    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)
