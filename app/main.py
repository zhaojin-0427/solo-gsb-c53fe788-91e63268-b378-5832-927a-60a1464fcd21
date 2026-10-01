"""FastAPI application: project/item CRUD plus exact-time evaluation."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from . import db
from .engine import (
    EngineError,
    Fraction,
    Item,
    evaluate,
    referrer_chain,
    resolve,
    to_time,
)
from .schemas import ItemIn, ProjectIn
from .seed import seed_if_empty

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed_if_empty()
    yield


app = FastAPI(title="灯光渐变时间轴", version="1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def item_to_dict(it: Item, resolved, all_items) -> dict:
    refs_me = referrer_chain(all_items, it.id)
    return {
        "id": it.id,
        "fixture": it.fixture,
        "channel": it.channel,
        "target": it.target,
        "duration": it.duration,
        "start_mode": it.start_mode,
        "start_ms": it.start_ms,
        "ref_id": it.ref_id,
        "ref_edge": it.ref_edge,
        "ref_offset": it.ref_offset,
        "actual_start": resolved.start,
        "actual_end": resolved.end,
        "interrupted_by": resolved.interrupted_by,
        "referrers": refs_me,
    }


def project_payload(conn, project_id: int) -> dict:
    items = db.load_items(conn, project_id)
    view = resolve(items)
    resolved_by_id = {r.item.id: r for r in view.items}
    return {
        "id": project_id,
        "name": db.project_name(conn, project_id),
        "items": [
            item_to_dict(it, resolved_by_id[it.id], items) for it in items
        ],
        "duration": max((r.end for r in view.items), default=0),
    }


def to_item(data: ItemIn, item_id: Optional[int] = None) -> Item:
    return Item(
        id=item_id,
        fixture=data.fixture.strip(),
        channel=data.channel,
        target=data.target,
        duration=data.duration,
        start_mode=data.start_mode,
        start_ms=data.start_ms if data.start_mode == "abs" else None,
        ref_id=data.ref_id if data.start_mode == "ref" else None,
        ref_edge=data.ref_edge if data.start_mode == "ref" else None,
        ref_offset=data.ref_offset if data.start_mode == "ref" else None,
    )


def get_project_or_404(conn, project_id: int):
    if not db.project_exists(conn, project_id):
        raise HTTPException(status_code=404, detail="工程不存在")


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

@app.get("/api/projects")
def api_list_projects():
    with db.transaction() as conn:
        out = []
        for row in db.list_projects(conn):
            pid = row["id"]
            items = db.load_items(conn, pid)
            end = max((r.end for r in resolve(items).items), default=0)
            out.append({"id": pid, "name": row["name"], "item_count": len(items),
                        "duration": end})
        return out


@app.post("/api/projects", status_code=201)
def api_create_project(data: ProjectIn):
    with db.transaction() as conn:
        pid = db.create_project(conn, data.name.strip())
        return project_payload(conn, pid)


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: int):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        return project_payload(conn, project_id)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/items", status_code=201)
def api_create_item(project_id: int, data: ItemIn):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        order = db.next_sort_order(conn, project_id)
        new_id = db.insert_item(conn, project_id, to_item(data), order)
        return _validate_and_payload(conn, project_id, new_id)


@app.put("/api/projects/{project_id}/items/{item_id}")
def api_update_item(project_id: int, item_id: int, data: ItemIn):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        if data.start_mode == "ref" and data.ref_id == item_id:
            raise HTTPException(status_code=400, detail="提示项不能引用自身")
        db.update_item(conn, project_id, to_item(data, item_id))
        return _validate_and_payload(conn, project_id, item_id)


@app.delete("/api/projects/{project_id}/items/{item_id}")
def api_delete_item(project_id: int, item_id: int):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        referenced = db.is_referenced(conn, project_id, item_id)
        if referenced:
            raise HTTPException(
                status_code=409,
                detail=f"该提示项被 #{'、#'.join(map(str, referenced))} 引用，不能删除",
            )
        db.delete_item_row(conn, project_id, item_id)
        return _validate_and_payload(conn, project_id, None)


def _validate_and_payload(conn, project_id: int, focus_id: Optional[int]):
    """Re-validate the entire resulting project; rollback on any error.

    The row writes above are uncommitted.  ``resolve`` raises
    EngineError on a missing reference / cycle / same-instant collision,
    which aborts the transaction so the previously saved project is kept.
    """
    try:
        payload = project_payload(conn, project_id)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    payload["focus_item_id"] = focus_id
    return payload


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

class EvalResponse(BaseModel):
    # integral instants serialize as int (e.g. 5), fractional ones as float
    time_ms: int | float
    channels: list


def _eval_time(t: str = Query(..., description="非负毫秒时刻，可为小数，如 1.5")) -> Fraction:
    """Parse the eval instant exactly; any non-negative decimal accepted.

    ``Fraction`` parses decimal strings exactly (``"1.5"`` -> 3/2) and also
    accepts rational strings such as ``"3/2"``.  Invalid / negative input
    becomes a 422, just like the old constrained integer parameter.
    """
    try:
        value = to_time(t)
    except (ValueError, ZeroDivisionError):
        raise RequestValidationError([
            {"type": "value_error",
             "loc": ("query", "t"),
             "msg": "t 必须是非负数字（允许小数毫秒）",
             "input": t}
        ])
    if value < 0:
        raise RequestValidationError([
            {"type": "greater_than_equal",
             "loc": ("query", "t"),
             "msg": "t 不能为负",
             "input": t,
             "ctx": {"ge": 0}}
        ])
    return value


@app.get("/api/projects/{project_id}/eval", response_model=EvalResponse)
def api_eval(project_id: int, t: Fraction = Depends(_eval_time)):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        items = db.load_items(conn, project_id)
        try:
            view = resolve(items)
            channels = evaluate(view, t)
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=exc.detail)
        ordered = sorted(channels.values(), key=lambda c: (c["fixture"], c["channel"]))
        # echo the evaluated instant: keep ints as ints, decimals as floats
        echo: int | float = t.numerator // t.denominator if t.denominator == 1 else float(t)
        return {"time_ms": echo, "channels": ordered}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
