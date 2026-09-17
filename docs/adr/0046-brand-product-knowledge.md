# Brand/product classification and a source-backed knowledge library

Date: 2026-09-06. Status: implemented baseline, not deployed by this change.

## Problem and reference assessment

A brand such as 高吉星 has common knowledge and multiple products. Operators need to upload and query material and distinguish collected website data by product. Existing Studio Projects, workflows and records describe execution ownership but have no business brand/product taxonomy.

The narrow reference question was which knowledge patterns to reuse without introducing a competing project, identity or model platform. No existing approved brand-knowledge design was found in the repository's ADR/research entry points. Source inspection was limited to current first-party documentation and the local implementation:

| Verified reference | Reusable conclusion | Implementation decision |
| --- | --- | --- |
| [obsidian-llm-wiki research compiler loop](https://github.com/2233admin/obsidian-llm-wiki/blob/main/docs/RESEARCH_COMPILER_LOOP.md) | Keep sources, generated drafts and reviewed knowledge distinct; retain citations. | Immutable upload content, separate editable pages, generated drafts, explicit human publication and retained revisions. |
| [obsidian-llm-wiki README](https://github.com/2233admin/obsidian-llm-wiki) | A small knowledge corpus can start without embedding infrastructure. | Bounded lexical retrieval using the existing database; no vector database or separate model configuration. |
| [obsidian-llm-wiki ingest contract](https://github.com/2233admin/obsidian-llm-wiki/blob/main/docs/INGEST.md) | Capture/registration and accepted knowledge are different stages. | Upload returns success only after extracted text and original bytes are persisted; AI output stays a draft. No claim that registering an external URL imports it. |
| [Feishu knowledge-page operations](https://www.feishu.cn/hc/zh-CN/articles/763992129311-%E6%89%B9%E9%87%8F%E6%93%8D%E4%BD%9C%E7%9F%A5%E8%AF%86%E5%BA%93%E9%A1%B5%E9%9D%A2) and [knowledge updates](https://www.feishu.cn/hc/zh-CN/articles/891296506340-%E5%85%B3%E6%B3%A8%E7%9F%A5%E8%AF%86%E5%BA%93%E6%9B%B4%E6%96%B0%E5%8A%A8%E6%80%81) | A navigable hierarchy and permission-aware reading make a library usable. | Workspace-governed brand space, nested directories, document reader, search/answer column. Existing workspace permissions control access. |
| [Feishu knowledge Q&A](https://www.feishu.cn/content/article/7594399781179395261) | Questions can be answered from accessible business material. | The server authorizes workspace membership and selects brand/product content before calling the existing chat model resolver. |

These sources are design references, not new runtime dependencies; no reference-project source code was copied. The Feishu reference is not an integration with a user's private knowledge space. Its fine-grained page permissions, online editing and synchronization are not claimed by this baseline.

## Domain and ownership

`Workspace → Brand → Product` supplies business classification. The existing `StudioProject → StudioWorkflow → CollectedRecord` chain remains authoritative for execution and data lineage. A separate `BrandProjectScope` links each project to a brand and optionally one product. A brand-level project has no product. An unclassified project has no classification row.

Classification is a current project property: assigning/reassigning changes how both existing and future records are filtered. It does not rewrite records, duplicate them, guess historical product ownership, or provide per-record multi-product tagging. Removal of the classification is supported. The UI explains this behavior. Existing records without project/workflow lineage remain visible in the existing unfiltered records view.

Product classification is a business filter, not a new authorization boundary. Knowledge routes enforce governed-workspace membership; admin/maintainer configuration permission is required for writes. Product-specific permission policy is not introduced. Existing unfiltered record API authorization is not expanded or represented as a new tenant-isolation guarantee.

## Knowledge behavior

- `KnowledgePage` represents a folder, immutable uploaded source, or editable knowledge page. Original bytes stay in the existing database so backup/storage ownership does not split across another platform. Original filenames are normalized, never used as local paths.
- UTF-8 TXT, Markdown, CSV and DOCX are accepted, with a 2 MB upload and 100,000-character extracted-text limit. DOCX extraction is bounded and rejects entity declarations. Unsupported formats fail visibly. PDF/OCR is not installed by this change. A database-unique scope/content key prevents duplicate uploads; reuse is explicit, while a different requested folder or an archived duplicate returns an actionable conflict.
- Product retrieval includes only that product plus brand-common pages. Brand-wide retrieval includes all of that brand's products. Creation/upload with no product creates brand-common material; the UI states this distinction. Folder and child must have the same brand/product scope.
- Uploads are published source material, not human-verified claims. Editable/AI-generated pages start as drafts. Only published non-folder pages are retrieved. Archived pages can be restored; original source bodies cannot be rewritten. Version checks reject concurrent stale edits and revision snapshots retain older text.
- Retrieval uses escaped case-insensitive lexical matching, Chinese bigrams, up to 24 unique query terms, the newest 200 matching pages, overlapping 1,000-character passages, and at most eight results. It is a bounded baseline, not semantic/vector search or exhaustive corpus recall.
- AI uses the existing `chat` role resolver, credentials and failover policy. No candidates/provider failure produces a visible unavailable response while keyword search remains usable. Empty retrieval never calls the model. Prompts treat source text as data, and returned citation numbers must refer to retrieved excerpts. This validates references, not the factual correctness of every generated claim; the UI asks users to verify against original sources.
- A single published document up to 16,000 characters can be summarized into a draft with source revision/excerpt references. Larger documents fail explicitly rather than silently truncate. Publication requires an explicit operator action. This is not a full multi-document compiler or contradiction detector.

## Reused capabilities and remaining boundaries

Reused: SQLAlchemy/Alembic, database backup ownership, FastAPI response/auth/RBAC, Studio project/record lineage, the LLM resolver and provider adapters, authenticated Axios client, React Query, page shell/navigation and UI controls.

The repository's `primitive.knowledge.retrieve`, `primitive.knowledge.index`, and document extraction nodes were inspected in `backend/plugins/capability_catalog.py`; their missing runtime bindings remain explicit. This change does not falsely mark them runnable. Connecting the library to workflow nodes, the global Agent tool registry, direct record-to-knowledge promotion, analytical reports, full-site product switching, and Feishu synchronization requires their own integration contracts. Existing Feishu table source/writeback functionality is not a Wiki connector.

Other future capabilities are rich Markdown rendering, links/backlinks and a knowledge graph, document move/rename across scopes, source freshness/contradiction checks, large-corpus semantic retrieval and PDF/OCR. The shipped source/editor view intentionally renders text without executing HTML or embedded instructions.

## Evidence and maintenance

Reference conclusions above pass the value gate because they explain durable ownership and trust decisions, and the confidence gate for the specifically cited behavior only. Proposed extensions are not described as current capabilities. This ADR records the implementation decision; it does not create a separate knowledge platform or active plan.

Evidence: 27 backend tests passed across `tests/integration/test_brand_knowledge.py` and `tests/integration/test_records_api.py`, covering scoped retrieval, workspace/role denial, parent validation, original retention, versions, review state, citation validity, model unavailability, parser bounds, upload uniqueness, project classification and records filtering. Four browser tests in `frontend/e2e/brand-knowledge.spec.mjs` passed against explicit API fixtures, including a cancelled navigation preserving unsaved edits; this does not prove real provider output quality. Production build, targeted lint, fresh SQLite migration chain and downgrade/re-upgrade passed. Windows standalone preview encountered a pre-existing dependency-link issue; browser checks ran the built application with installed local dependencies. See the accompanying usage note for operational limits.

Routing: primary agent implemented the connected backend/frontend change; one Sol High agent independently reviewed only this feature. Review led to editor loss-prevention and database-enforced upload uniqueness fixes. Existing unscoped record authorization was identified and retained as a pre-existing boundary, not reclassified as new product-level security. No other agents or full-repository refactor were used.

Invalidate/revisit these conclusions when the referenced upstream contract changes, brand/product scope changes from one product per project, permission granularity changes, corpus bounds become limiting, or workflow/Agent integration is implemented. Minimum recheck: inspect the two reference loop/ingest documents and local capability bindings, then run the scoped tests and a migration check. Do not repeat unrelated external research.
