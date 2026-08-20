---
name: focused-fix
description: Implement one bounded repository fix and prove it with focused verification
allowed_tools:
  - Repository
  - File
  - Git
  - Run
---
Complete this bounded coding task: $ARGUMENTS

Follow this contract:

1. Read the repository instructions and inspect the relevant map, symbols, references, and current diff.
2. State a short plan before editing. Preserve unrelated user changes and keep the patch as small as possible.
3. Use `File` for exact edits. Do not touch credentials, environment files, generated artifacts, or paths outside the workspace.
4. Run the narrowest relevant test or verifier with `Run`; do not claim success from code inspection alone.
5. Review the final Git diff and report changed files, verification performed, and any remaining risk.

If the request needs network access, destructive Git operations, an unknown product decision, or a broader refactor,
stop and ask for direction instead of expanding scope.
