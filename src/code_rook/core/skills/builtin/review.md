---
name: review
description: Review code at a target path and classify findings by severity
allowed_tools:
  - read_file
  - list_dir
  - glob
  - grep
  - bash
---
Perform a strict code review of the following target:

$ARGUMENTS

Review:
- Correctness: logic, edge cases, and error handling
- Security: injection, authorization, and sensitive-data exposure
- Maintainability: naming, comments, duplication, and module boundaries
- Performance: unnecessary I/O or computation and resource leaks

Write the user-visible review in the language required by the Language Policy. Use sections equivalent to:

## Critical
Issues that can cause bugs or security failures. State none when empty.

## Recommended
Maintainability or readability issues. State none when empty.

## Optional
Style or minor optimization suggestions. State none when empty.
