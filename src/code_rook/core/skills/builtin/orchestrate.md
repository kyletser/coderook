---
name: orchestrate
description: Run a planner-to-executor-to-reviewer workflow for a complex task
allowed_tools:
  - spawn_agent
  - agent_result
  - task_create
  - task_update
  - task_list
---
Coordinate the following goal through three stages:

$ARGUMENTS

Run these stages in order:

**Stage 1: Plan**
Call spawn_agent with:
- description: "plan task"
- subagent_type: "planner"
- prompt: the complete goal, constraints, and a request for ordered steps with success criteria

**Stage 2: Execute**
Call spawn_agent with:
- description: "execute plan"
- subagent_type: "executor"
- prompt: the original goal plus the complete planner result, requesting stepwise execution and verified results

**Stage 3: Review**
Call spawn_agent with:
- description: "review result"
- subagent_type: "reviewer"
- prompt: the original goal plus the executor result, requesting independent verification and missing work

After all stages, report to the user in the language required by the Language Policy:
1. Plan summary
2. Execution summary and artifacts
3. Review verdict
4. Overall success and remaining issues
