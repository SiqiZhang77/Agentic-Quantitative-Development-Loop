import os
import subprocess
from pathlib import Path
import pytest

docker = pytest.importorskip("docker")

IMAGE_TAG = "agenticloop-sandbox:test"
_SANDBOX_DIR = Path(__file__).parent.parent  # rae_runtime/sandbox/
_RAE_RUNTIME_DIR = _SANDBOX_DIR.parent  # rae_runtime/
BUILD_CONTEXT = str(_RAE_RUNTIME_DIR)
DOCKERFILE = str(_SANDBOX_DIR / "Dockerfile")


@pytest.fixture(scope="session")
def sandbox_image():
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", DOCKERFILE, BUILD_CONTEXT],
        check=True,
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )
    yield IMAGE_TAG
    docker.from_env().images.remove(IMAGE_TAG, force=True)


def test_refuses_root(sandbox_image):
    client = docker.from_env()
    with pytest.raises(docker.errors.ContainerError) as exc_info:
        client.containers.run(
            sandbox_image,
            command="true",
            user="0",
            remove=True,
        )
    err = exc_info.value
    assert err.exit_status == 1
    assert b"must not run as root" in err.stderr


def test_allows_correct_user(sandbox_image):
    client = docker.from_env()
    client.containers.run(
        sandbox_image,
        command="true",
        user="1000",
        remove=True,
    )
