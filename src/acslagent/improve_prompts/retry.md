RETRY GENERATION: this is a regeneration attempt for the SAME task, not an
initial request or a request to extend its old annotation. One of this task's
retries has already been consumed. Repairs are NOT reset. Read retry_reason,
budget, current_score, best_score, diagnostics and candidate_history first.

Return exactly {{candidate_count}} NEW complete alternatives. None may repeat any historical
candidate or another candidate in this batch after formatting/label/order
normalization. Do not return the previous winner unchanged. Use failure evidence
to change the proof strategy: fix induction failures, missing relationships,
incorrect frames, unsupported syntax or unjustified input restrictions. A failed
proof alone does not establish a concrete counterexample. Never repair a goal
belonging to another task by modifying that task here. Original assertions and
all other task blocks remain fixed. If a previous response was malformed, follow
the response schema exactly this time. Do not return C source or commentary.

Required exact response shape:
{{batch_schema}}
