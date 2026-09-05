import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List

import yaml
from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from models import Controller, ControllerType
from deps import get_accounts_service, get_bots_orchestrator
from services.accounts_service import AccountsService
from services.bots_orchestrator import BotsOrchestrator
from utils.controller_state import (
    active_position_for_config,
    import_external_position_state,
    imported_position_state,
    imported_wallet_allocations,
)
from utils.file_system import fs_util

router = APIRouter(tags=["Controllers"], prefix="/controllers")


@router.get("/", response_model=Dict[str, List[str]])
async def list_controllers():
    """
    List all controllers organized by type.

    Detects both single-file controllers (controller.py) and
    package-style controllers (controller/controller.py).

    Returns:
        Dictionary mapping controller types to lists of controller names
    """
    result = {}
    for controller_type in ControllerType:
        controllers = []
        type_path = f'controllers/{controller_type.value}'

        try:
            # Get single-file controllers (*.py files)
            files = fs_util.list_files(type_path)
            controllers.extend([
                f.replace('.py', '') for f in files
                if f.endswith('.py') and f != "__init__.py"
            ])

            # Get package-style controllers (folders with same-named .py file inside)
            folders = fs_util.list_folders(type_path)
            for folder in folders:
                if folder.startswith('__') or folder == 'examples':
                    continue
                # Check if folder contains a .py file with the same name
                try:
                    folder_files = fs_util.list_files(f'{type_path}/{folder}')
                    if f'{folder}.py' in folder_files:
                        controllers.append(folder)
                except FileNotFoundError:
                    pass

            result[controller_type.value] = sorted(set(controllers))
        except FileNotFoundError:
            result[controller_type.value] = []
    return result


# Controller Configuration endpoints (must come before controller type routes)
@router.get("/configs/", response_model=List[Dict])
async def list_controller_configs():
    """
    List all controller configurations with metadata.

    Returns:
        List of controller configuration objects with name, controller_name, controller_type, and other metadata
    """
    try:
        config_files = [f for f in fs_util.list_files('conf/controllers') if f.endswith('.yml')]
        configs = []

        for config_file in config_files:
            config_name = config_file.replace('.yml', '')
            try:
                config = fs_util.read_yaml_file(f"conf/controllers/{config_file}")
                config["id"] = config_name
                configs.append(config)
            except Exception as e:
                # If config is malformed, still include it with basic info
                configs.append({
                    "id": config_name,
                    "id": config_name,
                    "controller_name": "error",
                    "controller_type": "error",
                    "error": str(e)
                })

        return configs
    except FileNotFoundError:
        return []


@router.get("/configs/{config_name}/raw", response_model=Dict[str, str])
async def get_controller_config_raw(config_name: str):
    """读取配置文件原文，保留注释、空行和字段顺序。"""
    try:
        yaml_content = fs_util.read_file(f"conf/controllers/{config_name}.yml")
        return {"yaml_content": yaml_content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/configs/{config_name}/raw")
async def update_controller_config_raw(config_name: str, body: Dict):
    """校验 YAML 后原样保存，不重新生成文本。"""
    yaml_content = body.get("yaml_content")
    if not isinstance(yaml_content, str):
        raise HTTPException(status_code=400, detail="yaml_content must be a string")

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="YAML must parse to a mapping")

    try:
        fs_util.add_file(
            "conf/controllers",
            f"{config_name}.yml",
            yaml_content,
            override=True,
        )
        return {"message": f"Configuration '{config_name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/configs/{config_name}", response_model=Dict)
async def get_controller_config(config_name: str):
    """
    Get controller configuration by config name.

    Args:
        config_name: Name of the configuration file to retrieve

    Returns:
        Dictionary with controller configuration

    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        config = fs_util.read_yaml_file(f"conf/controllers/{config_name}.yml")
        config["id"] = config_name
        return config
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Configuration '{config_name}' is malformed: {e}")


@router.get("/configs/{config_name}/external-position")
async def get_external_position(
    config_name: str,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """读取已导入但尚未部署的 MICRODUCK 持仓。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config_name):
        raise HTTPException(status_code=400, detail="配置名称不合法")
    try:
        config = fs_util.read_yaml_file(f"conf/controllers/{config_name}.yml")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"配置 '{config_name}' 不存在")
    controller_name = str(config.get("controller_name") or "")
    if controller_name != "microduck_profit_trailing":
        raise HTTPException(status_code=400, detail="只有 MICRODUCK 跟踪策略支持导入外部持仓")
    active_instances = set(bots_manager.active_bots)
    if active_position_for_config(config_name, controller_name, active_instances):
        raise HTTPException(status_code=409, detail="这个配置已在运行，不能修改持仓")
    state = imported_position_state(config_name, controller_name)
    if state is None:
        return {"imported": False, "config_id": config_name}
    first_trade = next(
        (trade for trade in state.get("trade_history", []) if trade.get("source") == "external_import"),
        {},
    )
    return {
        "imported": True,
        "config_id": config_name,
        "position_base": str(state.get("position_base", "")),
        "entry_unit_price_usd": str(state.get("entry_unit_price_usd", "")),
        "transaction_hash": first_trade.get("transaction_hash"),
        "wallet_address": state.get("wallet_address"),
    }


@router.post("/configs/{config_name}/external-position")
async def import_external_position(
    config_name: str,
    body: Dict,
    accounts_service: AccountsService = Depends(get_accounts_service),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """为 MICRODUCK 配置导入一笔外部买入的持仓。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config_name):
        raise HTTPException(status_code=400, detail="配置名称不合法")
    try:
        config = fs_util.read_yaml_file(f"conf/controllers/{config_name}.yml")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"配置 '{config_name}' 不存在")

    if config.get("controller_name") != "microduck_profit_trailing":
        raise HTTPException(status_code=400, detail="只有 MICRODUCK 跟踪策略支持导入外部持仓")

    try:
        position_base = Decimal(str(body.get("position_base", "")))
        entry_price = Decimal(str(body.get("entry_unit_price_usd", "")))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="持仓数量和买入单价必须是有效数字")
    if not position_base.is_finite() or not entry_price.is_finite() or position_base <= 0 or entry_price <= 0:
        raise HTTPException(status_code=400, detail="持仓数量和买入单价必须大于0")

    controller_name = str(config["controller_name"])
    active_instances = set(bots_manager.active_bots)
    if active_position_for_config(config_name, controller_name, active_instances):
        raise HTTPException(
            status_code=409,
            detail="这个配置已有运行中或尚未归档的持仓，不能再次导入",
        )

    wallet_address = str(config.get("wallet_address") or "").strip()
    chain = str(config.get("chain") or "ethereum")
    network = str(config.get("network") or "robinhoodchain")
    if not wallet_address:
        raise HTTPException(status_code=400, detail="配置中缺少 wallet_address")

    try:
        response = await accounts_service.gateway_client.get_balances(
            chain, network, wallet_address, tokens=["MICRODUCK"]
        )
        balances = response.get("balances", {}) if isinstance(response, dict) else {}
        wallet_balance = Decimal(
            str(balances.get("MICRODUCK", balances.get("microduck", "0")))
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"无法读取钱包余额：{exc}")

    allocated = imported_wallet_allocations(
        wallet_address,
        exclude_config_id=config_name,
        active_instance_names=active_instances,
    )
    if allocated + position_base > wallet_balance:
        raise HTTPException(
            status_code=409,
            detail=(
                f"导入后将超过钱包余额：钱包有 {wallet_balance} MICRODUCK，"
                f"其他配置已占用 {allocated}，本次要求 {position_base}"
            ),
        )

    transaction_hash = str(body.get("transaction_hash") or "").strip() or None
    if transaction_hash and not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
        raise HTTPException(status_code=400, detail="交易哈希必须是0x开头的64位十六进制值")
    was_imported = imported_position_state(config_name, controller_name) is not None
    import_external_position_state(
        config_id=config_name,
        controller_name=controller_name,
        wallet_address=wallet_address,
        position_base=str(position_base),
        entry_unit_price_usd=str(entry_price),
        transaction_hash=transaction_hash,
    )
    return {
        "imported": True,
        "updated": was_imported,
        "config_id": config_name,
        "wallet_address": wallet_address,
        "position_base": str(position_base),
        "entry_unit_price_usd": str(entry_price),
        "wallet_balance": str(wallet_balance),
        "allocated_to_other_configs": str(allocated),
    }


@router.post("/configs/{config_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_controller_config(config_name: str, config: Dict):
    """
    Create or update controller configuration.

    Args:
        config_name: Name of the configuration file
        config: Configuration dictionary to save

    Returns:
        Success message when configuration is saved

    Raises:
        HTTPException: 400 if save error occurs
    """
    try:
        yaml_content = yaml.dump(config, default_flow_style=False)
        fs_util.add_file('conf/controllers', f"{config_name}.yml", yaml_content, override=True)
        return {"message": f"Configuration '{config_name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/configs/{config_name}")
async def delete_controller_config(config_name: str):
    """
    Delete controller configuration.

    Args:
        config_name: Name of the configuration file to delete

    Returns:
        Success message when configuration is deleted

    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        fs_util.delete_file('conf/controllers', f"{config_name}.yml")
        return {"message": f"Configuration '{config_name}' deleted successfully"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")


@router.get("/{controller_type}/{controller_name}", response_model=Dict[str, str])
async def get_controller(controller_type: ControllerType, controller_name: str):
    """
    Get controller content by type and name.

    Supports both single-file controllers (controller.py) and
    package-style controllers (controller/controller.py).

    Args:
        controller_type: Type of the controller
        controller_name: Name of the controller

    Returns:
        Dictionary with controller name, type, and content

    Raises:
        HTTPException: 404 if controller not found
    """
    # Try single-file first, then package-style
    paths_to_try = [
        f"controllers/{controller_type.value}/{controller_name}.py",
        f"controllers/{controller_type.value}/{controller_name}/{controller_name}.py",
    ]

    for path in paths_to_try:
        try:
            content = fs_util.read_file(path)
            return {
                "name": controller_name,
                "type": controller_type.value,
                "content": content
            }
        except FileNotFoundError:
            continue

    raise HTTPException(
        status_code=404,
        detail=f"Controller '{controller_name}' not found in '{controller_type.value}'"
    )


@router.post("/{controller_type}/{controller_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_controller(controller_type: ControllerType, controller_name: str, controller: Controller):
    """
    Create or update a controller.

    If controller exists as a package (folder), updates the file inside.
    Otherwise creates/updates as a single file.

    Args:
        controller_type: Type of controller to create/update
        controller_name: Name of the controller (from URL path)
        controller: Controller object with content (and optional type for validation)

    Returns:
        Success message when controller is saved

    Raises:
        HTTPException: 400 if controller type mismatch or save error
    """
    # If type is provided in body, validate it matches URL
    if controller.type is not None and controller.type != controller_type:
        raise HTTPException(
            status_code=400,
            detail=f"Controller type mismatch: URL has '{controller_type}', body has '{controller.type}'"
        )

    try:
        type_path = f'controllers/{controller_type.value}'
        package_path = f'{type_path}/{controller_name}'

        # Check if controller exists as a package (folder with same-named .py file)
        if fs_util.path_exists(package_path):
            fs_util.add_file(package_path, f"{controller_name}.py", controller.content, override=True)
        else:
            fs_util.add_file(type_path, f"{controller_name}.py", controller.content, override=True)

        return {"message": f"Controller '{controller_name}' saved successfully in '{controller_type.value}'"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{controller_type}/{controller_name}")
async def delete_controller(controller_type: ControllerType, controller_name: str):
    """
    Delete a controller.

    Handles both single-file and package-style controllers.

    Args:
        controller_type: Type of the controller
        controller_name: Name of the controller to delete

    Returns:
        Success message when controller is deleted

    Raises:
        HTTPException: 404 if controller not found
    """
    type_path = f'controllers/{controller_type.value}'

    # Try single-file first
    try:
        fs_util.delete_file(type_path, f"{controller_name}.py")
        return {"message": f"Controller '{controller_name}' deleted successfully from '{controller_type.value}'"}
    except FileNotFoundError:
        pass

    # Try package-style (delete entire folder)
    try:
        fs_util.delete_folder(type_path, controller_name)
        return {"message": f"Controller '{controller_name}' deleted successfully from '{controller_type.value}'"}
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Controller '{controller_name}' not found in '{controller_type.value}'"
        )


@router.get("/{controller_type}/{controller_name}/config/template")
async def get_controller_config_template(controller_type: ControllerType, controller_name: str):
    """
    Get controller configuration template with default values.

    Args:
        controller_type: Type of the controller
        controller_name: Name of the controller

    Returns:
        Dictionary with configuration template and default values

    Raises:
        HTTPException: 404 if controller configuration class not found
    """
    config_class = fs_util.load_controller_config_class(controller_type.value, controller_name)
    if config_class is None:
        raise HTTPException(
            status_code=404,
            detail=f"Controller configuration class for '{controller_name}' not found"
        )

    # Extract fields and default values
    config_fields = {name: {"default": field.default,
                            "type": field.annotation,
                            "required": field.required if hasattr(field, 'required') else False,
                            } for name, field in config_class.model_fields.items()}
    return json.loads(json.dumps(config_fields, default=str))


@router.post("/{controller_type}/{controller_name}/config/validate")
async def validate_controller_config(controller_type: ControllerType, controller_name: str, config: Dict):
    """
    Validate controller configuration against the controller's config class.

    Args:
        controller_type: Type of the controller
        controller_name: Name of the controller
        config: Configuration dictionary to validate

    Returns:
        Success message if configuration is valid

    Raises:
        HTTPException: 400 if validation fails
    """
    config_class = fs_util.load_controller_config_class(controller_type.value, controller_name)
    if config_class is None:
        raise HTTPException(
            status_code=404,
            detail=f"Controller configuration class for '{controller_name}' not found"
        )

    try:
        config_class(**config)  # Validate by instantiating the model
        return {"message": "Configuration is valid"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# Bot-specific controller config endpoints
@router.get("/bots/{bot_name}/configs", response_model=List[Dict])
async def get_bot_controller_configs(bot_name: str):
    """
    Get all controller configurations for a specific bot.

    Args:
        bot_name: Name of the bot to get configurations for

    Returns:
        List of controller configurations for the bot

    Raises:
        HTTPException: 404 if bot not found
    """
    bots_config_path = f"instances/{bot_name}/conf/controllers"
    if not fs_util.path_exists(bots_config_path):
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")

    configs = []
    for controller_file in fs_util.list_files(bots_config_path):
        if controller_file.endswith('.yml'):
            config = fs_util.read_yaml_file(f"{bots_config_path}/{controller_file}")
            config['_config_name'] = controller_file.replace('.yml', '')
            configs.append(config)
    return configs


@router.post("/bots/{bot_name}/{controller_name}/config")
async def update_bot_controller_config(bot_name: str, controller_name: str, config: Dict):
    """
    Update controller configuration for a specific bot.

    Args:
        bot_name: Name of the bot
        controller_name: Name of the controller to update
        config: Configuration dictionary to update with

    Returns:
        Success message when configuration is updated

    Raises:
        HTTPException: 404 if bot or controller not found, 400 if update error
    """
    bots_config_path = f"instances/{bot_name}/conf/controllers"
    if not fs_util.path_exists(bots_config_path):
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")

    try:
        config_path = f"{bots_config_path}/{controller_name}.yml"
        current_config = fs_util.read_yaml_file(config_path)
        confirm_live_trading = bool(config.pop("_confirm_live_trading", False))
        merged = {**current_config, **{key: value for key, value in config.items() if not key.startswith("_")}}
        controller_type = str(current_config.get("controller_type") or "")
        controller_class_name = str(current_config.get("controller_name") or "")
        config_class = fs_util.load_controller_config_class(controller_type, controller_class_name)
        if config_class is None:
            raise HTTPException(status_code=400, detail="无法加载控制器配置模型")

        current_validated = config_class(**current_config).model_dump(mode="json")
        updated_validated = config_class(**merged).model_dump(mode="json")
        changed_fields = {
            key for key, value in updated_validated.items()
            if current_validated.get(key) != value
        }
        updatable_fields = {
            name for name, field in config_class.model_fields.items()
            if (field.json_schema_extra or {}).get("is_updatable", False)
        }
        forbidden = changed_fields - updatable_fields
        if forbidden:
            raise HTTPException(
                status_code=400,
                detail=f"运行中不能修改这些字段：{', '.join(sorted(forbidden))}",
            )

        if not current_validated.get("live_trading") and updated_validated.get("live_trading"):
            if not confirm_live_trading:
                raise HTTPException(status_code=409, detail="开启真实交易需要再次确认")

        state_path = os.path.join(
            "bots", "instances", bot_name, "data",
            f"{controller_name}_{controller_class_name}.json",
        )
        runtime_state = ""
        if os.path.isfile(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as file:
                    runtime_state = str((json.load(file) or {}).get("state") or "")
            except (OSError, ValueError, json.JSONDecodeError):
                runtime_state = ""
            if runtime_state in {"buying", "selling"} and changed_fields - {"status_log_interval_seconds"}:
                raise HTTPException(status_code=409, detail="交易正在确认中，暂时不能修改交易参数")
            buy_size_fields = {"buy_size_mode", "buy_budget_usd", "buy_amount_base"}
            # 已完成代表本轮已经结束且没有持仓；允许调整下一次使用的买入参数。
            # 仍持仓、卖出跟踪、外部退出和交易确认中的状态保持禁止修改。
            if runtime_state not in {"", "waiting_to_buy", "trailing_buy", "completed"} and changed_fields & buy_size_fields:
                raise HTTPException(
                    status_code=409,
                    detail="已经持仓或进入卖出阶段，不能修改买入方式、预算或数量",
                )

        full_path = fs_util._get_full_path(config_path)
        temporary_path = f"{full_path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            yaml.safe_dump(updated_validated, file, allow_unicode=True, sort_keys=False)
        os.replace(temporary_path, full_path)
        # 已完成的一轮没有持仓或挂起交易。修改交易参数时，为下一轮写入可恢复的等待状态；
        # 交易历史和累计利润仍保留在原状态文件中。
        restart_fields = {
            "buy_size_mode", "buy_budget_usd", "buy_amount_base",
            "buy_price_min_usd", "buy_price_upward_tolerance_usd",
            "buy_trailing_rebound_mode", "buy_trailing_rebound_usd",
            "buy_trailing_rebound_percent", "buy_trailing_rebound_adjustment_factor",
            "buy_trailing_rebound_max_percent", "sell_profit_multiple",
            "sell_price_max_usd", "sell_price_downward_tolerance_usd",
            "sell_trailing_drop_mode", "sell_trailing_drop_usd", "sell_trailing_drop_percent",
            "normal_check_interval", "trailing_check_interval", "live_trading",
        }
        should_restart_completed = bool(changed_fields & restart_fields) or (
            "auto_start_next_cycle" in changed_fields
            and bool(updated_validated.get("auto_start_next_cycle"))
        )
        if runtime_state == "completed" and should_restart_completed:
            try:
                with open(state_path, "r", encoding="utf-8") as file:
                    completed_state = json.load(file) or {}
                completed_state.update({
                    "state": "waiting_to_buy",
                    "trough_unit_buy_price_usd": "0",
                    "peak_unit_sell_price_usd": "0",
                    "entry_unit_price_usd": "0",
                    "position_base": "0",
                    "pending_executor_id": None,
                    "pending_side": None,
                    "balance_before_base": "0",
                    "balance_before_quote": "0",
                    "external_balance_change": None,
                })
                temporary_state_path = f"{state_path}.tmp"
                with open(temporary_state_path, "w", encoding="utf-8") as file:
                    json.dump(completed_state, file, ensure_ascii=False, indent=2)
                os.replace(temporary_state_path, state_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=500, detail=f"配置已保存，但无法重置下一轮状态：{exc}")
        return {
            "message": f"Controller configuration for bot '{bot_name}' updated successfully",
            "changed_fields": sorted(changed_fields),
            "applies_within_seconds": 10,
        }
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Controller configuration '{controller_name}' not found for bot '{bot_name}'"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
