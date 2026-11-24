"""Build the local LLVM 12 call-graph fix; preserve the original vendor files."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PROJECT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT/'.tools'
VENDOR = PROJECT/'vendor/acslagent/LLM4Veri/llvm'
ARCHIVE = 'clang+llvm-12.0.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz'
ARCHIVE_SHA256 = '6b3cc55d3ef413be79785c4dc02828ab3bd6b887872b143e3091692fc6acefe7'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def patched_source(text):
    old = '''                    // errs()<<*callInst;
                    if (callBase->isIndirectCall()) {

                      pointerAnalysis->addToWorkList(&Ins);
                      continue;
                    }

                    // Direct Call
                    Function *calleeFunc = callBase->getCalledFunction();
                    if(calleeFunc->isIntrinsic()) continue; // Ignored Function calls inserted by LLVM'''
    new = '''                    // A direct callee may be wrapped in a constant pointer cast
                    // (e.g. a C call without a prototype). getCalledFunction()
                    // returns null for that form; resolve it before dereferencing.
                    Function *calleeFunc = dyn_cast<Function>(
                        callBase->getCalledOperand()->stripPointerCasts());
                    if (!calleeFunc) {
                      // Preserve the existing points-to analysis for indirect calls.
                      pointerAnalysis->addToWorkList(&Ins);
                      continue;
                    }
                    if(calleeFunc->isIntrinsic()) continue; // Ignored Function calls inserted by LLVM'''
    if text.count(old) != 1:
        raise RuntimeError('unexpected original CallGraph.cpp; refusing an ambiguous patch')
    # The original includes Boost but never uses it. Avoid a global dependency.
    return text.replace(old, new).replace('#include <boost/algorithm/string.hpp>\n', '')


def build():
    if sys.platform != 'linux':
        raise RuntimeError('run build_analyzer.py inside WSL/Linux, or use setup_native.py')
    original = VENDOR/'llvm-pass/src'
    source = patched_source((original/'CallGraph.cpp').read_text())
    inputs = {p.name: digest(p) for p in original.iterdir() if p.suffix in ('.cpp', '.h')}
    inputs.update(patch=hashlib.sha256(source.encode()).hexdigest(),
                  compiler=digest(TOOLS/'llvm12/bin/clang-12'), wrapper=digest(VENDOR/'bin/veri-clang'))
    target = TOOLS/'veri-clang-fixed'
    manifest = target/'build.json'
    library = target/'lib/veri-llvm-pass.so'
    wrapper = target/'bin/veri-clang'
    if manifest.exists() and library.exists() and wrapper.exists():
        previous = json.loads(manifest.read_text())
        if (previous.get('inputs') == inputs and previous.get('sha256') == digest(library)
                and digest(wrapper) == inputs['wrapper']):
            wrapper.chmod(0o755)
            print('Using checked local analyzer: '+str(target), flush=True)
            return target
    sdk = TOOLS/'llvm12-sdk'
    if not (sdk/'include/llvm/IR/Module.h').exists():
        archive = TOOLS/'downloads'/ARCHIVE
        if not archive.exists() or digest(archive) != ARCHIVE_SHA256:
            raise RuntimeError('LLVM 12 archive missing or invalid; run scripts/setup_native.py')
        sdk.mkdir(parents=True, exist_ok=True)
        print('Extracting LLVM 12 headers from the existing archive', flush=True)
        with tarfile.open(archive, 'r|xz') as stream:
            member_root = next(iter(stream)).name.split('/')[0]
        subprocess.run(['tar', '-xJf', str(archive), '-C', str(sdk), '--strip-components=1',
                        member_root+'/include'], check=True)
    staging = TOOLS/'analyzer-build/src'
    staging.mkdir(parents=True, exist_ok=True)
    for p in original.iterdir():
        if p.suffix in ('.cpp', '.h'):
            shutil.copy2(p, staging/p.name)
    (staging/'CallGraph.cpp').write_text(source)
    library.parent.mkdir(parents=True, exist_ok=True)
    temporary = library.with_suffix('.so.tmp')
    command = [str(TOOLS/'llvm12/bin/clang-12'), '--driver-mode=g++', '-std=c++14',
               '-O2', '-fPIC', '-shared', '-fno-rtti', '-I'+str(sdk/'include'),
               str(staging/'CallGraph.cpp'), str(staging/'Graph.cpp'), str(staging/'PointerAnalysis.cpp'),
               '-o', str(temporary)]
    env = dict(os.environ, LD_LIBRARY_PATH=str(TOOLS/'compat/lib/x86_64-linux-gnu'))
    print('Building local call-graph plugin', flush=True)
    subprocess.run(command, env=env, check=True)
    temporary.replace(library)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VENDOR/'bin/veri-clang', wrapper)
    wrapper.chmod(0o755)
    manifest.write_text(json.dumps(dict(inputs=inputs, command=command, sha256=digest(library)), indent=2)+'\n')
    print('Built local analyzer: '+str(target), flush=True)
    return target


if __name__ == '__main__':
    build()
