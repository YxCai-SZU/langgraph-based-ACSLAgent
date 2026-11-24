"""Check the validated Frama-C version, prover discovery and actual WP parsing.

No model calls or environment changes. The current command excludes termination
goals explicitly; these probes check that loops pass and a false assertion fails.
Install the baseline dependencies using the README commands first.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
from acslagent import framac


# Read the exact version from the install manifest to prevent stale advice.
REFERENCE_VERSION = tomllib.loads((PROJECT/'pyproject.toml').read_text())['tool']['acslagent']['native']['frama-c']
SMOKE_SOURCE = ('/*@ requires 0 <= x <= 100; ensures \\result == x + 1; assigns \\nothing; */\n'
                'int inc(int x) { return x + 1; }\n'
                'int main(void) { int r=inc(2);\n//@ assert r == 3;\nreturn 0; }\n')
# Deliberately no loop variant: this tests the partial-correctness stage that
# FB4C runs before its separate optional termination application.
LOOP_SOURCE = ('int main(void) {\n  int i = 0;\n'
               '  /*@ loop invariant 0 <= i <= 3;\n      loop assigns i; */\n'
               '  while (i < 3) { i++; }\n  //@ assert i == 3;\n  return 0;\n}\n')
FALSE_ASSERT_SOURCE = LOOP_SOURCE.replace('assert i == 3;', 'assert i == 4;')


def check_probe(executable, output, label, source):
    c_file = output/(label+'.c')
    c_file.write_text(source, encoding='utf-8')
    try:
        outcome = framac.run(c_file, output, label=label, executable=executable)
        text = '\n'.join(Path(path).read_text(encoding='utf-8', errors='replace')
                         for path in (outcome.stdout_path, outcome.stderr_path) if path)
        generated = bool(re.search(r'^Goal (?:Termination-condition|Loop variant|Decreasing|Positivity)\b',
                                   text, re.M))
        # A preflight must notice installation errors as well as proof results.
        tool_error = any(marker in text for marker in framac.ABORT_MARKERS) or 'not found in why3.conf' in text
        okay = outcome.strict_passed and outcome.total > 0 and not tool_error and not outcome.process_timed_out
        return outcome.as_dict() | {'generated_termination': generated, 'tool_error': tool_error}, okay
    except (RuntimeError, OSError) as error:
        return {'error': str(error)}, False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frama-c',default='frama-c')
    parser.add_argument('--output',type=Path,default=Path('work/verifier_check'))
    args=parser.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=True)
    report={'frama_c':args.frama_c,'expected_provers':['Alt-Ergo','Z3'],
            'validated_version':REFERENCE_VERSION,
            'verification_scope':framac.VERIFICATION_SCOPE,
            'property_filter':framac.PROPERTY_FILTER}
    for label,command in [('version',[args.frama_c,'-version']),
                          ('wp_provers',[args.frama_c,'-wp-list-provers']),
                          ('why3_provers',['why3','config','list-provers'])]:
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=30)
            report[label]={'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
        except (OSError,subprocess.TimeoutExpired) as error:
            report[label]={'error':str(error)}
    version=report['version']
    version_match=version.get('returncode') == 0 and bool(re.match(
        re.escape(REFERENCE_VERSION)+r'(?:\s|$)',version.get('stdout','').strip()))
    report['reference_version_matches']=version_match
    provers=report['wp_provers']
    missing_provers=[name for name in report['expected_provers']
                     if not re.search(r'\bProver\s+'+re.escape(name)+r'\b',provers.get('stdout',''),re.I)]
    report['missing_provers']=missing_provers
    provers_ok=provers.get('returncode') == 0 and not missing_provers
    report['smoke'],smoke_ok=check_probe(args.frama_c,args.output,'smoke',SMOKE_SOURCE)
    report['loop_smoke'],loop_ok=check_probe(args.frama_c,args.output,'loop_smoke',LOOP_SOURCE)
    report['false_assertion'],_=check_probe(args.frama_c,args.output,'false_assertion',FALSE_ASSERT_SOURCE)
    negative=report['false_assertion']
    assertion_rejected=(negative.get('verdict') == 'fail' and negative.get('proved',0) < negative.get('total',0)
                        and not negative.get('tool_error') and not negative.get('process_timed_out'))
    report['false_assertion_rejected']=assertion_rejected
    issues=[]
    if version.get('returncode') != 0:
        issues.append('Unable to read the verifier version; check the executable.')
    elif not version_match:
        issues.append(f'Expected the validated Frama-C {REFERENCE_VERSION} baseline. '
                      'Other versions need separate compatibility testing; see README.md.')
    if not provers_ok:
        issues.append('Frama-C must list both Alt-Ergo and Z3 with -wp-list-provers; '
                      'install the README dependencies and activate the matching opam environment.')
    if report['loop_smoke'].get('generated_termination'):
        issues.append('The loop probe still contains termination goals; the partial-correctness '
                      'property filter is not taking effect.')
    if not smoke_ok:
        issues.append('Basic verifier/prover smoke test failed; inspect its stdout/stderr.')
    if not loop_ok:
        issues.append('The loop-without-variant probe did not prove all expected goals.')
    if not assertion_rejected:
        issues.append('The deliberately false assertion was not rejected as expected; inspect its diagnostics.')
    report['issues']=issues
    report['probes_passed']=(smoke_ok and loop_ok and assertion_rejected
                            and not report['loop_smoke'].get('generated_termination'))
    okay=version_match and provers_ok and report['probes_passed']
    report['ready']=okay
    (args.output/'check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not okay:
        print('Verifier is not ready for partial-correctness checks:\n- '+'\n- '.join(issues),file=sys.stderr)
        print('Inspect the saved commands and logs, confirm the README installation steps and '
              'frama-c -wp-list-provers, then repeat this check before using a model API. '
              'Frama-C 29 has known output-format incompatibilities with this parser.',file=sys.stderr)
    return 0 if okay else 1


if __name__=='__main__':
    raise SystemExit(main())
