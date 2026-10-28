# Project constraints

- Target the complete 15-servo XL330 Microduck; do not introduce a 10-servo variant.
- Keep native robotd the exclusive bus owner. Never add serial writes beside it.
- Preserve task idempotency, bounded action lifetime, epoch fencing and post-inference revalidation.
- Unknown, stale or malformed measurements are never converted to safe values.
- Do not claim accepted commands are physical success. Keep completion evidence separate.
- AgentJev chooses among already validated actions; GLM cannot change physical limits.
- Keep demo/synthetic measurements explicitly labeled and forbidden on the live backend.
- Do not auto-enable torque, auto-resume interrupted tasks or expose arbitrary shell/code execution.
- Inspect git status before repository changes; preserve unrelated user edits and the main branch.
- Test the actual protocol and concrete failure paths. Keep hardware and model validation claims accurate.

