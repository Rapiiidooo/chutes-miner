"""
Routes for server management.
"""

import aiohttp
import orjson as json
from fastapi import APIRouter, Depends, HTTPException, status
from starlette.responses import StreamingResponse
from sqlalchemy import select, exists, or_
from sqlalchemy.ext.asyncio import AsyncSession
from api.database import get_db_session
from api.config import k8s_core_client, settings, validator_by_hotkey
from api.auth import authorize
from api.server.schemas import Server, ServerArgs
from api.server.util import bootstrap_server

router = APIRouter()


async def _get_server(db, id_or_name):
    server = (
        (
            await db.execute(
                select(Server).where(or_(Server.name == id_or_name, Server.server_id == id_or_name))
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No kubernetes node with id or name {id_or_name} found!",
        )
    return server


@router.get("/")
async def list_servers(
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(authorize(allow_miner=True, purpose="management")),
):
    """
    List servers, this can be quite a large response...
    """
    return (await db.execute(select(Server))).unique().scalars().all()


@router.post("/")
async def create_server(
    server_args: ServerArgs,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(authorize(allow_miner=True, allow_validator=False)),
):
    """
    Add a new server/kubernetes node to our inventory.  This is a very
    slow/long-running response via SSE, since it needs to do a lot of things.
    """
    node = k8s_core_client().read_node(name=server_args.name)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No kubernetes node with name={server_args.name} found!",
        )
    if (await db.execute(select(exists().where(Server.name == server_args.name)))).scalar():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Server with name={server_args.name} is already provisioned!",
        )

    # Validate short ref.
    validator = validator_by_hotkey(server_args.validator)
    supported_gpus = set([])
    try:
        async with aiohttp.ClientSession(raise_for_status=True) as s:
            async with s.get(f"{validator.api}/nodes/supported") as resp:
                supported_gpus = set(await resp.json())
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching validator's supported GPUs to check short ref: {exc}",
        )
    if server_args.gpu_short_ref not in supported_gpus:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{server_args.gpu_short_ref} is not supported by validator {server_args.validator}: {supported_gpus}",
        )

    # Stream creation/provisioning details back as they occur.
    async def _stream_provisioning_status():
        async for chunk in bootstrap_server(node, server_args):
            yield chunk

    return StreamingResponse(_stream_provisioning_status())


@router.get("/{id_or_name}/lock")
async def lock_server(
    id_or_name: str,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(authorize(allow_miner=True, allow_validator=False, purpose="management")),
):
    """
    Lock a server's deployments so it won't chase bounties.
    """
    server = await _get_server(db, id_or_name)
    server.locked = True
    await db.commit()
    await db.refresh(server)
    return server


@router.get("/{id_or_name}/unlock")
async def unlock_server(
    id_or_name: str,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(authorize(allow_miner=True, allow_validator=False, purpose="management")),
):
    """
    Unlock a server's deployments so it can chase bounties.
    """
    server = await _get_server(db, id_or_name)
    server.locked = False
    await db.commit()
    await db.refresh(server)
    return server


@router.delete("/{id_or_name}")
async def delete_server(
    id_or_name: str,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(authorize(allow_miner=True, allow_validator=False, purpose="management")),
):
    """
    Remove a kubernetes node from the cluster.
    """
    server = await _get_server(db, id_or_name)
    await settings.redis_client.publish(
        "miner_events",
        json.dumps(
            {
                "event_type": "server_deleted",
                "event_data": {
                    "server_id": server.server_id,
                },
            }
        ).decode(),
    )
    return {
        "status": "started",
        "detail": f"Deletion of {server.name=} {server.server_id=} started, and will be processed asynchronously by gepetto.",
    }
