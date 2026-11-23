"""Replay historical model replies through the graph and the real verifier.

This makes no network/model requests. It validates plumbing and environment;
replies generated for old prompts are not a new model-quality experiment.
"""
import argparse
import json
from collections import defaultdict, deque
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acslagent import cli, graph, llm
from acslagent.trace import read_trace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recorded_run',type=Path)
    parser.add_argument('source',type=Path)
    parser.add_argument('--run-id',default='count_replay_fixed')
    parser.add_argument('--output',default='runs')
    parser.add_argument('--retries',type=cli.retry_budget,default=graph.MAX_RETRIES,
                        help='FB4C outer-loop attempts, including the first (default 5)')
    parser.add_argument('--repairs',type=cli.repair_budget,default=graph.MAX_REPAIRS)
    parser.add_argument('--frama-c',default='frama-c')
    parser.add_argument('--analyzer-config',default='.tools/native.json')
    args=parser.parse_args()
    # One task can now have several proposals in the same round. Preserve their
    # order instead of overwriting all but the last reply under (round, task).
    replies=defaultdict(deque)
    for event in read_trace(args.recorded_run):
        if event['node']=='generate' and event['event']=='reply':
            replies[(event['round'],event['task_id'])].append(event['detail'])
    run_dir=Path(args.output)/args.run_id
    def ask(config,messages,temperature,*,attempts):
        request=next(e for e in reversed(read_trace(run_dir)) if e['node']=='generate' and e['event']=='request')
        key=(request['round'],request['task_id'])
        if not replies[key]:
            raise RuntimeError(f'No historical reply for round/task {key}; replay stops without contacting a model')
        detail=replies[key].popleft()
        choices=detail.get('choices')
        if choices is None:
            choices=[detail['raw']]  # Historical v1 traces.
        return llm.Reply(choices[0],0,0,{'source':'recorded_reply','round':key[0],'task':key[1]},tuple(choices))
    argv=['run',str(args.source),'--run-id',args.run_id,'--output',args.output,
          '--retries',str(args.retries),'--repairs',str(args.repairs),
          '--frama-c',args.frama_c,'--analyzer-config',args.analyzer_config]
    with patch.object(llm,'ask',ask),patch.object(graph.ModelConfig,'load',return_value=None):
        return cli.main(argv)


if __name__=='__main__':
    raise SystemExit(main())
