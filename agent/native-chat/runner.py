"""Linux bubblewrap 原生推理入口；凭据、工作目录与控制平面相互隔离。"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("/opt/opencli-native-chat")


def command(runtime: str, arguments: list[str]) -> list[str]:
    if runtime not in {"codex", "omp"}:
        raise ValueError("unsupported native runtime")
    terminal = bool(arguments and arguments[0] == "--terminal")
    if terminal:
        arguments = arguments[1:]
    work_root = (ROOT / "work").resolve(strict=True)
    cwd = (
        work_root
        if arguments in (["--version"], ["--operator-chat-probe"])
        else Path.cwd().resolve(strict=True)
    )
    if not cwd.is_relative_to(work_root):
        raise ValueError("native working directory is outside the isolated work root")
    home = "/home/agent"
    credential_home = f"{home}/.codex" if runtime == "codex" else f"{home}/.omp/agent"
    arguments_out = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        home,
        "--setenv",
        "HOME",
        home,
        "--setenv",
        "PATH",
        "/runtime/node_modules/.bin:/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "TERM",
        "xterm-256color" if terminal else "dumb",
        "--setenv",
        "CODEX_HOME",
        f"{home}/.codex",
        "--setenv",
        "PI_CODING_AGENT_DIR",
        f"{home}/.omp/agent",
    ]
    for system_path in (
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/etc/ssl/certs",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
    ):
        if Path(system_path).exists():
            arguments_out.extend(["--ro-bind", system_path, system_path])
    arguments_out.extend(
        [
            "--ro-bind",
            str(ROOT / "node_modules"),
            "/runtime/node_modules",
            "--bind",
            str(ROOT / "credentials" / runtime),
            credential_home,
            "--ro-bind",
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
        ]
    )
    if runtime == "omp" and not terminal:
        arguments_out.extend(
            [
                "--ro-bind",
                str(Path(__file__).with_name("omp-inference.ts")),
                "/runtime/omp-inference.ts",
                "--",
                "/runtime/node_modules/.bin/bun",
                "/runtime/omp-inference.ts",
                *arguments,
            ]
        )
    elif runtime == "omp":
        arguments_out.extend(["--", "/runtime/node_modules/.bin/omp", *arguments])
    else:
        arguments_out.extend(["--", "/runtime/node_modules/.bin/codex", *arguments])
    proxy_file = ROOT / "proxy.json"
    if proxy_file.is_file():
        proxy = json.loads(proxy_file.read_text())["https_proxy"]
        parsed = urlsplit(proxy)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("invalid native proxy configuration")
        insertion = arguments_out.index("--")
        arguments_out[insertion:insertion] = [
            "--setenv",
            "HTTPS_PROXY",
            proxy,
            "--setenv",
            "HTTP_PROXY",
            proxy,
        ]
    return arguments_out


if __name__ == "__main__":
    try:
        argv = command(sys.argv[1], sys.argv[2:])
    except (ValueError, IndexError, OSError):
        sys.exit("native isolation configuration is invalid")
    os.execv(argv[0], argv)
