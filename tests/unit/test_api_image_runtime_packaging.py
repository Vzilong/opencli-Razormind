import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _docker_stages(source: str) -> list[tuple[str, str, str]]:
    headers = list(
        re.finditer(r"^FROM\s+(.+?)\s+AS\s+([\w-]+)\s*$", source, flags=re.MULTILINE)
    )
    return [
        (
            match.group(2),
            match.group(1),
            source[match.end() : headers[index + 1].start() if index + 1 < len(headers) else None],
        )
        for index, match in enumerate(headers)
    ]


def test_api_image_defaults_to_production_runtime_without_acceptance_fixtures():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = _docker_stages(dockerfile)
    stage_names = [name for name, _, _ in stages]
    stage_map = {name: (base, body) for name, base, body in stages}

    assert stage_names[-1] == "production"
    assert stage_map["production"][0] == "runtime"

    runtime = stage_map["runtime"][1]
    assert 'EXPOSE 8000' in runtime
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in runtime
    assert 'CMD ["uvicorn", "backend.main:app"' in runtime
    assert "tests/acceptance" not in runtime
    assert "OPENCLI_BIN" not in runtime
    assert "III_CLI_PATH" not in runtime

    production = stage_map["production"][1]
    assert "tests/acceptance" not in production
    assert "opencli-proof" not in production
    assert "OPENCLI_BIN" not in production
    assert "III_CLI_PATH" not in production


def test_acceptance_target_retains_fixture_tools_and_compose_targets_production():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stage_map = {name: (base, body) for name, base, body in _docker_stages(dockerfile)}
    acceptance_base, acceptance = stage_map["non-bypass-acceptance"]

    assert acceptance_base == "runtime"
    assert "COPY tests/acceptance/fixtures/opencli-proof" in acceptance
    assert "COPY tests/acceptance/non_bypass_vertical.py" in acceptance
    assert "OPENCLI_BIN=/opt/non-bypass/opencli-proof" in acceptance
    assert "III_CLI_PATH=/opt/iii/iii" in acceptance

    compose = yaml.safe_load((ROOT / "docker-compose.build.yml").read_text(encoding="utf-8"))
    for service in ("api", "worker", "beat"):
        assert compose["services"][service]["build"]["target"] == "production"

    acceptance_compose = yaml.safe_load(
        (ROOT / "docker-compose.non-bypass-acceptance.yml").read_text(encoding="utf-8")
    )
    assert (
        acceptance_compose["services"]["proof-admin"]["build"]["target"]
        == "non-bypass-acceptance"
    )
