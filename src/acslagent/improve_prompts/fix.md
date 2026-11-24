COUNTEREXAMPLE ANALYSIS AND ONE REPLACEMENT, for task.task_id only.
Analyze current_specification against the actual C code and diagnostics. Each
failure includes its kind and whether it was observed in a local or full check.
The local and whole-file proof contexts can differ. Remaining retries and
repairs are independent lifetime budgets; the controller charges them.

Use this procedure internally, then output only the small JSON object:
1. Identify the exact clause/obligation at this task. Focus on failed clauses
   first; do not reason at length about successful or unrelated goals.
2. Seek a concrete witness using the code, preconditions and C types:
   - Initiation: compute the first loop-head state from an allowed function
     input; show the claimed invariant is false before the first iteration.
   - Preservation: give a loop-head state satisfying ALL invariants and guard,
     execute one FULL body (including branches and inner loops), and show a
     clause is false at the next head. State whether this is an abstract
     inductiveness witness or an execution reachable from function entry.
   - Postcondition: choose a legal input, evaluate a defined terminating path,
     and show return/output state contradicts the selected postcondition.
   - Frame: identify a concrete modified location omitted by this block's
     assigns. Respect aliasing and valid memory; do not use undefined behavior.
   - Assertion/call precondition: check the selected block's claims separately.
     A counterexample to an original assertion is not automatically a
     counterexample to the selected invariant or contract.
   Try zero iterations, one iteration, boundary/negative values, and a small
   array when relevant. Check the witness against every applicable requirement.
3. If a witness falsifies this block, set has_counterexample=true and return
   ONE complete corrected replacement. Preserve useful valid clauses and frame;
   fix the cause without excluding legal inputs or editing another task.
4. If the block appears valid but is too weak, or you cannot establish a
   witness, set false, explain the missing relationship or uncertainty briefly,
   and specification=null. This causes a retry with the configured number of new alternatives.
   Do not invent a witness just to spend repairs. A timeout alone proves
   neither validity nor invalidity.

CRITICAL: A solver-produced model is NOT required. Your manually reasoned,
concrete witness is sufficient for true, even when WP reports Timeout/Unknown.
These witnesses are recorded as unvalidated_llm_analysis. Never call them
solver-certified or dynamically tested. Do not write a concrete violating input
and then say false merely because WP did not supply it.

Example: sum(a,n) adds arbitrary signed elements under valid(a+(0..n-1)), n>=0.
Its selected contract claims ensures result>=0. With a={-1}, n=1, the defined
execution returns -1. This is a postcondition counterexample even if WP timed
out. Return true and a corrected contract describing the actual sum, using
only supported ACSL and available logic. Do not impose a[i]>=0 to conceal it.
Conversely, invariant 0<=i<=n may be valid but omit the accumulated sum needed
by the postcondition. That weakness alone is NOT a counterexample to the bound;
return false and identify the missing accumulator/processed-prefix relation.

Keep the counterexample explanation concise (normally under 150 words):
kind; before/input; relevant transition; after/output; exact violated clause.
For false, give the specific uncertainty or missing fact in at most 100 words.
Check history for duplicates. Never emit multiple candidates, long analysis, C code,
or revised source files. The JSON boolean must agree with your explanation.
