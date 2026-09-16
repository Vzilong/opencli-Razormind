import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCHER = ROOT / "scripts" / "patch-opencli.js"


def test_remote_daemon_patch_forwards_preferred_context_without_local_spawn(tmp_path):
    package = tmp_path / "node_modules" / "@jackwener" / "opencli" / "dist" / "src"
    browser = package / "browser"
    browser.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (package / "daemon.js").write_text(
        "httpServer.listen(PORT, '127.0.0.1', () => {", encoding="utf-8"
    )
    (browser / "daemon-lifecycle.js").write_text(
        "export async function ensureBrowserBridgeReady() { throw new Error('local spawn'); }\n",
        encoding="utf-8",
    )
    (browser / "daemon-transport.js").write_text(
        "const DAEMON_PORT = process.env.OPENCLI_DAEMON_PORT ?? '19825';\n"
        "const DAEMON_HOST = process.env.OPENCLI_DAEMON_HOST ?? '127.0.0.1';\n"
        "const DAEMON_URL = `http://${DAEMON_HOST}:${DAEMON_PORT}`;\n"
        "export async function fetchDaemonStatus(options) {\n"
        "  console.log(JSON.stringify({ daemonUrl: DAEMON_URL, options }));\n"
        "  return { ok: true };\n"
        "}\n",
        encoding="utf-8",
    )
    (browser / "bridge.js").write_text(
        "import { ensureBrowserBridgeReady } from './daemon-lifecycle.js';\n"
        "import { fetchDaemonStatus } from './daemon-transport.js';\n"
        "const DAEMON_SPAWN_TIMEOUT = 9000;\n"
        "export class Bridge {\n"
        "    async _ensureDaemon(timeoutSeconds, contextId, preferredContextId) {\n"
        "        const daemonHost = process.env.OPENCLI_DAEMON_HOST;\n"
        "        if (daemonHost && daemonHost !== '127.0.0.1' && daemonHost !== 'localhost') {\n"
        "            const remoteStatus = await fetchDaemonStatus({\n"
        "                timeout: "
        "(timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000)) * 1000,\n"
        "                contextId,\n"
        "                preferredContextId,\n"
        "            });\n"
        "            if (!remoteStatus) throw new Error('remote daemon unavailable');\n"
        "            return;\n"
        "        }\n"
        "        await ensureBrowserBridgeReady({ timeoutSeconds, contextId, "
        "preferredContextId });\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    runner = tmp_path / "run.mjs"
    runner.write_text(
        "import { Bridge } from './node_modules/@jackwener/opencli/dist/src/browser/bridge.js';\n"
        "await new Bridge()._ensureDaemon(7, 'context-a', 'preferred-b');\n",
        encoding="utf-8",
    )

    assert "OPENCLI_ADMIN_REMOTE_DAEMON_ROUTE_V2" in PATCHER.read_text(encoding="utf-8")

    result = subprocess.run(
        ["node", str(runner)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "OPENCLI_DAEMON_HOST": "remote-daemon",
            "OPENCLI_DAEMON_PORT": "19825",
        },
    )
    assert json.loads(result.stdout) == {
        "daemonUrl": "http://remote-daemon:19825",
        "options": {
            "timeout": 7000,
            "contextId": "context-a",
            "preferredContextId": "preferred-b",
        },
    }
