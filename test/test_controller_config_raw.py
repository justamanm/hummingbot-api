import pytest
from fastapi import HTTPException

from routers.controllers import (
    get_controller_config_raw,
    update_controller_config_raw,
)
from utils.file_system import fs_util


@pytest.mark.asyncio
async def test_raw_config_round_trip_preserves_text(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_util, "base_path", str(tmp_path))
    yaml_content = (
        "# 安全与运行模式, 测试模式-false，正常-true\n"
        "\n"
        "live_trading: true\n"
        "manual_kill_switch: true\n"
    )

    await update_controller_config_raw(
        "comment_round_trip", {"yaml_content": yaml_content}
    )
    result = await get_controller_config_raw("comment_round_trip")

    assert result["yaml_content"] == yaml_content
    assert (
        tmp_path / "conf/controllers/comment_round_trip.yml"
    ).read_text(encoding="utf-8") == yaml_content


@pytest.mark.asyncio
async def test_raw_config_rejects_invalid_yaml_without_overwriting(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_util, "base_path", str(tmp_path))
    config_dir = tmp_path / "conf/controllers"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "comment_round_trip.yml"
    config_path.write_text("live_trading: true\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        await update_controller_config_raw(
            "comment_round_trip", {"yaml_content": "live_trading: ["}
        )

    assert exc_info.value.status_code == 400
    assert config_path.read_text(encoding="utf-8") == "live_trading: true\n"
