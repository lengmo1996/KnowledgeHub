# KnowledgeHub V2.0.6 controlled-pilot release report

## Outcome

V2.0.6 freezes the current implementation before the first private
real-project controlled Pilot. The implementation commit is
`4006934e8dd65995897136b63d05071fd5b59bbf`; the release commit contains this
report and `state/releases/v2_0_6_manifest.json`.

Historical manifests through V2.0.5 remain immutable. V2.0.6 records the
current configuration, V3 Workspace boundary, production Writing Materials
release and 17-tool read-only MCP interface without modifying a production
index or creating a real Workspace.

## Frozen scope

- V3 fixture workspaces and the fail-closed `--allow-real-project` Gate F.
- Workspace-scoped read-only project Context, Query and Skill routing.
- Writing Materials extraction, review, quality, RBAC, release and retention
  governance.
- The active 1,107-point Writing Materials `quality_v2` release and its
  rollback-labelled alias state.
- Current Literature, Code and Writing configuration, active prompts,
  taxonomy, dual-GPU retrieval profile and MCP project-state example.

## Runtime evidence

Read-only online validation observed:

- Literature: `zotero_papers_qwen3_4b_1024_v2`, 190,131 points, green;
- Code: `knowledgehub_code_current`, 1,118 points, green;
- Writing: `knowledgehub_writing_current`, 1,107 points, green;
- RAG core, Search API, LAN MCP and Tailscale MCP active;
- both MCP listeners ready with dense, sparse and embedding dependencies ready.

The freeze did not build, promote, roll back, clean or delete a collection.

## Verification

- 603 tests passed.
- Ruff passed.
- strict MyPy passed over 133 source files.
- `git diff --check` passed.
- `knowledgehub validate all` passed online Qdrant membership checks.
- offline and live V2 evaluation each passed 11 groups and 66 samples with
  zero failed groups.
- the release validator checks 12 committed configuration and boundary files.

Evaluation reports remain private under the controlled-Pilot report root and
are represented in the manifest only by SHA-256 digests.

## Pilot boundary

V2.0.6 does not itself authorize or start the four-week Pilot. The real
project must still pass Day 0 plus Gate G through Gate J. In particular, the
project Workspace must use an independent private state root, MCP must load it
read-only, unknown and traversal Workspace IDs must fail closed, and the
target repository must retain the same Git state before and after every Pilot
session.
