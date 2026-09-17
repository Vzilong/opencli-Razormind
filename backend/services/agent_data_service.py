"""Read-only, project-scoped data shared by REST and downstream MCP tools."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from backend.models.agent_run import AgentRun, AgentSession
from backend.models.brand_knowledge import (
    BrandProduct,
    KnowledgeLibrary,
    KnowledgePage,
    ProjectKnowledgeBinding,
)
from backend.models.record import CollectedRecord
from backend.models.studio import StudioProject, StudioWorkflow
from backend.schemas.agent_data import (
    AgentDataCapabilities,
    ProjectContextMatch,
    ProjectContextResult,
    ProjectRecordList,
    ProjectRecordRead,
)
from backend.security.identity import RequestIdentity
from backend.security.workspace_rbac import WorkspacePermission, require_permission
from backend.services.brand_knowledge_service import query_terms
from backend.services.studio_agent_session_access import resolve_agent_session_workspace

_READABLE_RECORD_STATUSES = frozenset({"normalized", "ai_processed", "notified"})
_SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credentials?|password|private_?key|secret|token)(_|$)",
    re.IGNORECASE,
)
_MAX_MATCH_TEXT = 1_000
_MAX_FLATTENED_VALUES = 200
_MAX_RECORDS_SCANNED = 500
_MAX_RESEARCH_RUNS_SCANNED = 50
_MAX_RESEARCH_MATCH_CANDIDATES = 100
_MAX_RESEARCH_SOURCES = 12
_MAX_RESEARCH_FINDINGS = 100
_MAX_RESEARCH_CHANGES = 100
_MAX_RESEARCH_GAPS = 20
_MAX_RESEARCH_EVIDENCE_PER_FINDING = 12
_MAX_RESEARCH_EVIDENCE_QUOTE = 1_000
_RESEARCH_SOURCE_FIELDS = frozenset(
    {
        "id",
        "url",
        "title",
        "fetched_at",
        "content_hash",
        "excerpt",
        "status",
        "watch_status",
        "baseline_run_id",
        "change_excerpt",
    }
)


@dataclass(frozen=True)
class AgentDataProjectScope:
    """The caller-authorized governed scope and exact Studio project resource."""

    requested_workspace_id: str
    governed_workspace_id: str
    project: StudioProject


def _safe_value(value: Any) -> Any:
    """Remove credential-shaped fields from externally returned JSON."""

    if isinstance(value, dict):
        return {
            str(key): _safe_value(item) for key, item in value.items() if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return value


def _is_sensitive_key(key: Any) -> bool:
    """Recognize common snake, kebab, and camel-case credential field names."""

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return _SENSITIVE_KEY.search(normalized) is not None


async def authorize_project(
    db: AsyncSession,
    *,
    workspace_id: str,
    project_id: str,
    identity: RequestIdentity,
) -> AgentDataProjectScope:
    """Re-authorize membership/local bridge, then bind the exact project."""

    workspace_scope = await resolve_agent_session_workspace(
        db,
        identity,
        workspace_id,
        context={"project_id": project_id},
    )
    require_permission(workspace_scope.access, WorkspacePermission.READ)
    project = await db.get(StudioProject, project_id)
    if project is None or project.workspace_id != workspace_id or project.archived:
        # Do not reveal whether a project exists in another workspace.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return AgentDataProjectScope(
        requested_workspace_id=workspace_id,
        governed_workspace_id=workspace_scope.workspace_id,
        project=project,
    )


def _project_workflow_ids(project_id: str):
    return select(StudioWorkflow.id).where(
        StudioWorkflow.project_id == project_id,
        StudioWorkflow.archived.is_(False),
    )


def _record_filters(project_id: str) -> list[Any]:
    return [
        CollectedRecord.workflow_id.in_(_project_workflow_ids(project_id)),
        CollectedRecord.status.in_(_READABLE_RECORD_STATUSES),
    ]


def _bounded_record_value(value: Any) -> tuple[Any, bool]:
    """Bound record JSON while removing credentials before copying values."""
    nodes_left, chars_left = 200, 16_000
    truncated = False

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes_left, chars_left, truncated
        if depth > 8 or nodes_left <= 0 or chars_left <= 0:
            truncated = True
            return None
        nodes_left -= 1
        if isinstance(item, str):
            size = min(len(item), chars_left, 4_000)
            truncated |= size < len(item)
            chars_left -= size
            return item[:size]
        if isinstance(item, dict):
            output = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 200 or nodes_left <= 0 or chars_left <= 0:
                    truncated = True
                    break
                if _is_sensitive_key(key):
                    continue
                if len(str(key)) > min(256, chars_left):
                    truncated = True
                    continue
                chars_left -= len(str(key))
                output[str(key)] = visit(child, depth + 1)
            return output
        if isinstance(item, list):
            output_list = []
            for child in item:
                if nodes_left <= 0 or chars_left <= 0:
                    truncated = True
                    break
                output_list.append(visit(child, depth + 1))
            return output_list
        return item

    result = visit(value, 0)
    return result, truncated


def _record_read(record: CollectedRecord) -> ProjectRecordRead:
    source: dict[str, Any] = {
        "type": "record",
        "record_id": record.id,
        "source_id": record.source_id,
        "task_id": record.task_id,
        "workflow_id": record.workflow_id,
        "run_id": record.workflow_run_id,
        "content_hash": record.content_hash,
    }
    data, data_truncated = _bounded_record_value(record.normalized_data)
    lineage, lineage_truncated = _bounded_record_value(record.lineage)
    if lineage:
        source["lineage"] = lineage
    return ProjectRecordRead(
        id=record.id,
        version=record.content_hash,
        status=record.status,
        data=data,
        source=source,
        updated_at=record.updated_at,
        projection_truncated=data_truncated or lineage_truncated,
    )


async def list_project_records(
    db: AsyncSession,
    *,
    workspace_id: str,
    project_id: str,
    identity: RequestIdentity,
    query: str | None = None,
    limit: int = 20,
) -> ProjectRecordList:
    """Return existing normalized records; never collect or invoke a model."""

    await authorize_project(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
    )
    normalized_query = query.strip() if query and query.strip() else None
    filters = _record_filters(project_id)
    statement = (
        select(CollectedRecord)
        .options(
            load_only(
                CollectedRecord.id,
                CollectedRecord.content_hash,
                CollectedRecord.status,
                CollectedRecord.normalized_data,
                CollectedRecord.source_id,
                CollectedRecord.task_id,
                CollectedRecord.workflow_id,
                CollectedRecord.workflow_run_id,
                CollectedRecord.lineage,
                CollectedRecord.updated_at,
            )
        )
        .where(*filters)
        .order_by(CollectedRecord.updated_at.desc(), CollectedRecord.id.desc())
    )
    if normalized_query:
        # Filter only the same redacted projection the caller can read. A SQL
        # predicate on raw JSON leaks hidden values through matching row counts.
        terms = query_terms(normalized_query)
        # The candidate window is independent of the query and hidden values,
        # so even the truncation flag cannot become a credential oracle.
        rows = (await db.scalars(statement.limit(_MAX_RECORDS_SCANNED + 1))).all()
        truncated = len(rows) > _MAX_RECORDS_SCANNED
        matches: list[ProjectRecordRead] = []
        total = 0
        projection_truncated = False
        for record in rows[:_MAX_RECORDS_SCANNED]:
            readable = _record_read(record)
            projection_truncated |= readable.projection_truncated
            if any(_text_score(part, terms) for part in _flatten_safe_text(readable.data)):
                total += 1
                if len(matches) < limit:
                    matches.append(readable)
        return ProjectRecordList(
            items=matches,
            total=total,
            truncated=truncated,
            projection_truncated=projection_truncated,
            total_is_exact=not (truncated or projection_truncated),
        )
    total = await db.scalar(select(func.count()).select_from(CollectedRecord).where(*filters))
    rows = (await db.scalars(statement.limit(limit))).all()
    items = [_record_read(record) for record in rows]
    return ProjectRecordList(
        items=items,
        total=int(total or 0),
        projection_truncated=any(item.projection_truncated for item in items),
    )


def _flatten_safe_text(value: Any) -> list[str]:
    """Flatten safe normalized JSON into bounded, path-labelled text values."""

    output: list[str] = []

    def visit(item: Any, path: str, depth: int) -> None:
        if depth > 8 or len(output) >= _MAX_FLATTENED_VALUES:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                if _is_sensitive_key(key):
                    continue
                child_path = f"{path}.{key}" if path else str(key)
                visit(nested, child_path, depth + 1)
            return
        if isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]", depth + 1)
            return
        if item is None:
            return
        text = str(item).strip()
        if text:
            output.append(f"{path}: {text}" if path else text)

    visit(value, "", 0)
    return output


def _text_score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(term) for term in terms)


def _matched_excerpt(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    offsets = [offset for term in terms if (offset := lowered.find(term)) >= 0]
    start = max(0, min(offsets) - 180) if offsets else 0
    return text[start : start + _MAX_MATCH_TEXT].strip()


def _record_match(record: ProjectRecordRead, query: str) -> ProjectContextMatch | None:
    terms = query_terms(query)
    parts = _flatten_safe_text(record.data)
    matched = [part for part in parts if _text_score(part, terms)]
    if not matched:
        return None
    return ProjectContextMatch(
        id=f"record:{record.id}:{record.version}",
        text="\n".join(matched)[:_MAX_MATCH_TEXT],
        source=record.source | {"version": record.version, "updated_at": record.updated_at},
    )


def _research_version(run: AgentRun) -> str:
    encoded = json.dumps(
        run.reply_payload or {}, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _research_source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    return _safe_value({key: source[key] for key in _RESEARCH_SOURCE_FIELDS if key in source})


def _research_gaps(result: dict[str, Any]) -> list[str]:
    raw_gaps = result.get("gaps")
    if not isinstance(raw_gaps, list):
        return []
    return [item[:500] for item in raw_gaps[:_MAX_RESEARCH_GAPS] if isinstance(item, str) and item]


def _research_base_source(
    run: AgentRun,
    *,
    result: dict[str, Any],
    evidence_kind: str,
    version: str,
) -> dict[str, Any]:
    return {
        "type": "research",
        "run_id": run.id,
        "version": version,
        "status": run.status,
        "template_id": str((run.request_payload or {}).get("template_id", "")),
        "updated_at": run.updated_at,
        "evidence_kind": evidence_kind,
        "gaps": _research_gaps(result),
    }


def _research_run_matches(
    run: AgentRun,
    *,
    query: str,
) -> tuple[list[tuple[int, ProjectContextMatch]], list[str]]:
    payload = run.reply_payload if isinstance(run.reply_payload, dict) else {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return [], []
    version = _research_version(run)
    terms = query_terms(query)
    raw_sources = result.get("sources")
    sources = (
        [source for source in raw_sources[:_MAX_RESEARCH_SOURCES] if isinstance(source, dict)]
        if isinstance(raw_sources, list)
        else []
    )
    sources_by_id = {
        source_id: source
        for source in sources
        if isinstance((source_id := source.get("id")), str) and source_id
    }
    run_gaps = _research_gaps(result)
    matches: list[tuple[int, ProjectContextMatch]] = []

    raw_findings = result.get("findings")
    findings = raw_findings[:_MAX_RESEARCH_FINDINGS] if isinstance(raw_findings, list) else []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or not isinstance(finding.get("text"), str):
            continue
        finding_text = finding["text"].strip()[:5_000]
        source_ids = finding.get("source_ids")
        evidence = finding.get("evidence")
        if (
            not finding_text
            or not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) > _MAX_RESEARCH_SOURCES
            or not all(
                isinstance(source_id, str) and source_id in sources_by_id
                for source_id in source_ids
            )
            or not isinstance(evidence, list)
            or not evidence
            or len(evidence) > _MAX_RESEARCH_EVIDENCE_PER_FINDING
        ):
            continue
        validated_evidence: list[dict[str, str]] = []
        for item in evidence[:_MAX_RESEARCH_EVIDENCE_PER_FINDING]:
            if not isinstance(item, dict):
                validated_evidence = []
                break
            source_id = item.get("source_id")
            quote = item.get("quote")
            if not isinstance(source_id, str) or not isinstance(quote, str):
                validated_evidence = []
                break
            source = sources_by_id.get(source_id)
            content = source.get("content") if isinstance(source, dict) else None
            if (
                source_id not in source_ids
                or not quote.strip()
                or len(quote) > _MAX_RESEARCH_EVIDENCE_QUOTE
                or not isinstance(content, str)
                or quote not in content
            ):
                validated_evidence = []
                break
            validated_evidence.append({"source_id": source_id, "quote": quote})
        if not validated_evidence:
            continue
        searchable = "\n".join([finding_text, *(item["quote"] for item in validated_evidence)])
        score = _text_score(searchable, terms)
        if not score:
            continue
        cited_sources = [
            _research_source_metadata(sources_by_id[source_id]) for source_id in source_ids
        ]
        source = _research_base_source(
            run, result=result, evidence_kind="model-analyzed-finding", version=version
        )
        source.update(
            {
                "source_ids": source_ids,
                "evidence": validated_evidence,
                "sources": cited_sources,
            }
        )
        matches.append(
            (
                score + 2,
                ProjectContextMatch(
                    id=f"research:{run.id}:{source['version']}:finding:{index}",
                    text=finding_text[:_MAX_MATCH_TEXT],
                    source=source,
                ),
            )
        )

    for source_id, raw_source in sources_by_id.items():
        content = raw_source.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        metadata = _research_source_metadata(raw_source)
        searchable = "\n".join([content, *_flatten_safe_text(metadata)])
        score = _text_score(searchable, terms)
        if not score:
            continue
        source = _research_base_source(
            run, result=result, evidence_kind="captured-source", version=version
        )
        source.update({"source_id": source_id, "source": metadata})
        matches.append(
            (
                score,
                ProjectContextMatch(
                    id=f"research:{run.id}:{source['version']}:source:{source_id}",
                    text=(
                        "Captured evidence (not a model-analyzed finding):\n"
                        f"{_matched_excerpt(content, terms)}"
                    )[:_MAX_MATCH_TEXT],
                    source=source,
                ),
            )
        )

    raw_changes = result.get("changes")
    changes = raw_changes[:_MAX_RESEARCH_CHANGES] if isinstance(raw_changes, list) else []
    for index, raw_change in enumerate(changes):
        if not isinstance(raw_change, dict):
            continue
        change = _safe_value(raw_change)
        searchable = "\n".join(_flatten_safe_text(change))
        score = _text_score(searchable, terms)
        if not score:
            continue
        source = _research_base_source(
            run, result=result, evidence_kind="captured-change", version=version
        )
        source["change"] = change
        matches.append(
            (
                score,
                ProjectContextMatch(
                    id=f"research:{run.id}:{source['version']}:change:{index}",
                    text=(
                        "Captured change evidence (not a model-analyzed finding):\n"
                        f"{_matched_excerpt(searchable, terms)}"
                    )[:_MAX_MATCH_TEXT],
                    source=source,
                ),
            )
        )

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[:_MAX_RESEARCH_MATCH_CANDIDATES], run_gaps if matches else []


async def _research_matches(
    db: AsyncSession,
    *,
    scope: AgentDataProjectScope,
    query: str,
    limit: int,
) -> tuple[list[ProjectContextMatch], list[str]]:
    runs_with_sentinel = (
        await db.scalars(
            select(AgentRun)
            .join(AgentSession)
            .where(
                AgentRun.kind == "research",
                AgentRun.status.in_(("completed", "partial")),
                AgentSession.workspace_id == scope.governed_workspace_id,
                AgentSession.context["studio_workspace_id"].as_string()
                == scope.requested_workspace_id,
                AgentSession.context["project_id"].as_string() == scope.project.id,
            )
            .order_by(AgentRun.updated_at.desc(), AgentRun.id.desc())
            .limit(_MAX_RESEARCH_RUNS_SCANNED + 1)
        )
    ).all()
    scan_truncated = len(runs_with_sentinel) > _MAX_RESEARCH_RUNS_SCANNED
    runs = runs_with_sentinel[:_MAX_RESEARCH_RUNS_SCANNED]
    ranked: list[tuple[int, datetime, ProjectContextMatch]] = []
    for run in runs:
        run_matches, _ = _research_run_matches(run, query=query)
        ranked.extend((score, run.updated_at, match) for score, match in run_matches)
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    matches = [item[2] for item in ranked[:limit]]
    gaps: list[str] = []
    for match in matches:
        for gap in match.source.get("gaps", []):
            labelled = f"Research run {match.source['run_id']}: {gap}"
            if labelled not in gaps:
                gaps.append(labelled)
    if scan_truncated:
        gaps.append(
            f"Research context scan was limited to the {_MAX_RESEARCH_RUNS_SCANNED} "
            "most recently updated runs."
        )
    return matches, gaps


async def _knowledge_matches(
    db: AsyncSession,
    *,
    workspace_id: str,
    project_id: str,
    query: str,
    limit: int,
) -> tuple[list[ProjectContextMatch], bool]:
    # Only explicit library bindings authorize downstream retrieval. Never
    # fall back to legacy classification after a binding has been removed.
    authorized_bindings = (
        select(ProjectKnowledgeBinding.library_id)
        .join(KnowledgeLibrary, KnowledgeLibrary.id == ProjectKnowledgeBinding.library_id)
        .outerjoin(BrandProduct, BrandProduct.id == ProjectKnowledgeBinding.product_id)
        .where(
            ProjectKnowledgeBinding.project_id == project_id,
            KnowledgeLibrary.workspace_id == workspace_id,
            or_(
                ProjectKnowledgeBinding.product_id.is_(None),
                BrandProduct.brand_id == KnowledgeLibrary.legacy_brand_id,
            ),
        )
    )
    if await db.scalar(authorized_bindings.limit(1)) is None:
        return [], False
    page_is_bound = (
        authorized_bindings.where(
            KnowledgePage.library_id == ProjectKnowledgeBinding.library_id,
            or_(
                ProjectKnowledgeBinding.product_id.is_(None),
                KnowledgePage.product_id.is_(None),
                KnowledgePage.product_id == ProjectKnowledgeBinding.product_id,
            ),
        )
        .correlate(KnowledgePage)
        .exists()
    )

    terms = query_terms(query)
    stmt = (
        select(KnowledgePage)
        .options(defer(KnowledgePage.original_bytes))
        .where(
            page_is_bound,
            KnowledgePage.status == "published",
            KnowledgePage.kind != "folder",
            or_(
                *(
                    KnowledgePage.content.icontains(term, autoescape=True)
                    | KnowledgePage.title.icontains(term, autoescape=True)
                    for term in terms
                )
            ),
        )
    )
    pages = (await db.scalars(stmt.order_by(KnowledgePage.updated_at.desc()).limit(200))).all()

    ranked: list[tuple[int, datetime, ProjectContextMatch]] = []
    for page in pages:
        best: tuple[int, int, str] | None = None
        for offset in range(0, len(page.content), 800):
            excerpt = page.content[offset : offset + _MAX_MATCH_TEXT]
            score = sum(term in excerpt.lower() for term in terms)
            score += 2 * sum(term in page.title.lower() for term in terms)
            if score and (best is None or score > best[0]):
                best = (score, offset, excerpt)
        if best is None:
            continue
        source = {
            "type": "knowledge",
            "page_id": page.id,
            "library_id": page.library_id,
            "brand_id": page.brand_id,
            "product_id": page.product_id,
            "title": page.title,
            "kind": page.kind,
            "revision": page.revision,
            "updated_at": page.updated_at,
            "offset": best[1],
            "content_hash": page.content_hash,
            "source_refs": _safe_value(page.source_refs or []),
        }
        ranked.append(
            (
                best[0],
                page.updated_at,
                ProjectContextMatch(
                    id=f"knowledge:{page.id}:r{page.revision}:{best[1]}",
                    text=best[2],
                    source=source,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[:limit]], True


async def search_project_context(
    db: AsyncSession,
    *,
    workspace_id: str,
    project_id: str,
    identity: RequestIdentity,
    query: str,
    limit: int = 8,
) -> ProjectContextResult:
    """Retrieve existing record/knowledge snippets without synthesis or collection."""

    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "q must not be blank")
    scope = await authorize_project(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
    )
    research, research_gaps = await _research_matches(
        db,
        scope=scope,
        query=normalized_query,
        limit=limit,
    )
    knowledge, has_binding = await _knowledge_matches(
        db,
        workspace_id=scope.governed_workspace_id,
        project_id=project_id,
        query=normalized_query,
        limit=limit,
    )
    record_result = await list_project_records(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
        query=normalized_query,
        limit=limit,
    )
    record_matches = [
        match
        for record in record_result.items
        if (match := _record_match(record, normalized_query)) is not None
    ]
    matches = (research + knowledge + record_matches)[:limit]
    gaps: list[str] = list(research_gaps)
    if record_result.truncated:
        gaps.append(
            f"Record context scan was limited to the {_MAX_RECORDS_SCANNED} "
            "most recently updated readable records; older matches may be omitted."
        )
    if record_result.projection_truncated:
        gaps.append("Some record projections were size-limited; omitted content was not searched.")
    if not has_binding:
        gaps.append("Project has no authorized knowledge binding.")
    if not matches:
        gaps.append("No matching project records, knowledge, or research evidence were found.")
    return ProjectContextResult(query=normalized_query, matches=matches, gaps=gaps)


def get_capabilities() -> AgentDataCapabilities:
    """Report contract availability without probing or starting research work."""

    return AgentDataCapabilities(
        records=True,
        context=True,
        research=importlib.util.find_spec("backend.api.v1.research") is not None,
    )


__all__ = [
    "AgentDataProjectScope",
    "authorize_project",
    "get_capabilities",
    "list_project_records",
    "search_project_context",
]
