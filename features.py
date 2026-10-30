"""Optional evidence features. All execution reuses the core Docker runner."""
import argparse
import ast
from collections import Counter
import concurrent.futures
import html
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import ghostmaintainer as gm


def source(path, repo, line):
    return f'{path.relative_to(repo).as_posix()}:{line}'


def drift(repo):
    """Advisory findings only; conservative literal-name extraction."""
    docs_path = repo / 'README.md'
    docs = gm.read(docs_path) if docs_path.exists() else ''
    env_path = repo / '.env.example'
    example = gm.read(env_path) if env_path.exists() else ''
    variables, versions = [], []
    for path in repo.rglob('*'):
        if not path.is_file() or path.is_symlink() or any(p in gm.EXCLUDE for p in path.relative_to(repo).parts):
            continue
        if path.suffix not in ('.py', '.js', '.ts', '.jsx', '.tsx'):
            continue
        if path.stat().st_size > 1_000_000:
            continue
        text = gm.read(path)
        if path.suffix == '.py':
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Subscript) and ast.unparse(node.value) == 'os.environ' and isinstance(node.slice, ast.Constant):
                    name = node.slice.value
                if isinstance(node, ast.Call) and ast.unparse(node.func) in ('os.getenv', 'os.environ.get') and node.args and isinstance(node.args[0], ast.Constant):
                    name = node.args[0].value
                if isinstance(name, str):
                    variables.append({'name': name, 'source': source(path, repo, node.lineno)})
        else:
            for n, line in enumerate(text.splitlines(), 1):
                for name in re.findall(r'process\.env\.([A-Z_][A-Z0-9_]*)', line):
                    variables.append({'name': name, 'source': source(path, repo, n)})
    notes = []
    for item in variables:
        name = item['name']
        in_docs = bool(re.search(r'\b'+re.escape(name)+r'\b', docs))
        in_example = bool(re.search(r'^\s*'+re.escape(name)+r'\s*=', example, re.M))
        if not in_docs or not in_example:
            notes.append({'kind': 'environment', **item, 'documented': in_docs, 'in_env_example': in_example,
                          'readme_source': f'README.md:{next((i for i,l in enumerate(docs.splitlines(),1) if name in l), "not found")}',
                          'example_source': f'.env.example:{next((i for i,l in enumerate(example.splitlines(),1) if l.startswith(name+"=")), "not found")}',
                          'message': f'{name} is read in code; ' + ('README mention missing. ' if not in_docs else '') + ('.env.example entry missing.' if not in_example else '')})
    paths = [docs_path, repo/'Dockerfile', repo/'pyproject.toml', repo/'.python-version', repo/'.nvmrc', repo/'package.json']
    paths += list((repo/'.github'/'workflows').glob('*.y*ml'))
    for path in paths:
        if not path.exists():
            continue
        for n, line in enumerate(gm.read(path).splitlines(), 1):
            matches = []
            if path.name == 'README.md':
                matches = [('python', m) for m in re.findall(r'(?i)Python\s+(\d+\.\d+)', line)] + [('node', m) for m in re.findall(r'(?i)Node(?:\.js)?\s+(\d+)', line)]
            elif path.name in ('.python-version', '.nvmrc'):
                matches = [('python' if path.name == '.python-version' else 'node', line.strip().lstrip('v'))]
            elif path.name == 'Dockerfile':
                matches = re.findall(r'(?i)FROM\s+(python|node):(\d+(?:\.\d+)?)', line)
            else:
                for language, pattern in [('python', r'python-version:\s*["\x27]?(\d+\.\d+)'), ('node', r'node-version:\s*["\x27]?(\d+)')]:
                    matches += [(language, m) for m in re.findall(pattern, line)]
                if 'requires-python' in line:
                    matches += [('python-range', line.split('=', 1)[-1].strip())]
                if path.name == 'package.json' and re.search(r'"node"\s*:', line):
                    matches += [('node-range', line.strip())]
            versions += [{'language': lang, 'value': value, 'source': source(path, repo, n)} for lang,value in matches if value]
    for language in ('python', 'node'):
        declared = [v for v in versions if v['language'] == language and v['source'].startswith('README.md:')]
        configured = [v for v in versions if v['language'] == language and not v['source'].startswith('README.md:')]
        for doc in declared:
            for config in configured:
                if doc['value'] != config['value']:
                    notes.append({'kind': 'version', 'message': f'{language}: README says {doc["value"]}; configuration specifies {config["value"]}. Check compatibility; different supported versions are not necessarily errors.', 'readme': doc, 'configuration': config})
    return {'notes': notes, 'version_evidence': versions, 'scope': 'Advisory literal Python/Node scan; no patches. Dynamic variable names and version-range compatibility are not inferred.'}


def friction(report):
    phases = {}
    for phase in ('original', 'negative_control', 'verification'):
        if phase not in report:
            continue
        steps = report[phase]['events']
        times = [e.get('duration_s') for e in steps]
        phases[phase] = {'sandbox_time_s': round(sum(t or 0 for t in times), 3) if all(t is not None for t in times) else None,
                         'steps': steps, 'slowest_step': max(steps, key=lambda e:e.get('duration_s',0), default=None)}
    return phases


def ship(repo, options, report=None, destination=None):
    report = report or gm.drill(repo, options)
    destination = destination or options.out / repo.name / 'ship'
    destination.mkdir(parents=True, exist_ok=True)
    verification = report.get('verification', report['original'])
    if report['status'] not in ('repaired', 'healthy') or not verification['ok']:
        raise ValueError('Ship requires a successful fixed verification target')
    commands = [e['cmd'] for e in verification['events']]
    script = '#!/usr/bin/env bash\nset -euo pipefail\n' + '\n'.join(commands) + '\n'
    (destination/'setup.sh').write_text(script, encoding='utf-8', newline='\n')
    image = getattr(options, 'image', 'python:3.12-slim')
    gm.write_json(destination/'devcontainer.json', {'name':'GhostMaintainer setup', 'image':image,
        'postCreateCommand':'bash ship/setup.sh', 'customizations':{'vscode':{'settings':{'ghostmaintainer.notice':'Generated from verified commands; devcontainer not separately verified'}}}})
    (destination/'AGENTS_setup.md').write_text('# Setup\n\nCommands passed the declared verification target. The setup script has a separate verification result in verification.json. This agent document and devcontainer were generated, not separately verified. Copy this entire ship directory to the repository root before use.\n\n```sh\n'+'\n'.join(commands)+'\n```\n', encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='ghost-ship-') as temp:
        checkout = Path(temp)/'repo'
        gm.copy_repo(repo, checkout)
        target = checkout/'ship'
        target.mkdir(exist_ok=True)
        (target/'setup.sh').write_text(script, encoding='utf-8', newline='\n')
        if report.get('patched_readme'):
            (checkout/'README.md').write_text(report['patched_readme'], encoding='utf-8')
        result = gm.run(checkout, [{'cmd':'bash ship/setup.sh'}], report['goal'], 'docker', options.net, image=image)
    status = {'setup_script':'verified' if result['ok'] else 'verification failed',
              'devcontainer':'generated, not separately verified', 'agent_document':'generated, not separately verified', 'result':result}
    gm.write_json(destination/'verification.json', status)
    if not result['ok']:
        raise ValueError('Generated setup.sh failed its fresh-container self-check; see verification.json')
    return status


def matrix(repo, options):
    environments = options.matrix.split(',')
    if not 1 <= len(environments) <= 6 or any(not re.fullmatch(r'py3\.\d{1,2}', e) for e in environments):
        raise ValueError('Use 1–6 Python environments such as py3.9,py3.11,py3.12')
    def cell(environment):
        image = f'python:{environment[2:]}-slim'
        opts = SimpleNamespace(**{**vars(options), 'image':image})
        report = gm.drill(repo, opts)
        gm.save_report(report, options.out/repo.name/environment)
        return {'environment':environment, 'image':image, 'report':report}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        cells = list(pool.map(cell, environments))
    audit = drift(repo)
    pins = [v for v in audit['version_evidence'] if v['language']=='python' and '.github/' in v['source']]
    note = f'Requires Python {pins[0]["value"]} (source: {pins[0]["source"]}).' if pins and any(not c['report']['original']['ok'] for c in cells) else None
    result = {'cells':[{'environment':c['environment'],'status':c['report']['status'],'image':c['image']} for c in cells], 'version_note':note,
              'note_limit':'A documented support version does not repair an unsupported runtime.'}
    folder = options.out/repo.name
    gm.write_json(folder/'matrix.json', result)
    if note:
        import difflib
        original = gm.read(repo/'README.md')
        patch = ''.join(difflib.unified_diff(original.splitlines(True),(original.rstrip()+'\n\n'+note+'\n').splitlines(True),fromfile='a/README.md',tofile='b/README.md'))
        (folder/'version-note.patch').write_text(patch,encoding='utf-8')
    commands = list(dict.fromkeys(e['cmd'] for c in cells for e in c['report']['original']['events']))
    rows = ''
    for command in commands:
        values = []
        for c in cells:
            event = next((e for e in c['report']['original']['events'] if e['cmd']==command), None)
            status = 'Passed' if event and event['exit_code']==0 else 'Failed' if event else 'Not reached'
            values.append(f'<td style="color:{"#137346" if status=="Passed" else "#b52c2c"}">{status}</td>')
        rows += '<tr><td>'+html.escape(command)+'</td>'+''.join(values)+'</tr>'
    page = '<!doctype html><meta name="viewport" content="width=device-width"><title>Runtime matrix</title><style>body{font:17px system-ui;max-width:1000px;margin:40px auto;padding:20px;background:#f5f7fa}td,th{padding:18px;border:1px solid #ccd5de}table{border-collapse:collapse;background:white}</style><h1>Same instructions. Different Python versions.</h1><table><tr><th>Command</th>'+''.join(f'<th><a href="{c["environment"]}/replay.html">{c["environment"]}</a></th>' for c in cells)+'</tr>'+rows+'</table><p>'+html.escape(note or 'No supported-version note inferred.')+'</p><p>Each cell runs the same README and verification target. Unsupported runtimes remain red.</p>'
    (folder/'matrix.html').write_text(page,encoding='utf-8')
    return result


DENY = re.compile(r'\b(?:sudo|docker|ssh|scp|doas)\b|\brm\s+[^\n]*-[a-z]*r[a-z]*f|\brm\s+[^\n]*-[a-z]*f[a-z]*r|\b(?:curl|wget)\b[^\n]*\|\s*(?:ba)?sh|\b(?:eval|exec)\s', re.I)


def safe_commands(repo):
    commands = gm.extract(gm.read(repo/'README.md')) + gm.config_commands(repo)
    goal = repo/'ghostmaintainer.json'
    if goal.exists():
        commands += [{'cmd':json.loads(gm.read(goal))['cmd']}]
    return [c for c in commands if DENY.search(c['cmd'])]


def clone(url, destination, commit=None):
    from app import repository_url
    canonical = repository_url(url)
    import os
    env = {**os.environ,'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_LFS_SKIP_SMUDGE':'1'}
    result = subprocess.run(['git','-c','credential.helper=','clone','--depth','1','--no-recurse-submodules','--',canonical,str(destination)],capture_output=True,text=True,timeout=45,env=env,stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('Clone failed')
    if commit:
        if not re.fullmatch('[a-fA-F0-9]{40}',commit):
            raise ValueError('Benchmark commit must be a full SHA')
        subprocess.run(['git','-C',str(destination),'fetch','--depth','1','origin',commit],check=True,capture_output=True,timeout=45,env=env)
        subprocess.run(['git','-C',str(destination),'checkout','--detach',commit],check=True,capture_output=True,timeout=10,env=env)
    if any(p.is_symlink() for p in destination.rglob('*')):
        raise ValueError('Symlinked repositories are unsupported')
    return subprocess.check_output(['git','-C',str(destination),'rev-parse','HEAD'],text=True).strip()


def scan(options):
    urls = [s.strip() for s in gm.read(options.list).splitlines() if s.strip() and not s.startswith('#')][:options.max_repos]
    options.out.mkdir(parents=True,exist_ok=True)
    results = []
    for index,url in enumerate(urls):
        row = {'index':index+1}
        with tempfile.TemporaryDirectory(prefix='ghost-scan-') as temp:
            repo = Path(temp)/'repo'
            try:
                revision = clone(url,repo)
                license_file = next((p for p in repo.iterdir() if p.is_file() and p.name.lower() in ('license','licence','license.md','license.txt')),None)
                license_text = gm.read(license_file) if license_file else ''
                if not any(term in license_text for term in ('Permission is hereby granted','Redistribution and use','Apache License')):
                    raise ValueError('skip: permissive license not identified')
                if not (repo/'README.md').exists():
                    raise ValueError('skip: root README.md missing')
                if safe_commands(repo):
                    raise ValueError('skip: setup command safety filter')
                if not (repo/'ghostmaintainer.json').exists():
                    gm.write_json(repo/'ghostmaintainer.json', {'cmd':'python -m unittest discover -v'})
                # Separate CLI process gives a hard total deadline, including model/repair loops.
                command = [sys.executable,str(gm.ROOT/'ghostmaintainer.py'),'drill',str(repo),'--out',str(Path(temp)/'results'),'--image',options.image]
                if options.net:
                    command += ['--net']
                subprocess.run(command,capture_output=True,text=True,timeout=options.timeout,stdin=subprocess.DEVNULL)
                reports = list((Path(temp)/'results').rglob('report.json'))
                if not reports:
                    raise ValueError('inconclusive: no report')
                report = json.loads(gm.read(reports[0]))
                failed = next((i+1 for i,e in enumerate(report['original']['events']) if e['exit_code']),None)
                failure = next((e for e in report['original']['events'] if e['exit_code']),{})
                row.update(status={'repaired':'doc_gap'}.get(report['status'],report['status']), first_failure_step=failed,
                           signature=report['original']['status'] if not failure else f'exit_{failure["exit_code"]}',license=license_file.name,commit=revision)
            except subprocess.TimeoutExpired:
                row.update(status='inconclusive',signature='timeout')
            except (ValueError,OSError,subprocess.SubprocessError) as error:
                row.update(status='skipped' if str(error).startswith('skip:') else 'inconclusive',signature=str(error))
        results.append(row)
        (options.out/'scan_results.jsonl').write_text('\n'.join(json.dumps(r) for r in results)+'\n',encoding='utf-8')
    count = Counter(r['status'] for r in results)
    positions = [r['first_failure_step'] for r in results if r.get('first_failure_step')]
    aggregate = {'n_selected':len(urls),'n_completed':len(results),'counts':dict(count),
                 'percent_of_selected':{key:round(100*value/len(urls),1) for key,value in count.items()} if urls else {},
                 'median_first_failure_step':statistics.median(positions) if positions else None,
                 'failure_signatures':dict(Counter(r.get('signature') for r in results if r.get('signature'))),
                 'limit':'Local aggregate only. Hand-selected sample; no ecosystem-wide prevalence claim. Skipped repos are not healthy. Source repositories are not named in results.'}
    gm.write_json(options.out/'scan_summary.json',aggregate)
    (options.out/'SCAN.md').write_text('# Ecosystem scan\n\n```json\n'+json.dumps(aggregate,indent=2)+'\n```\n',encoding='utf-8')
    return aggregate


def benchmark(options):
    dataset = json.loads(gm.read(options.dataset))
    rows = []
    for model in options.models.split(','):
        correct,total,healthy,false_positive,recovered,eligible = 0,0,0,0,0,0
        latency,errors = [],[]
        for record in dataset:
            with tempfile.TemporaryDirectory(prefix='ghost-bench-') as temp:
                repo = gm.ROOT/record['path'] if 'path' in record else Path(temp)/'repo'
                try:
                    if 'url' in record:
                        clone(record['url'],repo,record['commit'])
                    text = gm.read(repo/'README.md')
                    start = time.perf_counter()
                    selected,meta = gm.label(text,model)
                    latency.append(time.perf_counter()-start)
                    expected = set(record['indices'])
                    actual = {i for i,s in enumerate(gm.extract(text)) if s in selected}
                    correct += sum((i in expected)==(i in actual) for i in range(len(gm.extract(text))))
                    total += len(gm.extract(text))
                    if record.get('evaluate'):
                        opts = SimpleNamespace(backend='docker',net=False,labeller=model,image=options.image)
                        report = gm.drill(repo,opts)
                        if record.get('healthy'):
                            healthy += 1
                            false_positive += report['status']=='repaired'
                            if report['original']['ok']:
                                for step in gm.extract(text):
                                    lines=text.splitlines(True);del lines[step['line']-1]
                                    mutation=gm.drill(repo,opts,''.join(lines))
                                    if not mutation['original']['ok']:
                                        eligible+=1;recovered+=mutation['status']=='repaired'
                except Exception as error:
                    errors.append({'case':record.get('path',record.get('url')),'error':str(error)})
        rows.append({'model':model,'label_accuracy':correct/total if total else None,'n_steps':total,
                     'recovery_rate':recovered/eligible if eligible else None,'n_failure_inducing_mutations':eligible,
                     'false_positives':false_positive,'n_healthy_repos':healthy,'median_label_latency_s':round(statistics.median(latency),3) if latency else None,
                     'n_readmes':len(latency),'errors':errors,'cache_policy':'Existing model-response cache may be used; latency includes cache effects.'})
    options.out.mkdir(parents=True,exist_ok=True)
    gm.write_json(options.out/'bench_models.json',rows)
    table = '# GhostBench v0\n\nSmall hand-labelled sample; no general model ranking. Latency may include cache hits.\n\n|Model|Label accuracy (n steps)|Recovery (n mutations)|False positives (n healthy)|Median label seconds (n READMEs)|\n|---|---|---|---|---|\n'
    for row in rows:
        table += f'|{row["model"]}|{row["label_accuracy"]} ({row["n_steps"]})|{row["recovery_rate"]} ({row["n_failure_inducing_mutations"]})|{row["false_positives"]} ({row["n_healthy_repos"]})|{row["median_label_latency_s"]} ({row["n_readmes"]})|\n'
    (options.out/'BENCH.md').write_text(table,encoding='utf-8')
    return rows


def report_panels(report):
    escape = lambda value:html.escape(str(value))
    panels = '<section><h2>Friction · measured sandbox time</h2><p>Execution time, not estimated human onboarding time. Totals exclude image startup and model inference.</p>'
    for phase,data in friction(report).items():
        panels += f'<h3>{phase.replace("_"," ").title()} · {data["sandbox_time_s"] if data["sandbox_time_s"] is not None else "not recorded"} seconds</h3>'
        maximum=max((e.get('duration_s',0) for e in data['steps']),default=1) or 1
        for event in data['steps']:
            duration=event.get('duration_s')
            panels += f'<div><code>{escape(event["cmd"])}</code> · {duration if duration is not None else "not recorded"} s {"· failed" if event["exit_code"] else ""}<div style="height:7px;background:{"#bc453b" if event["exit_code"] else "#2c8a59"};width:{max(1,round((duration or 0)/maximum*100))}%"></div></div>'
        if data['slowest_step']:
            panels += f'<p>Slowest: {escape(data["slowest_step"]["cmd"])}</p>'
    panels += '</section>'
    if report.get('drift') is not None:
        notes=report['drift']['notes']
        panels += '<section><h2>Config drift notes · advisory</h2>'+(''.join(f'<p>{escape(n["message"])} <small>{escape(n.get("source",n.get("configuration",{}).get("source","")))}</small></p>' for n in notes) or '<p>No literal environment or version drift found in the supported checks.</p>')+'</section>'
    events=[{'phase':phase,**e} for phase in ('original','negative_control','verification') for e in report.get(phase,{}).get('events',[])]
    if not events:
        return panels
    payload=json.dumps(events).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    panels += '<section><h2>Replay the recorded run</h2><p>Move the slider or use its arrow keys. This replays saved evidence; it does not rerun commands.</p><input id="replaySlider" aria-label="Recorded step" type="range" min="0" max="'+str(len(events)-1)+'" value="0" style="width:100%"><p id="replayLabel"></p><pre id="replayOutput"></pre><p id="replayScore"></p><noscript><p>JavaScript is off. Expand the full logs above to read the complete static timeline.</p></noscript></section>'
    panels += '<script type="application/json" id="replayData">'+payload+'</script><script>(()=>{const events=JSON.parse(document.getElementById("replayData").textContent),slider=document.getElementById("replaySlider");function show(){const i=Number(slider.value),e=events[i],seen=events.slice(0,i+1).filter(x=>x.phase===e.phase),passed=seen.filter(x=>x.exit_code===0).length;document.getElementById("replayLabel").textContent=`Step ${i+1} of ${events.length} · ${e.phase} · ${e.cmd} · ${e.exit_code===0?"passed":"failed"}`;document.getElementById("replayOutput").textContent=e.output;document.getElementById("replayScore").textContent=`${passed}/${seen.length} executed steps passed in this phase so far. This is step completion, not overall correctness.`;}slider.addEventListener("input",show);show();})();</script>'
    return panels


def main(argv):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['drift','ship','bench-models','scan','guard'])
    parser.add_argument('repo',nargs='?',type=Path)
    parser.add_argument('--out',type=Path,default=gm.ROOT/'runs'/'features')
    parser.add_argument('--image',default='python:3.12-slim')
    parser.add_argument('--net',nargs='?',const='on',choices=['on','off'],default='off')
    parser.add_argument('--labeller',default='rules')
    parser.add_argument('--models',default='rules,qwen3:4b-instruct')
    parser.add_argument('--dataset',type=Path,default=gm.ROOT/'bench'/'labels.json')
    parser.add_argument('--list',type=Path)
    parser.add_argument('--max-repos',type=int,default=20)
    parser.add_argument('--timeout',type=int,default=180)
    options=parser.parse_args(argv);options.net=options.net=='on';options.backend='docker'
    try:
        if options.command=='bench-models': result=benchmark(options)
        elif options.command=='scan':
            if options.list is None or not 1<=options.max_repos<=20 or not 10<=options.timeout<=600:
                raise ValueError('Scan requires --list and limits of 1–20 repos / 10–600 seconds')
            result=scan(options)
        elif options.repo is None: raise ValueError('Repository folder required')
        elif options.command=='drift':
            result=drift(options.repo.resolve());gm.write_json(options.out/'drift.json',result)
        elif options.command=='ship':
            result=ship(options.repo.resolve(),options)
        else:
            result=gm.drill(options.repo.resolve(),options)
            gm.save_report(result,options.out/options.repo.name)
            diff=result.get('diff')
            body='## GhostMaintainer PR Guard\n\nResult: **'+result['status']+'**.\n\n'+('```diff\n'+diff+'\n```' if diff else 'No verified documentation patch proposed.')+'\n\nSee the onboarding-evidence workflow artifact for the full report. Fork PRs receive artifacts only; no write token.'
            options.out.mkdir(parents=True,exist_ok=True)
            (options.out/'comment.md').write_text(body,encoding='utf-8')
            return 0 if result['status'] in ('healthy','repaired') else 2
        print(json.dumps(result,indent=2));return 0
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(str(error),file=sys.stderr);return 1
