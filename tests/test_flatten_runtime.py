import pytest

from tools.flatten_runtime import dockerfile, verify


def test_preserves_runtime_configuration_and_does_not_expand_environment():
    config = {
        "Env": ["PATH=/usr/bin", "LITERAL=$HOME"],
        "User": "65532:65532",
        "WorkingDir": "/opt/pistachio",
        "Cmd": ["python", "-m", "worker"],
        "Entrypoint": None,
        "Labels": {"revision": "abc"},
    }
    before = {"Config": config, "Architecture": "amd64", "Os": "linux"}
    text = dockerfile(before)
    assert 'ENV LITERAL="\\$HOME"' in text
    assert 'CMD ["python", "-m", "worker"]' in text
    assert "USER 65532:65532" in text and "FROM scratch" in text
    verify(before, {**before, "RootFS": {"Layers": ["single"]}})
    with pytest.raises(AssertionError):
        verify(
            before,
            {**before, "Config": {**config, "User": "root"}, "RootFS": {"Layers": ["single"]}},
        )


def test_unhandled_runtime_metadata_is_rejected():
    with pytest.raises(ValueError, match="Healthcheck"):
        dockerfile({"Config": {"Healthcheck": {"Test": ["CMD", "true"]}}})
