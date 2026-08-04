---
name: orchestrate
description: Run a planner-to-executor-to-reviewer workflow for a complex task
allowed_tools:
  - agent
  - tasks
---
Coordinate the following goal through three stages:

$ARGUMENTS

Run these stages in order:

**Stage 1: Plan**
Call agent with action `start` and:
- description: "plan task"
- subagent_type: "planner"
- prompt: the complete goal, constraints, and a request for ordered steps with success criteria

Call agent with action `wait` for the returned worker id and retain the complete result.

**Stage 2: Execute**
Call agent with action `start` and:
- description: "execute plan"
- subagent_type: "executor"
- prompt: the original goal plus the complete planner result, requesting stepwise execution and verified results

Call agent with action `wait` for the returned worker id and retain the complete result.

**Stage 3: Review**
Call agent with action `start` and:
- description: "review result"
- subagent_type: "reviewer"
- prompt: the original goal plus the executor result, requesting independent verification and missing work

Call agent with action `wait` for the returned worker id and retain the complete result.

After all stages, report to the user in the language required by the Language Policy:
1. Plan summary
2. Execution summary and artifacts
3. Review verdict
4. Overall success and remaining issues
