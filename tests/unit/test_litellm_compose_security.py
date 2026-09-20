from pathlib import Path


def test_litellm_host_port_is_loopback_only() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    service = compose.split("  aads-litellm:", 1)[1].split("  aads-searxng:", 1)[0]

    assert '"127.0.0.1:4000:4000"' in service
    assert '\n      - "4000:4000"' not in service
