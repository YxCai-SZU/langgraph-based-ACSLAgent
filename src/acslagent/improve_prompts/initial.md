INITIAL GENERATION: propose exactly {{candidate_count}} independent, complete replacement
annotation blocks for task.task_id. This initial batch consumes no retries budget.
Candidates are evaluated independently; they will never be merged. The code
contains TARGET_TASK at the insertion point. Other visible annotations are
fixed context. Missing later annotations are not yours to generate.

Aim for {{candidate_count}} useful alternatives: e.g. relational conservation, conditional
bounds, precise processed-region properties, disjunctive phase information,
or a simpler but still informative inductive formulation. Choose only strategies
supported by this code; do not blindly force these categories. Each block must
stand on its own and preserve needed frame information. Consider all inputs
allowed by the existing context, not just a convenient positive example.

Required exact response shape:
{{batch_schema}}
Do not include reasoning, Markdown, scores or additional keys.
