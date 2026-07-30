---
name: summarize
description: Compress the current session into a concise durable summary
allowed_tools:
  - note_save
---
Create a concise English summary of the current conversation for future machine recovery.

Include:
1. Main session goal
2. Material completed steps, omitting exploratory attempts
3. Final conclusions or artifacts
4. Remaining issues or the next continuation point

Format:
- Markdown
- Concise, at most 350 English words
- Third person

Save the summary with note_save after writing it.

$ARGUMENTS
