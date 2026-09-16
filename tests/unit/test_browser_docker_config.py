from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_build_override_passes_engine_and_version_to_browser_images():
    compose = (ROOT / "docker-compose.build.yml").read_text(encoding="utf-8")
    assert "BROWSER_ENGINE: ${BROWSER_ENGINE-chromium}" in compose
    assert "BROWSER_ENGINE: ${BROWSER_ENGINE:-chromium}" not in compose
    assert "CLOAKBROWSER_VERSION: ${CLOAKBROWSER_VERSION:-0.5.10}" in compose
    assert compose.count("BROWSER_ENGINE") >= 2


def test_builtin_browser_passes_runtime_engine_without_baking_license():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "BROWSER_ENGINE: ${BROWSER_ENGINE-chromium}" in compose
    assert "BROWSER_ENGINE: ${BROWSER_ENGINE:-chromium}" not in compose
    assert "CLOAKBROWSER_CACHE_DIR:" in compose
    assert "CLOAKBROWSER_BINARY_PATH: ${CLOAKBROWSER_BINARY_PATH:-}" in compose
    assert "CLOAKBROWSER_LICENSE_KEY: ${CLOAKBROWSER_LICENSE_KEY:-}" in compose


def test_env_docs_keep_cloakbrowser_opt_in_and_fail_closed():
    for name in (".env.example", ".env.docker.example"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "BROWSER_ENGINE=chromium" in text
        assert "CLOAKBROWSER_VERSION=0.5.10" in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "CloakBrowser" in readme
    assert "不自动降级" in readme
