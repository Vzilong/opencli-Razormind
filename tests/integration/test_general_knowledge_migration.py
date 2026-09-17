"""Database migration evidence for compatibility-library backfill and rollback guard."""

# ruff: noqa: E501 -- multiline SQL fixtures retain their database column layout.

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from alembic import command
from alembic.config import Config

from backend.config import get_settings


def test_general_knowledge_migration_preserves_legacy_bytes_revisions_and_product_scope(
    monkeypatch,
):
    with TemporaryDirectory() as directory:
        database = Path(directory) / "general-knowledge-migration.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        config = Config()
        config.set_main_option("script_location", "backend/migrations")
        try:
            command.upgrade(config, "kb20260906a")
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    INSERT INTO workspaces (id, name, slug, active, created_at, updated_at)
                    VALUES ('workspace-1', 'Workspace', 'workspace-1', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO studio_workspaces (id, name, slug, active, created_at, updated_at)
                    VALUES ('workspace-1', 'Studio', 'studio-1', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO studio_projects
                    (id, workspace_id, name, slug, created_by_user_id, archived, app_type, created_at, updated_at)
                    VALUES ('project-1', 'workspace-1', 'Project', 'project-1', 'owner', 0, 'workflow', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO brands (id, workspace_id, name, description, created_at, updated_at)
                    VALUES ('brand-1', 'workspace-1', 'Legacy', 'existing brand', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO brand_products (id, brand_id, name, description, created_at, updated_at)
                    VALUES ('product-1', 'brand-1', 'Legacy Product', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO brand_project_scopes
                    (id, project_id, brand_id, product_id, created_at, updated_at)
                    VALUES ('scope-1', 'project-1', 'brand-1', 'product-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO knowledge_pages
                    (id, brand_id, product_id, title, kind, status, content, revision, source_refs,
                    original_name, original_bytes, content_hash, upload_key, created_by, created_at, updated_at)
                    VALUES ('page-1', 'brand-1', 'product-1', 'Source', 'source', 'published', 'old content', 7, '[]',
                    'source.md', X'6F726967696E616C', 'hash-1', 'upload-1', 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    INSERT INTO knowledge_revisions
                    (id, page_id, revision, content, title, status, actor_id, created_at, updated_at)
                    VALUES ('revision-7', 'page-1', 7, 'old content', 'Source', 'published', 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    """
                )
                connection.commit()
            finally:
                connection.close()

            command.upgrade(config, "head")
            connection = sqlite3.connect(database)
            try:
                library = connection.execute(
                    "SELECT id, workspace_id, name, legacy_brand_id FROM knowledge_libraries"
                ).fetchone()
                page = connection.execute(
                    "SELECT library_id, brand_id, product_id, revision, original_bytes, content_hash "
                    "FROM knowledge_pages WHERE id = 'page-1'"
                ).fetchone()
                binding = connection.execute(
                    "SELECT project_id, library_id, product_id FROM project_knowledge_bindings"
                ).fetchone()
                revision = connection.execute(
                    "SELECT revision, content FROM knowledge_revisions WHERE id = 'revision-7'"
                ).fetchone()
                assert library[1:] == ("workspace-1", "Legacy", "brand-1")
                assert page == (library[0], "brand-1", "product-1", 7, b"original", "hash-1")
                assert binding == ("project-1", library[0], "product-1")
                assert revision == (7, "old content")

                connection.execute(
                    "INSERT INTO knowledge_libraries "
                    "(id, workspace_id, name, description, created_at, updated_at) "
                    "VALUES ('generic-1', 'workspace-1', 'Generic', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
                connection.commit()
            finally:
                connection.close()
            with pytest.raises(RuntimeError, match="Cannot downgrade general knowledge libraries"):
                command.downgrade(config, "kb20260906a")
        finally:
            get_settings.cache_clear()
