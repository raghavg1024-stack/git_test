from pathlib import Path
import sys

mode = sys.argv[1]
if mode == 'init':
    Path('config.txt').write_text('demo=true')
elif mode == 'seed':
    assert Path('config.txt').exists(), 'missing config: run init first'
    Path('seed.txt').write_text('demo data')
elif mode == 'test':
    assert Path('config.txt').exists(), 'missing config'
    if Path('requires_seed').exists():
        assert Path('seed.txt').exists(), 'missing seed'
    print('GHOST_SMOKE_OK: 1 check passed')
elif mode == 'secret':
    raise SystemExit('missing token: maintainer input required')
elif mode in ('lint', 'deploy'):
    print('CI-only step')
else:
    raise SystemExit('unknown mode')
