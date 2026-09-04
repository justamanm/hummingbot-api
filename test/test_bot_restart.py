from unittest.mock import Mock

from docker.errors import DockerException

from services.docker_service import DockerService


def test_restart_container_restarts_existing_container_without_removal():
    service = object.__new__(DockerService)
    container = Mock()
    service.client = Mock()
    service.client.containers.get.return_value = container

    result = service.restart_container("bot_example")

    assert result == {
        "success": True,
        "message": "Container bot_example restarted successfully.",
    }
    container.restart.assert_called_once_with()
    container.remove.assert_not_called()


def test_restart_container_returns_failure_without_removal_when_docker_fails():
    service = object.__new__(DockerService)
    container = Mock()
    container.restart.side_effect = DockerException("restart failed")
    service.client = Mock()
    service.client.containers.get.return_value = container

    result = service.restart_container("bot_example")

    assert result == {"success": False, "message": "restart failed"}
    container.remove.assert_not_called()
