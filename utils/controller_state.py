import json
import logging
import os
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import yaml


logger = logging.getLogger(__name__)

IMPORTED_STATES_DIR = os.path.join("bots", "controller_states")


def controller_state_filename(config_id: str, controller_name: str) -> str:
    return f"{config_id}_{controller_name}.json"


def import_external_position_state(
    *,
    config_id: str,
    controller_name: str,
    wallet_address: str,
    position_base: str,
    entry_unit_price_usd: str,
    transaction_hash: str | None = None,
) -> str:
    """保存一份外部持仓，供该配置下次部署时恢复。"""
    os.makedirs(IMPORTED_STATES_DIR, exist_ok=True)
    filename = controller_state_filename(config_id, controller_name)
    path = os.path.join(IMPORTED_STATES_DIR, filename)
    payload = {
        "state": "holding",
        "trough_unit_buy_price_usd": "0",
        "peak_unit_sell_price_usd": "0",
        "entry_unit_price_usd": entry_unit_price_usd,
        "position_base": position_base,
        "pending_executor_id": None,
        "pending_side": None,
        "balance_before_base": "0",
        "balance_before_quote": "0",
        "realized_pnl_quote": "0",
        "wallet_address": wallet_address,
        "external_position": True,
        "trade_history": [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "side": "BUY",
                "price_usd": entry_unit_price_usd,
                "amount_base": position_base,
                "total_usd": str(Decimal(position_base) * Decimal(entry_unit_price_usd)),
                "fee_native": "0",
                "transaction_hash": transaction_hash,
                "source": "external_import",
            }
        ],
    }
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temporary_path, path)
    return path


def imported_position_state(config_id: str, controller_name: str) -> dict | None:
    """读取尚未部署的外部持仓，供界面回填和修正。"""
    path = os.path.join(
        IMPORTED_STATES_DIR,
        controller_state_filename(config_id, controller_name),
    )
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not state.get("external_position"):
        return None
    return state


def active_position_for_config(
    config_id: str,
    controller_name: str,
    active_instance_names: set[str] | None = None,
) -> dict | None:
    """读取真正运行中实例的持仓；遗留实例目录不能算作运行中。"""
    filename = controller_state_filename(config_id, controller_name)
    instances_dir = os.path.join("bots", "instances")
    if not os.path.isdir(instances_dir):
        return None
    candidates = []
    for instance_name in os.listdir(instances_dir):
        if active_instance_names is not None and instance_name not in active_instance_names:
            continue
        path = os.path.join(instances_dir, instance_name, "data", filename)
        if os.path.isfile(path):
            candidates.append(path)
    if not candidates:
        return None
    path = max(candidates, key=os.path.getmtime)
    try:
        with open(path, "r", encoding="utf-8") as file:
            state = json.load(file)
        if state.get("state") in {"holding", "trailing", "buying", "selling"} and float(
            state.get("position_base", 0)
        ) > 0:
            return state
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return None
    return None


def imported_wallet_allocations(
    wallet_address: str,
    exclude_config_id: str = "",
    active_instance_names: set[str] | None = None,
) -> Decimal:
    """统计其他配置当前仍在管理的 MICRODUCK 数量。"""
    total = Decimal("0")
    wallet = wallet_address.lower()
    candidates_by_filename: dict[str, list[str]] = {}
    state_roots = [IMPORTED_STATES_DIR]
    for group in ("instances", "archived"):
        group_dir = os.path.join("bots", group)
        if os.path.isdir(group_dir):
            state_roots.extend(
                os.path.join(group_dir, name, "data")
                for name in os.listdir(group_dir)
                if group != "instances"
                or active_instance_names is None
                or name in active_instance_names
            )
    for root in state_roots:
        if not os.path.isdir(root):
            continue
        for filename in os.listdir(root):
            if filename.endswith("_microduck_profit_trailing.json"):
                candidates_by_filename.setdefault(filename, []).append(os.path.join(root, filename))

    for filename, candidates in candidates_by_filename.items():
        path = max(candidates, key=os.path.getmtime)
        try:
            with open(path, "r", encoding="utf-8") as file:
                state = json.load(file)
            if state.get("wallet_address", "").lower() != wallet:
                continue
            if exclude_config_id and filename.startswith(f"{exclude_config_id}_"):
                continue
            if state.get("state") in {"holding", "trailing", "buying", "selling"}:
                total += Decimal(str(state.get("position_base", 0)))
        except (OSError, ValueError, InvalidOperation, json.JSONDecodeError, TypeError):
            continue
    return total


def restore_controller_states(instance_dir: str, controller_files: list[str], *, restore_archived: bool = True) -> list[str]:
    """把每个控制器最近一次归档状态复制到新实例，避免重新部署后丢失持仓。"""
    restored: list[str] = []
    destination_data_dir = os.path.join(instance_dir, "data")
    os.makedirs(destination_data_dir, exist_ok=True)
    archived_dir = os.path.join("bots", "archived")

    for controller_file in controller_files:
        config_path = os.path.join(instance_dir, "conf", "controllers", controller_file)
        if not os.path.isfile(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                config_data = yaml.safe_load(file) or {}
            config_id = str(config_data.get("id") or os.path.splitext(controller_file)[0])
            controller_name = str(config_data.get("controller_name") or "")
            if not controller_name:
                continue
            state_filename = controller_state_filename(config_id, controller_name)
            destination_path = os.path.join(destination_data_dir, state_filename)
            if os.path.exists(destination_path):
                continue

            candidates = []
            imported_candidate = os.path.join(IMPORTED_STATES_DIR, state_filename)
            if os.path.isfile(imported_candidate) and (
                restore_archived or imported_position_state(config_id, controller_name)
            ):
                candidates.append(imported_candidate)
            if restore_archived and os.path.isdir(archived_dir):
                for archived_name in os.listdir(archived_dir):
                    candidate = os.path.join(
                        archived_dir, archived_name, "data", state_filename
                    )
                    if os.path.isfile(candidate):
                        candidates.append(candidate)
            if not candidates:
                continue

            source_path = max(candidates, key=os.path.getmtime)
            with open(source_path, "r", encoding="utf-8") as file:
                saved_state = json.load(file)
            if not isinstance(saved_state, dict) or "state" not in saved_state:
                logger.warning("Ignored invalid controller state: %s", source_path)
                continue

            shutil.copy2(source_path, destination_path)
            restored.append(state_filename)
            logger.info("Restored controller state %s from %s", state_filename, source_path)
        except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not restore state for controller %s: %s",
                controller_file,
                exc,
            )
    return restored
