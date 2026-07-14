import asyncio
import contextlib

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi import Query
from pydantic import BaseModel

from pivot.config import Timeframe
from server.routers.chart import _parse_before, _parse_ma_windows
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


@router.get("/api/live/history/{symbol}")
async def history(
    symbol: str,
    request: Request,
    timeframe: str = "day",
    ma: str | None = Query(None, description="comma-separated moving average windows"),
    before: str | None = Query(
        None, description="exclusive upper bound: yyyy-mm-dd or unix seconds"
    ),
) -> dict:
    try:
        tf = Timeframe.from_code(timeframe)
        return await _service(request).chart_history(
            symbol,
            tf,
            _parse_ma_windows(ma),
            before=_parse_before(before, tf),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Kiwoom chart request failed") from exc


@router.websocket("/ws/live")
async def live_events(websocket: WebSocket) -> None:
    await websocket.accept()
    service: LiveService = websocket.app.state.live
    listener = await service.add_listener()
    sender = asyncio.create_task(_send_events(websocket, listener))
    receiver = asyncio.create_task(_wait_for_disconnect(websocket))
    try:
        done, _ = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await task
    except (WebSocketDisconnect, ListenerClosed):
        pass
    finally:
        for task in (sender, receiver):
            task.cancel()
        for task in (sender, receiver):
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, ListenerClosed
            ):
                await task
        await service.remove_listener(listener)


async def _send_events(websocket: WebSocket, listener) -> None:
    while True:
        await websocket.send_json(await listener.get())


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass
