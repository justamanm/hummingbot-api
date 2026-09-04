import json
from decimal import Decimal

from utils.controller_state import (
    active_position_for_config,
    import_external_position_state,
    imported_position_state,
    imported_wallet_allocations,
    restore_controller_states,
)


def write_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "id: microduck\ncontroller_name: microduck_profit_trailing\n",
        encoding="utf-8",
    )


def write_state(path, state, entry_price):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"state": state, "entry_unit_price_usd": entry_price}),
        encoding="utf-8",
    )


def test_restore_uses_the_latest_archived_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    instance_dir = tmp_path / "bots/instances/new-bot"
    write_config(instance_dir / "conf/controllers/microduck.yml")
    older = tmp_path / "bots/archived/old/data/microduck_microduck_profit_trailing.json"
    newer = tmp_path / "bots/archived/new/data/microduck_microduck_profit_trailing.json"
    write_state(older, "waiting_to_buy", "0")
    write_state(newer, "holding", "0.014")
    older.touch()
    newer.touch()

    restored = restore_controller_states(
        str(instance_dir), ["microduck.yml"]
    )

    destination = instance_dir / "data/microduck_microduck_profit_trailing.json"
    assert restored == ["microduck_microduck_profit_trailing.json"]
    assert json.loads(destination.read_text(encoding="utf-8"))["state"] == "holding"


def test_fresh_deployment_does_not_restore_template_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    instance_dir = tmp_path / "bots/instances/fresh"
    write_config(instance_dir / "conf/controllers/microduck.yml")
    write_state(tmp_path / "bots/archived/old/data/microduck_microduck_profit_trailing.json", "external_exit", "0.014")
    write_state(tmp_path / "bots/controller_states/microduck_microduck_profit_trailing.json", "external_exit", "0.014")
    assert restore_controller_states(str(instance_dir), ["microduck.yml"], restore_archived=False) == []
    assert not (instance_dir / "data/microduck_microduck_profit_trailing.json").exists()


def test_fresh_deployment_still_restores_explicit_import(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    instance_dir = tmp_path / "bots/instances/imported"
    write_config(instance_dir / "conf/controllers/microduck.yml")
    import_external_position_state(config_id="microduck", controller_name="microduck_profit_trailing", wallet_address="0xabc", position_base="10", entry_unit_price_usd="0.02")
    assert restore_controller_states(str(instance_dir), ["microduck.yml"], restore_archived=False)


def test_restore_never_overwrites_an_existing_instance_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    instance_dir = tmp_path / "bots/instances/new-bot"
    write_config(instance_dir / "conf/controllers/microduck.yml")
    archived = tmp_path / "bots/archived/old/data/microduck_microduck_profit_trailing.json"
    destination = instance_dir / "data/microduck_microduck_profit_trailing.json"
    write_state(archived, "holding", "0.014")
    write_state(destination, "trailing", "0.015")

    restored = restore_controller_states(
        str(instance_dir), ["microduck.yml"]
    )

    assert restored == []
    assert json.loads(destination.read_text(encoding="utf-8"))["state"] == "trailing"


def test_external_position_is_restored_on_next_deploy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    instance_dir = tmp_path / "bots/instances/new-bot"
    write_config(instance_dir / "conf/controllers/microduck.yml")
    import_external_position_state(
        config_id="microduck",
        controller_name="microduck_profit_trailing",
        wallet_address="0xabc",
        position_base="80",
        entry_unit_price_usd="0.014",
        transaction_hash="0xtx",
    )

    restored = restore_controller_states(str(instance_dir), ["microduck.yml"])

    destination = instance_dir / "data/microduck_microduck_profit_trailing.json"
    state = json.loads(destination.read_text(encoding="utf-8"))
    assert restored == ["microduck_microduck_profit_trailing.json"]
    assert state["state"] == "holding"
    assert state["position_base"] == "80"
    assert state["entry_unit_price_usd"] == "0.014"
    assert state["trade_history"][0]["source"] == "external_import"


def test_imported_position_can_be_read_and_replaced_before_deploy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    arguments = {
        "config_id": "microduck",
        "controller_name": "microduck_profit_trailing",
        "wallet_address": "0xabc",
        "position_base": "600",
        "transaction_hash": None,
    }
    import_external_position_state(entry_unit_price_usd="0.001237", **arguments)
    assert imported_position_state(
        "microduck", "microduck_profit_trailing"
    )["entry_unit_price_usd"] == "0.001237"

    import_external_position_state(entry_unit_price_usd="0.01237", **arguments)
    state = imported_position_state("microduck", "microduck_profit_trailing")

    assert state["state"] == "holding"
    assert state["position_base"] == "600"
    assert state["entry_unit_price_usd"] == "0.01237"
    assert state["trade_history"][0]["total_usd"] == "7.42200"


def test_wallet_allocations_exclude_the_config_being_replaced(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for config_id, amount in (("trade-a", "80"), ("trade-b", "120")):
        import_external_position_state(
            config_id=config_id,
            controller_name="microduck_profit_trailing",
            wallet_address="0xAbC",
            position_base=amount,
            entry_unit_price_usd="0.014",
        )

    assert imported_wallet_allocations("0xabc") == Decimal("200")
    assert imported_wallet_allocations("0xabc", exclude_config_id="trade-a") == Decimal("120")


def test_completed_newer_state_releases_wallet_allocation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import_external_position_state(
        config_id="trade-a",
        controller_name="microduck_profit_trailing",
        wallet_address="0xabc",
        position_base="80",
        entry_unit_price_usd="0.014",
    )
    completed = tmp_path / "bots/archived/new/data/trade-a_microduck_profit_trailing.json"
    write_state(completed, "completed", "0.014")
    state = json.loads(completed.read_text(encoding="utf-8"))
    state.update({"position_base": "0", "wallet_address": "0xabc"})
    completed.write_text(json.dumps(state), encoding="utf-8")

    assert imported_wallet_allocations("0xabc") == Decimal("0")


def test_active_position_detects_unarchived_holding(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / "bots/instances/live/data/microduck_microduck_profit_trailing.json"
    write_state(state_path, "holding", "0.014")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["position_base"] = "80"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert active_position_for_config("microduck", "microduck_profit_trailing") is not None


def test_stale_instance_directory_is_not_treated_as_running(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / "bots/instances/stale/data/microduck_microduck_profit_trailing.json"
    write_state(state_path, "holding", "0.014")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["position_base"] = "80"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert active_position_for_config(
        "microduck", "microduck_profit_trailing", set()
    ) is None
    assert active_position_for_config(
        "microduck", "microduck_profit_trailing", {"stale"}
    ) is not None


def test_stale_instance_does_not_reserve_wallet_balance(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / "bots/instances/stale/data/trade-a_microduck_profit_trailing.json"
    write_state(state_path, "holding", "0.014")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"position_base": "80", "wallet_address": "0xabc"})
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert imported_wallet_allocations("0xabc", active_instance_names=set()) == Decimal("0")
    assert imported_wallet_allocations("0xabc", active_instance_names={"stale"}) == Decimal("80")
