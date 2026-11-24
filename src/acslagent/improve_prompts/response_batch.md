FINAL OUTPUT CHECK: only one JSON object, exactly {{candidate_count}} complete distinct blocks:
{{batch_schema}}
Replace every placeholder with real clauses. Compare against ALL candidate_history
blocks. No prose, Markdown fences, explanations or extra keys. Each block must
end with a semicolon. Escape each ACSL backslash for JSON (e.g. "ensures \\result >= 0;").
Keep each block concise and useful; do not spend the output on repeated tautologies.
Put ALL clauses of one alternative inside its single specification string. Never
append an unnamed string after that field or repeat specification inside one object.
