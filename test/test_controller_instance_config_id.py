from services.docker_service import DockerService


def test_instance_copy_gets_filename_id_without_changing_comments(tmp_path):
    config_path = tmp_path / "microduck.yml"
    original = (
        "# 用户注释必须保留\n"
        "\n"
        "controller_name: microduck_profit_trailing\n"
        "controller_type: generic\n"
    )
    config_path.write_text(original, encoding="utf-8")

    DockerService._ensure_controller_config_id(
        str(config_path), "microduck_012_013_profit50_observe.yml"
    )

    assert config_path.read_text(encoding="utf-8") == (
        "id: microduck_012_013_profit50_observe\n" + original
    )


def test_existing_id_is_left_byte_for_byte_unchanged(tmp_path):
    config_path = tmp_path / "microduck.yml"
    original = "# 用户注释\nid: custom_id\ncontroller_type: generic\n"
    config_path.write_text(original, encoding="utf-8")

    DockerService._ensure_controller_config_id(
        str(config_path), "microduck_012_013_profit50_observe.yml"
    )

    assert config_path.read_text(encoding="utf-8") == original
