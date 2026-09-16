// Real curl against an HTTP fixture on loopback only. No Discord/Steam access.
// --revision main tests the original workflow text without checking it out.
import { readFileSync } from 'node:fs';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:http';

const revision = process.argv[process.argv.indexOf('--revision') + 1];
const baseline = process.argv.includes('--revision');
const files = ['autocatalog', 'labels', 'pages', 'prices', 'rollback', 'notify_test'];
const flagSets = new Map();
for (const file of files) {
  const path = `.github/workflows/${file}.yml`;
  const source = baseline ? execFileSync('git', ['show', `${revision}:${path}`], { encoding: 'utf8' })
    : readFileSync(path, 'utf8');
  for (const match of source.matchAll(/curl (.*?) -H "Content-Type: application\/json"/g)) {
    flagSets.set(match[1], [...(flagSets.get(match[1]) || []), file]);
  }
}
if (!flagSets.size) throw Error('No Discord curl commands found');
const server = createServer((req, res) => { req.resume(); res.writeHead(Number(req.url.slice(1))); res.end(); });
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  for (const [flags, owners] of flagSets) {
    console.log(`flags=${flags}; workflows=${[...new Set(owners)].join(',')}`);
    for (const status of [204, 400, 429, 500]) {
      const code = await new Promise((resolve, reject) => {
        const child = spawn(process.platform === 'win32' ? 'curl.exe' : 'curl',
          [...flags.split(' '), '--noproxy', '*', '-H', 'Content-Type: application/json',
            '--data', '{"content":"offline fixture"}',
            `http://127.0.0.1:${server.address().port}/${status}`], { stdio: 'ignore' });
        child.on('error', reject); child.on('exit', resolve);
      });
      const expected = status === 204 ? 0 : 22;
      console.log(`HTTP ${status}: exit=${code}, expected=${expected}`);
      if (code !== expected) process.exitCode = 1;
    }
  }
} finally { server.close(); }
