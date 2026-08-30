# MCP Server Repository Agent Entry

## Common authority routing

- This file is a repository entry point, not a copy of team policy.
- Cross-repository policy, roles, Wave/WBS state, assignments, Git/PR workflow, and shared safety rules are owned only by the sibling `../urizo-final-master/AGENTS.md` and its required current-status documents.
- Before planning or editing, read that Master authority from the canonical parent workspace. If the sibling Master checkout is unavailable, do not infer current work from this repository alone; reopen the canonical five-repository workspace or synchronize Master first.
- Claude Code uses `CLAUDE.md`, which imports this file. Do not add a second copy of common policy there.

## Repository-local scope

- Own only the single Python MCP runtime, shared transport/authentication boundary, approved Coding/CMS tool-name catalog, health endpoints, tests, image, and dependency lock.
- Spring owns platform authorization, Job/Profile Version authority, Tool execution policy, and Core persistence. LangGraph owns coding workflow and checkpoints. Product repositories own feature UX and domain behavior.
- Never add Core database, Valkey, checkpoint, or production tool implementations without an approved Work ID and Master scope expansion.
- Keep Python/runtime pins, commands, and repository-local verification in `README.md`, `pyproject.toml`, and `uv.lock` rather than duplicating them in agent policy.
