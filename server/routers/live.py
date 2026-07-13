from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from server.live import ListenerClosed, LiveService, LiveServiceError

router = APIRouter(tags=["live"])


class ActivateModelRequest(BaseModel):
    run_id: int
    artifact_id: int | None = None


class SubscribeRequest(BaseModel):
    symbol: str


def _service(request: Request) -> LiveService:
    return request.app.state.live


@router.get("/api/live/state")
def state(request: Request) -> dict:
    return _service(request).state()


@router.put("/api/live/model")
async def activate_model(payload: ActivateModelRequest, request: Request) -> dict:
    try:
        return await _service(request).activate_model(
            payload.run_id, payload.artifact_id
        )
    except (LiveServiceError, LookupError, RuntimeError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, LiveServiceError) else "model activation failed"
        raise HTTPException(422, detail) from exc


@router.get("/api/live/subscriptions")
def subscriptions(request: Request) -> list[dict]:
    return _service(request).subscriptions()


@router.post("/api/live/subscriptions")
async def subscribe(payload: SubscribeRequest, request: Request) -> list[dict]:
    try:
        service = _service(request)
        await service.subscribe(payload.symbol)
        return service.subscriptions()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LiveServiceError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.delete("/api/live/subscriptions/{symbol}")
async def unsubscribe(symbol: str, request: Request) -> list[dict]:
    try:
        service = _service(request)
        await service.unsubscribe(symbol)
        return service.subscriptions()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, f"{symbol} is not subscribed") from exc
    except LiveServiceError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.websocket("/ws/live")
async def live_events(websocket: WebSocket) -> None:
    await websocket.accept()
    service: LiveService = websocket.app.state.live
    listener = await service.add_listener()
    try:
        while True:
            await websocket.send_json(await listener.get())
    except (WebSocketDisconnect, ListenerClosed):
        pass
    finally:
        await service.remove_listener(listener)
