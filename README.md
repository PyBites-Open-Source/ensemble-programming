# Pybites Ensemble Programming

Building a tool to work as a team on a single codebase.

Uses FastAPI, sqlmodel, websockets, redis and some htmx for the frontend.

## Setup + run

```bash
$ uv sync
$ docker run -d --name redis -p 6379:6379 redis
$ uv run fastapi dev main.py
# set db
$ cp .env-template .env
# go to localhost:8000, create a session and join in from 2nd browser using session link
```
