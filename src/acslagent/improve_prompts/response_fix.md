FINAL OUTPUT CHECK: only one JSON object with exactly these three keys.
If a concrete code-based witness falsifies this block (a solver model is NOT required):
{"has_counterexample":true,"counterexample":"kind; before; transition; after; violated clause","specification":"one complete replacement block"}
Otherwise:
{"has_counterexample":false,"counterexample":"specific missing fact or uncertainty","specification":null}
Do not add reasoning outside JSON, fences, extra keys, or literal unescaped
newlines/backslashes inside strings. Escape each ACSL backslash for JSON, e.g.
"ensures \\result >= 0;". Do not output the placeholder ellipsis as a clause.
