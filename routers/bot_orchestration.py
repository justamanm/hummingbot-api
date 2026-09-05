import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from deps import get_accounts_service, get_bot_archiver, get_bots_orchestrator, get_docker_service
from models import BotDisplayNameUpdate, StartBotAction, StopBotAction, V2ControllerDeployment, V2ScriptDeployment
from services.bots_orchestrator import BotsOrchestrator
from services.docker_service import DockerService
from services.accounts_service import AccountsService
from utils.bot_archiver import BotArchiver
from utils.file_system import fs_util

# Create module-specific logger
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Bot Orchestration"], prefix="/bot-orchestration")


def _expected_usdg_reservation(config: dict[str, Any]) -> Decimal | None:
    """Return one live Microduck Bot's maximum next-buy USDG use."""
    if str(config.get("controller_name") or "") != "microduck_profit_trailing":
        return None
    live = config.get("live_trading", True)
    if isinstance(live, str):
        live = live.strip().lower() in {"1", "true", "yes", "on"}
    if not live:
        return None
    try:
        if str(config.get("buy_size_mode") or "budget") == "quantity":
            amount = Decimal(str(config.get("buy_amount_base")))
            configured_price = Decimal(str(config.get("buy_price_min_usd")))
            tolerance = Decimal(str(config.get("buy_price_upward_tolerance_usd", "0")))
            # 数量模式实际允许的最高成交价是配置买入价加向上容差；
            # 授权必须覆盖这个最坏情况，不能只按开始追踪的配置价格预留。
            price = configured_price + max(Decimal("0"), tolerance)
            result = amount * price
        else:
            result = Decimal(str(config.get("buy_budget_usd")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _controller_needs_buy_reservation(report: dict[str, Any] | None) -> bool:
    """Only states that can make a future buy reserve shared wallet allowance."""
    state = str((report or {}).get("custom_info", {}).get("state") or "").lower()
    return state not in {"holding", "trailing", "selling", "completed", "external_exit"}


async def _validate_usdg_allowance_before_deploy(
    configs: list[dict[str, Any]],
    bots_manager: BotsOrchestrator,
    accounts_service: AccountsService,
) -> None:
    """Reject a live deployment before it can over-reserve a wallet's USDG allowance."""
    requested_by_wallet: dict[str, Decimal] = {}
    for config in configs:
        reservation = _expected_usdg_reservation(config)
        wallet = str(config.get("wallet_address") or "").strip()
        if reservation is not None and wallet:
            requested_by_wallet[wallet.lower()] = requested_by_wallet.get(wallet.lower(), Decimal("0")) + reservation
    if not requested_by_wallet:
        return

    occupied_by_wallet: dict[str, Decimal] = {}
    for bot_name in list(bots_manager.active_bots):
        config_dir = f"instances/{bot_name}/conf/controllers"
        if not fs_util.path_exists(config_dir):
            continue
        reports = bots_manager.mqtt_manager.get_bot_controller_reports(bot_name)
        for filename in fs_util.list_files(config_dir):
            if not filename.endswith(".yml"):
                continue
            existing = fs_util.read_yaml_file(f"{config_dir}/{filename}")
            reservation = _expected_usdg_reservation(existing)
            wallet = str(existing.get("wallet_address") or "").strip().lower()
            controller_id = str(existing.get("id") or filename[:-4])
            report = reports.get(controller_id) if isinstance(reports, dict) else None
            if reservation is not None and wallet and _controller_needs_buy_reservation(report):
                occupied_by_wallet[wallet] = occupied_by_wallet.get(wallet, Decimal("0")) + reservation

    for wallet, requested in requested_by_wallet.items():
        try:
            response = await accounts_service.gateway_client.get_allowances(
                "ethereum", "robinhoodchain", wallet, "uniswap/router", tokens=["USDG"],
            )
            approvals = response.get("approvals", {}) if isinstance(response, dict) else {}
            allowance = Decimal(str(approvals.get("USDG", approvals.get("usdg", "0"))))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"无法读取钱包 USDG 授权额度：{exc}")
        occupied = occupied_by_wallet.get(wallet, Decimal("0"))
        required = occupied + requested
        if not allowance.is_finite() or allowance < required:
            raise HTTPException(
                status_code=409,
                detail=(f"USDG 授权额度不足：钱包 …{wallet[-5:]} 总授权 {allowance:.6f} USDG，"
                        f"已占用 {occupied:.6f} USDG，本次需要 {requested:.6f} USDG，"
                        f"合计需要 {required:.6f} USDG。请先在钱包余额中设置授权。"),
            )


@router.get("/status")
def get_active_bots_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of all active bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and data containing all active bot statuses
    """
    return {"status": "success", "data": bots_manager.get_all_bots_status()}


@router.get("/mqtt")
def get_mqtt_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get MQTT connection status and discovered bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with MQTT connection status, discovered bots, and broker information
    """
    mqtt_connected = bots_manager.mqtt_manager.is_connected
    discovered_bots = bots_manager.mqtt_manager.get_discovered_bots()
    active_bots = list(bots_manager.active_bots.keys())

    # Check client state
    client_state = "connected" if bots_manager.mqtt_manager.is_connected else "disconnected"

    return {
        "status": "success",
        "data": {
            "mqtt_connected": mqtt_connected,
            "discovered_bots": discovered_bots,
            "active_bots": active_bots,
            "broker_host": bots_manager.broker_host,
            "broker_port": bots_manager.broker_port,
            "broker_username": bots_manager.broker_username,
            "client_state": client_state
        }
    }


@router.get("/controller-performance-latest")
async def get_latest_controller_performance(
    bot_name: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get the most recent performance snapshot for each bot/controller.
    Optionally filter by bot_name.
    """
    try:
        snapshots = await bots_manager.get_latest_controller_performance(bot_name=bot_name)
        return {"status": "success", "data": snapshots}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get latest controller performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/controller-performance-history")
async def get_controller_performance_history(
    bot_name: str = None,
    controller_id: str = None,
    limit: int = Query(default=100, le=1000),
    cursor: str = None,
    start_time: str = None,
    end_time: str = None,
    interval: str = Query(default="5m", pattern="^(5m|15m|30m|1h|4h|12h|1d)$"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get historical controller performance snapshots with pagination and interval sampling.
    """
    try:
        parsed_start = datetime.fromisoformat(start_time) if start_time else None
        parsed_end = datetime.fromisoformat(end_time) if end_time else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    try:
        history, next_cursor, has_more = await bots_manager.get_controller_performance_history(
            bot_name=bot_name,
            controller_id=controller_id,
            limit=limit,
            cursor=cursor,
            start_time=parsed_start,
            end_time=parsed_end,
            interval=interval
        )
        return {
            "status": "success",
            "data": history,
            "pagination": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
                "interval": interval,
            }
        }
    except Exception as e:
        logger.error(f"Failed to get controller performance history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/buy-tracking-history")
async def get_buy_tracking_history(
    bot_name: str,
    controller_id: str = None,
    range: str = Query(default="1h", pattern="^(1h|3h|6h|12h|24h)$"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """Return sampled Microduck buy-tracking points for the selected recent range."""
    hours = int(range.removesuffix("h"))
    try:
        points = await bots_manager.get_buy_tracking_history(
            bot_name, controller_id, datetime.now(timezone.utc) - timedelta(hours=hours),
        )
        return {"status": "success", "range": range, "points": points}
    except Exception as e:
        logger.error(f"Failed to get buy tracking history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-trades")
async def get_strategy_trades(
    bot_name: str,
    controller_id: str = None,
    limit: int = Query(default=100, ge=1, le=500),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """Return chain-confirmed trades belonging to one Bot/controller."""
    try:
        trades = await bots_manager.get_strategy_trade_records(bot_name, controller_id, limit)
        return {"status": "success", "trades": trades}
    except Exception as e:
        logger.error(f"Failed to get strategy trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-trades/recent-confirmed")
async def get_recent_confirmed_strategy_trades(
    limit: int = Query(default=500, ge=1, le=1000),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """返回所有钱包总账中最近确认的买卖；不创建第二份账单。"""
    try:
        trades = await bots_manager.get_recent_confirmed_strategy_trades(limit)
        return {"status": "success", "trades": trades}
    except Exception as e:
        logger.error(f"Failed to get recent confirmed strategy trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/wallet-ledger")
async def get_wallet_ledger(
    wallet_address: str = Query(..., min_length=1),
    bot_name: str | None = Query(default=None, min_length=1),
    controller_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=500, ge=1, le=1000),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """返回钱包唯一总账；传入 Bot/控制器时仅返回其在该钱包中的引用记录。"""
    try:
        return {
            "status": "success",
            **await bots_manager.get_wallet_strategy_ledger(
                wallet_address,
                limit,
                bot_name=bot_name,
                controller_id=controller_id,
            ),
        }
    except Exception as e:
        logger.error(f"Failed to get wallet ledger: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{bot_name}/status")
def get_bot_status(bot_name: str, bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of a specific bot.

    Args:
        bot_name: Name of the bot to get status for
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot status information

    Raises:
        HTTPException: 404 if bot not found
    """
    response = bots_manager.get_bot_status(bot_name)
    if not response:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "status": "success",
        "data": response
    }


@router.get("/{bot_name}/history")
async def get_bot_history(
    bot_name: str,
    days: int = 0,
    verbose: bool = False,
    precision: int = None,
    timeout: float = 30.0,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get trading history for a bot with optional parameters.

    Args:
        bot_name: Name of the bot to get history for
        days: Number of days of history to retrieve (0 for all)
        verbose: Whether to include verbose output
        precision: Decimal precision for numerical values
        timeout: Timeout in seconds for the operation
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot trading history
    """
    response = await bots_manager.get_bot_history(
        bot_name,
        days=days,
        verbose=verbose,
        precision=precision,
        timeout=timeout
    )
    return {"status": "success", "response": response}


@router.post("/start-bot")
async def start_bot(
    action: StartBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Start a bot with the specified configuration.

    Args:
        action: StartBotAction containing bot configuration parameters
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and response from bot start operation
    """
    response = await bots_manager.start_bot(
        action.bot_name, log_level=action.log_level, script=action.script,
        conf=action.conf, async_backend=action.async_backend
    )

    # Bot run tracking simplified - only track deployment and stop times

    return {"status": "success", "response": response}


@router.post("/stop-bot")
async def stop_bot(
    action: StopBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Stop a bot with the specified configuration.

    Args:
        action: StopBotAction containing bot stop parameters
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and response from bot stop operation
    """
    # Capture final status BEFORE stopping (performance data is cleared on stop)
    final_status = None
    try:
        final_status = bots_manager.get_bot_status(action.bot_name)
        logger.info(f"Captured final status for {action.bot_name} before stopping")
    except Exception as e:
        logger.warning(f"Failed to capture final status for {action.bot_name}: {e}")

    response = await bots_manager.stop_bot(
        action.bot_name, skip_order_cancellation=action.skip_order_cancellation,
        async_backend=action.async_backend
    )

    # Update bot run status to STOPPED if stop was successful
    if response.get("success"):
        try:
            await bots_manager.mark_bot_run_stopped(action.bot_name, final_status=final_status)
        except Exception as e:
            logger.error(f"Failed to update bot run status: {e}")
            # Don't fail the stop operation if bot run update fails

    return {"status": "success", "response": response}


@router.post("/restart-bot/{bot_name}")
async def restart_bot(
    bot_name: str,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    docker_manager: DockerService = Depends(get_docker_service),
):
    """Restart one existing Bot container without archiving or removing its data."""
    response = await bots_manager.restart_bot_container(bot_name, docker_manager)
    if not response.get("success"):
        raise HTTPException(status_code=404, detail=response.get("message", "Bot restart failed"))
    return {"status": "success", "response": response}


@router.get("/bot-runs")
async def get_bot_runs(
    bot_name: str = None,
    account_name: str = None,
    strategy_type: str = None,
    strategy_name: str = None,
    run_status: str = None,
    deployment_status: str = None,
    limit: int = 100,
    offset: int = 0,
    include_final_status: bool = False,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get bot runs with optional filtering.

    Args:
        bot_name: Filter by bot name
        account_name: Filter by account name
        strategy_type: Filter by strategy type (script or controller)
        strategy_name: Filter by strategy name
        run_status: Filter by run status (CREATED, RUNNING, STOPPED, ERROR)
        deployment_status: Filter by deployment status (DEPLOYED, FAILED, ARCHIVED)
        limit: Maximum number of results to return
        offset: Number of results to skip
        include_final_status: Include the final status snapshot for each run. Off by
            default because the blob can be ~89 KB per record (~99% of the payload);
            use GET /bot-runs/{bot_run_id} to fetch it for a single run.
        bots_manager: Bot orchestrator service dependency

    Returns:
        List of bot runs with their details
    """
    try:
        runs_data = await bots_manager.get_bot_runs(
            bot_name=bot_name,
            account_name=account_name,
            strategy_type=strategy_type,
            strategy_name=strategy_name,
            run_status=run_status,
            deployment_status=deployment_status,
            limit=limit,
            offset=offset,
            include_final_status=include_final_status
        )

        return {
            "status": "success",
            "data": runs_data,
            "total": len(runs_data),
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        logger.error(f"Failed to get bot runs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/stats")
async def get_bot_run_stats(
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get statistics about bot runs.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Bot run statistics
    """
    try:
        stats = await bots_manager.get_bot_run_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        logger.error(f"Failed to get bot run stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/{bot_run_id}")
async def get_bot_run_by_id(
    bot_run_id: int,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get a specific bot run by ID.

    Args:
        bot_run_id: ID of the bot run
        bots_manager: Bot orchestrator service dependency

    Returns:
        Bot run details

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        run_dict = await bots_manager.get_bot_run_by_id(bot_run_id)

        if not run_dict:
            raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

        return {"status": "success", "data": run_dict}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bot-runs/{bot_run_id}")
async def delete_bot_run(
    bot_run_id: int,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Delete a bot run record by ID.

    Args:
        bot_run_id: ID of the bot run to delete
        bots_manager: Bot orchestrator service dependency

    Returns:
        Confirmation of deletion

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        result = await bots_manager.delete_bot_run(bot_run_id)

        if not result:
            raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

        return {
            "status": "success",
            "message": f"Bot run {bot_run_id} deleted successfully",
            "bot_name": result["bot_name"],
            "archived_folder_deleted": result["archived_folder_deleted"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop-and-archive-bot/{bot_name}")
async def stop_and_archive_bot(
    bot_name: str,
    background_tasks: BackgroundTasks,
    skip_order_cancellation: bool = True,
    archive_locally: bool = True,
    s3_bucket: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    docker_manager: DockerService = Depends(get_docker_service),
    bot_archiver: BotArchiver = Depends(get_bot_archiver)
):
    """
    Gracefully stop a bot and archive its data in the background.
    This initiates a background task that will:
    1. Stop the bot trading process via MQTT
    2. Wait 15 seconds for graceful shutdown
    3. Monitor and stop the Docker container
    4. Archive the bot data (locally or to S3)
    5. Remove the container

    Returns immediately with a success message while the process continues in the background.
    """
    try:
        # Step 1: Normalize bot name and container name
        # Container name is now the same as bot name (no prefix added)
        actual_bot_name = bot_name
        container_name = bot_name

        logging.info(f"Normalized bot_name: {actual_bot_name}, container_name: {container_name}")

        # Step 2: Validate bot exists in active bots
        active_bots = list(bots_manager.active_bots.keys())

        # Check if bot exists in active bots (could be stored as either format)
        bot_found = (actual_bot_name in active_bots) or (container_name in active_bots)

        if not bot_found:
            return {
                "status": "error",
                "message": (
                    f"Bot '{actual_bot_name}' not found in active bots. "
                    f"Active bots: {active_bots}. Cannot perform graceful shutdown."
                ),
                "details": {
                    "input_name": bot_name,
                    "actual_bot_name": actual_bot_name,
                    "container_name": container_name,
                    "active_bots": active_bots,
                    "reason": "Bot must be actively managed via MQTT for graceful shutdown"
                }
            }

        # Use the format that's actually stored in active bots
        bot_name_for_orchestrator = container_name if container_name in active_bots else actual_bot_name

        # Add the background task
        background_tasks.add_task(
            bots_manager.stop_and_archive_bot,
            bot_name=actual_bot_name,
            container_name=container_name,
            bot_name_for_orchestrator=bot_name_for_orchestrator,
            skip_order_cancellation=skip_order_cancellation,
            archive_locally=archive_locally,
            s3_bucket=s3_bucket,
            docker_manager=docker_manager,
            bot_archiver=bot_archiver
        )

        return {
            "status": "success",
            "message": f"Stop and archive process started for bot {actual_bot_name}",
            "details": {
                "input_name": bot_name,
                "actual_bot_name": actual_bot_name,
                "container_name": container_name,
                "process": (
                    "The bot will be gracefully stopped, archived, and removed in the background. "
                    "This process typically takes 20-30 seconds."
                )
            }
        }

    except Exception as e:
        logging.error(f"Error initiating stop_and_archive_bot for {bot_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-controllers")
async def deploy_v2_controllers(
    deployment: V2ControllerDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    accounts_service: AccountsService = Depends(get_accounts_service),
):
    """
    Deploy a V2 strategy with controllers by generating the script config and creating the instance.
    This endpoint simplifies the deployment process for V2 controller strategies.

    Args:
        deployment: V2ControllerDeployment configuration
        docker_manager: Docker service dependency

    Returns:
        Dictionary with deployment response and generated configuration details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        selected_config_ids = {
            controller[:-4] if controller.endswith(".yml") else controller
            for controller in deployment.controllers_config
        }
        unknown_overrides = set(deployment.controller_overrides) - selected_config_ids
        if unknown_overrides:
            raise HTTPException(
                status_code=400,
                detail=f"Overrides reference unselected configs: {sorted(unknown_overrides)}",
            )
        normalized_overrides = {}
        validated_controller_configs: list[dict[str, Any]] = []
        for config_id, overrides in deployment.controller_overrides.items():
            template = fs_util.read_yaml_file(f"conf/controllers/{config_id}.yml")
            controller_type = str(template.get("controller_type") or "")
            controller_name = str(template.get("controller_name") or "")
            config_class = fs_util.load_controller_config_class(controller_type, controller_name)
            if config_class is None:
                raise HTTPException(status_code=400, detail=f"Cannot validate template '{config_id}'")
            unknown_fields = set(overrides) - set(config_class.model_fields)
            if unknown_fields:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown fields for '{config_id}': {sorted(unknown_fields)}",
                )
            merged = {**template, **overrides, "id": config_id}
            validated = config_class(**merged).model_dump(mode="json")
            validated_controller_configs.append(validated)
            normalized_overrides[config_id] = {
                key: validated[key] for key in overrides if key in validated
            }
        deployment.controller_overrides = normalized_overrides

        await _validate_usdg_allowance_before_deploy(
            validated_controller_configs, bots_manager, accounts_service,
        )

        # Generate unique script config filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        script_config_filename = f"{deployment.instance_name}-{timestamp}.yml"
        # Use the same name with timestamp for the instance to ensure uniqueness
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        # Ensure controller config names have .yml extension
        controllers_with_extension = []
        for controller in deployment.controllers_config:
            if not controller.endswith('.yml'):
                controllers_with_extension.append(f"{controller}.yml")
            else:
                controllers_with_extension.append(controller)

        # Create the script config content
        # Note: candles_config and markets removed - they're optional and empty,
        # and older hummingbot versions don't expect them in the config
        script_config_content = {
            "script_file_name": "v2_with_controllers.py",
            "controllers_config": controllers_with_extension,
        }

        # Add optional drawdown parameters if provided
        if deployment.max_global_drawdown_quote is not None:
            script_config_content["max_global_drawdown_quote"] = deployment.max_global_drawdown_quote
        if deployment.max_controller_drawdown_quote is not None:
            script_config_content["max_controller_drawdown_quote"] = deployment.max_controller_drawdown_quote

        # Save the script config to the scripts directory
        scripts_dir = os.path.join("conf", "scripts")

        script_config_path = os.path.join(scripts_dir, script_config_filename)
        fs_util.dump_dict_to_yaml(script_config_path, script_config_content)

        logging.info(f"Generated script config: {script_config_filename} with content: {script_config_content}")

        # Set generated config on the deployment and deploy
        deployment.instance_name = unique_instance_name
        deployment.script_config = script_config_filename
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["script_config_generated"] = script_config_filename
            response["controllers_deployed"] = deployment.controllers_config
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            await bots_manager.create_bot_run(
                bot_name=unique_instance_name,
                instance_name=unique_instance_name,
                strategy_type="controller",
                strategy_name="v2_with_controllers",
                account_name=deployment.credentials_profile,
                config_name=script_config_filename,
                image_version=deployment.image,
                deployment_config=deployment.dict(),
                display_name=deployment.display_name,
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error deploying V2 controllers: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-script")
async def deploy_v2_script(
    deployment: V2ScriptDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Deploy a V2 script bot with optional script configuration.
    This endpoint creates and starts a Hummingbot instance running the specified script.

    Args:
        deployment: V2ScriptDeployment configuration containing instance name, credentials,
                   optional script name and configuration
        docker_manager: Docker service dependency
        db_manager: Database manager dependency

    Returns:
        Dictionary with deployment response including instance details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        # Generate unique instance name with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        # Update deployment with unique name
        deployment.instance_name = unique_instance_name

        # Create the hummingbot instance
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            await bots_manager.create_bot_run(
                bot_name=unique_instance_name,
                instance_name=unique_instance_name,
                strategy_type="script",
                strategy_name=deployment.script or "default",
                account_name=deployment.credentials_profile,
                config_name=deployment.script_config,
                image_version=deployment.image,
                deployment_config=deployment.dict(),
                display_name=deployment.display_name,
            )

        return response

    except Exception as e:
        logging.error(f"Error deploying V2 script: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/bot-runs/{bot_name}/display-name")
async def update_bot_display_name(
    bot_name: str,
    update: BotDisplayNameUpdate,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """Set or clear a Bot alias. The container name is never changed."""
    bot_run = await bots_manager.update_bot_display_name(bot_name, update.display_name)
    if bot_run is None:
        raise HTTPException(status_code=404, detail=f"Bot run '{bot_name}' not found")
    return {"status": "success", "data": bot_run}
