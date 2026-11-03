from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import __version__
from app.adapters import DB_TYPE_INFO
from app.config import BASE_DIR, settings
from app.db import get_session
from app.models import ApiKey, DatabaseConnection
from app.services import connections as cs
from app.utils import utcnow

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))
templates.env.globals.update(version=__version__, db_type_info=DB_TYPE_INFO)


def render(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context: Any,
):
    context["request"] = request
    context.setdefault("flashes", request.session.pop("flashes", []))
    return templates.TemplateResponse(
        request,
        template_name,
        context,
        status_code=status_code,
        headers=headers,
    )


def flash(request: Request, message: str, category: str = "success") -> None:
    items = request.session.get("flashes", [])
    items.append({"message": message, "category": category})
    request.session["flashes"] = items


def parse_expiry(text: str | None) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise cs.ServiceError("过期时间格式不正确") from exc
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def expiry_input(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M")


def expiry_display(value: datetime | None) -> str:
    if value is None:
        return "永不过期"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def last_used_display(value: datetime | None) -> str:
    if value is None:
        return "从未使用"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.globals.update(
    expiry_display=expiry_display,
    last_used_display=last_used_display,
)


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/databases", status_code=303)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- databases


@router.get("/databases")
def databases_list(
    request: Request, session: Session = Depends(get_session)
):
    records = cs.list_databases(session)
    return render(
        request,
        "databases/list.html",
        active="databases",
        databases=records,
    )


@router.get("/databases/new")
def database_new(request: Request):
    return render(
        request,
        "databases/form.html",
        active="databases",
        mode="new",
        action_url="/databases/new",
        form=_empty_database_form(),
        record=None,
    )


@router.post("/databases/new")
def database_create(
    request: Request,
    name: str = Form(""),
    db_type: str = Form("mysql"),
    host: str = Form(""),
    port: str = Form(""),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    tunnel_url: str = Form(""),
    tunnel_username: str = Form(""),
    tunnel_password: str = Form(""),
    tunnel_encode_base64: bool = Form(False),
    options: str = Form(""),
    description: str = Form(""),
    enabled: bool = Form(False),
    max_rows: int = Form(settings.default_max_rows),
    query_timeout: int = Form(settings.default_query_timeout),
    session: Session = Depends(get_session),
):
    form = {
        "name": name,
        "db_type": db_type,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "tunnel_url": tunnel_url,
        "tunnel_username": tunnel_username,
        "tunnel_encode_base64": tunnel_encode_base64,
        "options": options,
        "description": description,
        "enabled": enabled,
        "max_rows": max_rows,
        "query_timeout": query_timeout,
        "clear_password": False,
        "clear_tunnel_password": False,
    }
    try:
        cs.create_database(
            session,
            name=name,
            db_type=db_type,
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            tunnel_url=tunnel_url,
            tunnel_username=tunnel_username,
            tunnel_password=tunnel_password,
            tunnel_encode_base64=tunnel_encode_base64,
            options_text=options,
            description=description,
            enabled=enabled,
            max_rows=max_rows,
            query_timeout=query_timeout,
        )
    except cs.ServiceError as exc:
        return render(
            request,
            "databases/form.html",
            active="databases",
            mode="new",
            action_url="/databases/new",
            form=form,
            record=None,
            error=str(exc),
            status_code=400,
        )

    flash(request, f"数据库连接「{name}」已创建")
    return RedirectResponse("/databases", status_code=303)


@router.get("/databases/{database_id}/edit")
def database_edit(
    request: Request,
    database_id: int,
    session: Session = Depends(get_session),
):
    record = cs.get_database(session, database_id)
    if record is None:
        return render(
            request,
            "error.html",
            active="databases",
            message="数据库连接不存在",
            status_code=404,
        )
    return render(
        request,
        "databases/form.html",
        active="databases",
        mode="edit",
        action_url=f"/databases/{record.id}/edit",
        form=_database_form(record),
        record=record,
    )


@router.post("/databases/{database_id}/edit")
def database_update(
    request: Request,
    database_id: int,
    name: str = Form(""),
    db_type: str = Form("mysql"),
    host: str = Form(""),
    port: str = Form(""),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    clear_password: bool = Form(False),
    tunnel_url: str = Form(""),
    tunnel_username: str = Form(""),
    tunnel_password: str = Form(""),
    clear_tunnel_password: bool = Form(False),
    tunnel_encode_base64: bool = Form(False),
    options: str = Form(""),
    description: str = Form(""),
    enabled: bool = Form(False),
    max_rows: int = Form(settings.default_max_rows),
    query_timeout: int = Form(settings.default_query_timeout),
    session: Session = Depends(get_session),
):
    record = cs.get_database(session, database_id)
    if record is None:
        return render(
            request,
            "error.html",
            active="databases",
            message="数据库连接不存在",
            status_code=404,
        )
    form = {
        "name": name,
        "db_type": db_type,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "tunnel_url": tunnel_url,
        "tunnel_username": tunnel_username,
        "tunnel_encode_base64": tunnel_encode_base64,
        "options": options,
        "description": description,
        "enabled": enabled,
        "max_rows": max_rows,
        "query_timeout": query_timeout,
        "clear_password": clear_password,
        "clear_tunnel_password": clear_tunnel_password,
    }
    try:
        cs.update_database(
            session,
            record,
            name=name,
            db_type=db_type,
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            clear_password=clear_password,
            tunnel_url=tunnel_url,
            tunnel_username=tunnel_username,
            tunnel_password=tunnel_password,
            clear_tunnel_password=clear_tunnel_password,
            tunnel_encode_base64=tunnel_encode_base64,
            options_text=options,
            description=description,
            enabled=enabled,
            max_rows=max_rows,
            query_timeout=query_timeout,
        )
    except cs.ServiceError as exc:
        return render(
            request,
            "databases/form.html",
            active="databases",
            mode="edit",
            action_url=f"/databases/{database_id}/edit",
            form=form,
            record=record,
            error=str(exc),
            status_code=400,
        )

    flash(request, f"数据库连接「{name}」已更新")
    return RedirectResponse("/databases", status_code=303)


@router.post("/databases/{database_id}/delete")
def database_delete(
    request: Request,
    database_id: int,
    session: Session = Depends(get_session),
):
    record = cs.get_database(session, database_id)
    if record is None:
        flash(request, "数据库连接不存在", "error")
        return RedirectResponse("/databases", status_code=303)
    name = record.name
    cs.delete_database(session, record)
    flash(request, f"数据库连接「{name}」已删除")
    return RedirectResponse("/databases", status_code=303)


@router.post("/databases/{database_id}/test")
def database_test(database_id: int, session: Session = Depends(get_session)):
    record = cs.get_database(session, database_id)
    if record is None:
        return JSONResponse({"ok": False, "message": "数据库连接不存在"}, status_code=404)
    return cs.test_database(record)


# --------------------------------------------------------------------- keys


@router.get("/keys")
def keys_list(request: Request, session: Session = Depends(get_session)):
    keys = cs.list_api_keys(session)
    databases = cs.list_databases(session)
    return render(
        request,
        "keys/list.html",
        active="keys",
        keys=keys,
        databases=databases,
        now=utcnow(),
    )


@router.get("/keys/new")
def key_new(request: Request, session: Session = Depends(get_session)):
    databases = cs.list_databases(session)
    return render(
        request,
        "keys/form.html",
        active="keys",
        mode="new",
        action_url="/keys/new",
        form=_empty_key_form(),
        databases=databases,
        record=None,
    )


@router.post("/keys/new")
def key_create(
    request: Request,
    name: str = Form(""),
    expires_at: str = Form(""),
    database_ids: list[int] = Form([]),
    enabled: bool = Form(False),
    session: Session = Depends(get_session),
):
    databases = cs.list_databases(session)
    form = {
        "name": name,
        "expires_at": expires_at,
        "database_ids": database_ids,
        "enabled": enabled,
    }
    try:
        record, raw_key = cs.create_api_key(
            session,
            name=name,
            database_ids=database_ids,
            expires_at=parse_expiry(expires_at),
            enabled=enabled,
        )
    except cs.ServiceError as exc:
        return render(
            request,
            "keys/form.html",
            active="keys",
            mode="new",
            action_url="/keys/new",
            form=form,
            databases=databases,
            record=None,
            error=str(exc),
            status_code=400,
        )

    return render(
        request,
        "keys/created.html",
        active="keys",
        key=record,
        raw_key=raw_key,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/keys/{key_id}/edit")
def key_edit(
    request: Request,
    key_id: int,
    session: Session = Depends(get_session),
):
    record = cs.get_api_key(session, key_id)
    if record is None:
        return render(
            request,
            "error.html",
            active="keys",
            message="API Key 不存在",
            status_code=404,
        )
    databases = cs.list_databases(session)
    return render(
        request,
        "keys/form.html",
        active="keys",
        mode="edit",
        action_url=f"/keys/{record.id}/edit",
        form=_key_form(record),
        databases=databases,
        record=record,
    )


@router.post("/keys/{key_id}/edit")
def key_update(
    request: Request,
    key_id: int,
    name: str = Form(""),
    expires_at: str = Form(""),
    database_ids: list[int] = Form([]),
    enabled: bool = Form(False),
    session: Session = Depends(get_session),
):
    record = cs.get_api_key(session, key_id)
    if record is None:
        return render(
            request,
            "error.html",
            active="keys",
            message="API Key 不存在",
            status_code=404,
        )
    databases = cs.list_databases(session)
    form = {
        "name": name,
        "expires_at": expires_at,
        "database_ids": database_ids,
        "enabled": enabled,
    }
    try:
        cs.update_api_key(
            session,
            record,
            name=name,
            database_ids=database_ids,
            expires_at=parse_expiry(expires_at),
            enabled=enabled,
        )
    except cs.ServiceError as exc:
        return render(
            request,
            "keys/form.html",
            active="keys",
            mode="edit",
            action_url=f"/keys/{key_id}/edit",
            form=form,
            databases=databases,
            record=record,
            error=str(exc),
            status_code=400,
        )

    flash(request, f"API Key「{name}」已更新")
    return RedirectResponse("/keys", status_code=303)


@router.post("/keys/{key_id}/toggle")
def key_toggle(
    request: Request, key_id: int, session: Session = Depends(get_session)
):
    record = cs.get_api_key(session, key_id)
    if record is None:
        flash(request, "API Key 不存在", "error")
        return RedirectResponse("/keys", status_code=303)
    record.enabled = not record.enabled
    session.commit()
    state = "启用" if record.enabled else "禁用"
    flash(request, f"API Key「{record.name}」已{state}")
    return RedirectResponse("/keys", status_code=303)


@router.post("/keys/{key_id}/rotate")
def key_rotate(
    request: Request, key_id: int, session: Session = Depends(get_session)
):
    record = cs.get_api_key(session, key_id)
    if record is None:
        return render(
            request,
            "error.html",
            active="keys",
            message="API Key 不存在",
            status_code=404,
        )
    raw_key = cs.rotate_api_key(session, record)
    return render(
        request,
        "keys/created.html",
        active="keys",
        key=record,
        raw_key=raw_key,
        rotated=True,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/keys/{key_id}/delete")
def key_delete(
    request: Request, key_id: int, session: Session = Depends(get_session)
):
    record = cs.get_api_key(session, key_id)
    if record is None:
        flash(request, "API Key 不存在", "error")
        return RedirectResponse("/keys", status_code=303)
    name = record.name
    cs.delete_api_key(session, record)
    flash(request, f"API Key「{name}」已删除")
    return RedirectResponse("/keys", status_code=303)


# ------------------------------------------------------------------- forms


def _empty_database_form() -> dict[str, Any]:
    return {
        "name": "",
        "db_type": "mysql",
        "host": "",
        "port": "",
        "database": "",
        "username": "",
        "tunnel_url": "",
        "tunnel_username": "",
        "tunnel_encode_base64": True,
        "options": "",
        "description": "",
        "enabled": True,
        "max_rows": settings.default_max_rows,
        "query_timeout": settings.default_query_timeout,
        "clear_password": False,
        "clear_tunnel_password": False,
    }


def _database_form(record: DatabaseConnection) -> dict[str, Any]:
    return {
        "name": record.name,
        "db_type": record.db_type,
        "host": record.host or "",
        "port": record.port or "",
        "database": record.database,
        "username": record.username or "",
        "tunnel_url": record.tunnel_url or "",
        "tunnel_username": record.tunnel_username or "",
        "tunnel_encode_base64": record.tunnel_encode_base64,
        "options": record.options_json if record.options_json != "{}" else "",
        "description": record.description or "",
        "enabled": record.enabled,
        "max_rows": record.max_rows,
        "query_timeout": record.query_timeout,
        "clear_password": False,
        "clear_tunnel_password": False,
    }


def _empty_key_form() -> dict[str, Any]:
    return {
        "name": "",
        "expires_at": "",
        "database_ids": [],
        "enabled": True,
    }


def _key_form(record: ApiKey) -> dict[str, Any]:
    return {
        "name": record.name,
        "expires_at": expiry_input(record.expires_at),
        "database_ids": [database.id for database in record.databases],
        "enabled": record.enabled,
    }
