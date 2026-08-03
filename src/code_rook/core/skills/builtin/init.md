---
name: init
description: Analyze the current project and generate its initial .coderook/context.md
allowed_tools:
  - read_file
  - list_dir
  - glob
  - grep
  - write_file
  - bash
---
Analyze the current project and generate `.coderook/context.md` so future agents can quickly recover the project context.

Steps:
1. Use File.list and File.search_name to inspect the root, major directories, and configuration files.
2. Use File.read for relevant files such as README, package.json, pyproject.toml, and Cargo.toml when present.
3. Identify languages, frameworks, major modules, and directory structure.

Write concise English machine-facing context containing:
- Project name and one-line purpose
- Technology stack
- Roles of key directories
- Common build, test, and run commands
- Important conventions or prohibitions

Use File.write to write `.coderook/context.md`, creating `.coderook/` if needed.

$ARGUMENTS
