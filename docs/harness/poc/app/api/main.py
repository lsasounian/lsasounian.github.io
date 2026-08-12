"""FastAPI que expõe o harness mediador via API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from langchain_core.messages import HumanMessage
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from app.agent.graph import build_graph
from app.telemetry import setup_telemetry


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry()
    app.state.graph = await build_graph()  # descobre as tools via MCP e compila o grafo 1x
    yield


app = FastAPI(title="Mediator Harness API", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    active_skill: str | None = None
    refined_query: str | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    config = {"configurable": {"thread_id": request.thread_id}}
    result = await app.state.graph.ainvoke(
        {"messages": [HumanMessage(content=request.message)]}, config=config
    )
    return ChatResponse(
        reply=result["messages"][-1].content,
        active_skill=result.get("active_skill"),
        refined_query=result.get("refined_query"),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
