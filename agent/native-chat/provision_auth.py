"""显式授权后的本机凭据副本；不复制设置、历史、缓存或插件。"""

import argparse
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def provision(
    source: Path,
    destination: Path,
    runtime: str,
    *,
    provider: str | None = None,
    account_id: int | None = None,
) -> None:
    if destination.exists():
        raise ValueError("destination already exists; refusing to overwrite credentials")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if runtime == "codex":
            shutil.copyfile(source, temporary)
        else:
            if not provider:
                raise ValueError("OMP requires an explicitly authorized provider")
            original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            isolated = sqlite3.connect(temporary)
            try:
                original.execute("BEGIN")
                query = (
                    "SELECT * FROM auth_credentials WHERE provider=? "
                    "AND credential_type='oauth' AND disabled_cause IS NULL"
                )
                parameters: list[object] = [provider]
                if account_id is not None:
                    query += " AND id=?"
                    parameters.append(account_id)
                credentials = original.execute(query, parameters).fetchall()
                if len(credentials) != 1:
                    raise ValueError("select exactly one authorized active OAuth account")
                for _name, statement in original.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ):
                    isolated.execute(statement)
                for table in ("auth_credentials", "auth_schema_version", "auth_change_revision"):
                    rows = (
                        credentials
                        if table == "auth_credentials"
                        else original.execute(f'SELECT * FROM "{table}"').fetchall()
                    )
                    if rows:
                        placeholders = ",".join("?" for _value in rows[0])
                        isolated.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
                isolated.commit()
            finally:
                isolated.close()
                original.close()
        temporary.chmod(0o600)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", choices=["codex", "omp"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--account-id", type=int)
    arguments = parser.parse_args()
    provision(
        arguments.source,
        arguments.destination,
        arguments.runtime,
        provider=arguments.provider,
        account_id=arguments.account_id,
    )
    print("Provisioned isolated authentication without printing credentials.")
