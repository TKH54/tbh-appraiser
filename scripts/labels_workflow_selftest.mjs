// Offline reproduction: use the promotion step's pipefail setting, a failing
// producer and a successful tee. Never run promotion or contact Supabase.
import { readFileSync } from 'node:fs';
import { spawnSync, execFileSync } from 'node:child_process';
const path = '.github/workflows/labels.yml';
const source = process.argv.includes('--revision')
  ? execFileSync('git', ['show', `${process.argv[process.argv.indexOf('--revision') + 1]}:${path}`], { encoding: 'utf8' })
  : readFileSync(path, 'utf8');
const step = source.split('      - name: Run promotion gates')[1]
  .split('      - name: Offender-trim')[0];
const setting = step.includes('set -o pipefail') ? 'set -o pipefail\n' : '';
const bash = process.platform === 'win32'
  ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
for (const code of [23, 0]) {
  const result = spawnSync(bash, ['--noprofile', '--norc', '-e', '-c',
    `${setting}(exit ${code}) | tee /dev/null`], { encoding: 'utf8' });
  if (result.error) throw result.error;
  console.log(`producer=${code}, step=${result.status}, expected=${code}`);
  if (result.status !== code) process.exitCode = 1;
}
