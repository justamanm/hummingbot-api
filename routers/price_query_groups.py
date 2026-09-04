"""保存并管理 MICRODUCK 日常报价分组。"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException

from database import AsyncDatabaseManager
from database.repositories.price_query_group_repository import (
    PriceQueryGroupRepository,
    normalize_price_query_group,
)
from deps import get_database_manager
from models.price_query_groups import PriceQueryGroupWrite
from utils.file_system import fs_util

router = APIRouter(tags=["Price query groups"], prefix="/price-query-groups")


def _microduck_config_references() -> list[dict[str, Any]]:
    """扫描模板与已部署 Bot 配置；旧配置也能自动出现在下拉列表。"""
    roots: list[tuple[str, str | None]] = [("conf/controllers", None)]
    try:
        roots.extend((f"instances/{bot_name}/conf/controllers", bot_name)
                     for bot_name in fs_util.list_folders("instances"))
    except FileNotFoundError:
        pass
    refs: list[dict[str, Any]] = []
    for root, bot_name in roots:
        try:
            files = fs_util.list_files(root)
        except FileNotFoundError:
            continue
        for filename in files:
            if not filename.endswith(".yml"):
                continue
            path = f"{root}/{filename}"
            try:
                config = fs_util.read_yaml_file(path)
            except Exception:
                continue
            if config.get("controller_name") != "microduck_profit_trailing":
                continue
            value = config.get("price_query_group")
            if value is None or not str(value).strip():
                continue
            refs.append({
                "path": path,
                "config_id": filename.removesuffix(".yml"),
                "bot_name": bot_name,
                "display_name": bot_name,
                "name": str(value).strip(),
                "config": config,
            })
    return refs


def _group_payload(name: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    _, normalized = normalize_price_query_group(name)
    matching = [
        {key: ref[key] for key in ("config_id", "bot_name", "display_name")}
        for ref in refs
        if normalize_price_query_group(ref["name"])[1] == normalized
    ]
    return {
        "name": name,
        "normalized_name": normalized,
        "reference_count": len(matching),
        "references": matching,
    }


async def _list_groups(repo: PriceQueryGroupRepository) -> list[dict[str, Any]]:
    refs = _microduck_config_references()
    existing = {item.normalized_name: item for item in await repo.list()}
    # 历史 Bot 中已经使用的名称也成为可选项，升级后不需要重新创建。
    for ref in refs:
        clean, normalized = normalize_price_query_group(ref["name"])
        if normalized not in existing:
            existing[normalized] = await repo.create(clean)
    return [_group_payload(item.name, refs) for item in sorted(existing.values(), key=lambda item: item.normalized_name)]


@router.get("")
async def list_price_query_groups(db: AsyncDatabaseManager = Depends(get_database_manager)):
    async with db.get_session_context() as session:
        repo = PriceQueryGroupRepository(session)
        return {"items": await _list_groups(repo)}


@router.post("", status_code=201)
async def create_price_query_group(
    body: PriceQueryGroupWrite,
    db: AsyncDatabaseManager = Depends(get_database_manager),
):
    try:
        async with db.get_session_context() as session:
            item = await PriceQueryGroupRepository(session).create(body.name)
            return _group_payload(item.name, _microduck_config_references())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/{name}")
async def rename_price_query_group(
    name: str,
    body: PriceQueryGroupWrite,
    db: AsyncDatabaseManager = Depends(get_database_manager),
):
    try:
        old_name, old_normalized = normalize_price_query_group(name)
        new_name, _ = normalize_price_query_group(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refs = _microduck_config_references()
    try:
        async with db.get_session_context() as session:
            repo = PriceQueryGroupRepository(session)
            item = await repo.get(old_name)
            if item is None:
                # 旧配置只有引用而未写入分组表时，也允许把它正式登记后重命名。
                if not any(normalize_price_query_group(ref["name"])[1] == old_normalized for ref in refs):
                    raise HTTPException(status_code=404, detail="报价分组不存在")
                item = await repo.create(old_name)
            item = await repo.rename(item, new_name)
            updated: list[dict[str, Any]] = []
            for ref in refs:
                if normalize_price_query_group(ref["name"])[1] != old_normalized:
                    continue
                ref["config"]["price_query_group"] = item.name
                fs_util.add_file(
                    ref["path"].rsplit("/", 1)[0],
                    ref["path"].rsplit("/", 1)[1],
                    yaml.safe_dump(ref["config"], allow_unicode=True, sort_keys=False),
                    override=True,
                )
                updated.append({key: ref[key] for key in ("config_id", "bot_name", "display_name")})
            return {"group": _group_payload(item.name, _microduck_config_references()), "updated": updated}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{name}")
async def delete_price_query_group(
    name: str,
    db: AsyncDatabaseManager = Depends(get_database_manager),
):
    try:
        clean, _ = normalize_price_query_group(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refs = _microduck_config_references()
    payload = _group_payload(clean, refs)
    if payload["reference_count"]:
        raise HTTPException(status_code=409, detail={"message": "报价分组仍被 Bot 使用，不能删除", "references": payload["references"]})
    async with db.get_session_context() as session:
        repo = PriceQueryGroupRepository(session)
        item = await repo.get(clean)
        if item is None:
            raise HTTPException(status_code=404, detail="报价分组不存在")
        await session.delete(item)
    return {"deleted": True, "name": clean}
