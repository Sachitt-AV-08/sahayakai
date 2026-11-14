/**
 * Class gate: every CI step that builds the Next.js app gives Node the same heap the
 * production image builds with.
 *
 * The bug this catches: the Dockerfile and cloudbuild-ci.yaml set
 * NODE_OPTIONS=--max-old-space-size=4096, but the GitHub workflows that also run
 * `npm run build` did not, so they ran with Node 20's default cap of about 2 GB. When
 * PR #156 grew the app, `next build` aborted out of memory in the Test Suite while the
 * same code built fine for production. A red check that production does not share
 * stops carrying information.
 *
 * Fixing one workflow would not stop the next one, so the gate is on the class: any
 * workflow step whose `run` builds the app must set NODE_OPTIONS with the Dockerfile's
 * --max-old-space-size, in the step's own env or its job's env.
 */
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

const APP_ROOT = path.resolve(__dirname, '..', '..', '..');
const WORKFLOW_DIR = path.join(APP_ROOT, '..', '.github', 'workflows');
const BUILDS_APP = /\b(npm run build|next build)\b/;

/** The heap the production image builds with, read from the Dockerfile so the gate follows it. */
function productionHeap(): string {
  const dockerfile = readFileSync(path.join(APP_ROOT, 'Dockerfile'), 'utf8');
  const m = dockerfile.match(/--max-old-space-size=(\d+)/);
  if (!m) throw new Error('Dockerfile no longer sets --max-old-space-size; update this gate');
  return m[1];
}

interface BuildStep { file: string; job: string; step: string; heap: string | null }

/**
 * Text-based on purpose, like .github/scripts/check_playwright_runners.py: our workflows
 * keep jobs as two-space keys under `jobs:` and steps as `- name:` items. A step's heap
 * is its own `NODE_OPTIONS`, else its job-level one.
 */
function buildSteps(): BuildStep[] {
  const found: BuildStep[] = [];
  for (const file of readdirSync(WORKFLOW_DIR).filter((f) => /\.ya?ml$/.test(f))) {
    const lines = readFileSync(path.join(WORKFLOW_DIR, file), 'utf8').split('\n');
    const jobsAt = lines.indexOf('jobs:');
    if (jobsAt < 0) continue;
    let job = '';
    let jobHeap: string | null = null;
    let step: { name: string; lines: string[] } | null = null;
    const flush = () => {
      if (!step) return;
      const body = step.lines.filter((l) => !l.trim().startsWith('#')).join('\n');
      if (BUILDS_APP.test(body)) {
        const own = body.match(/NODE_OPTIONS:\s*['"]?[^\n]*--max-old-space-size=(\d+)/);
        found.push({ file, job, step: step.name, heap: own ? own[1] : jobHeap });
      }
      step = null;
    };
    for (const line of lines.slice(jobsAt + 1)) {
      if (line && !line.startsWith(' ') && !line.startsWith('#')) break;
      const jobKey = line.match(/^ {2}([A-Za-z0-9_-]+):\s*$/);
      if (jobKey) { flush(); job = jobKey[1]; jobHeap = null; continue; }
      const jobEnvHeap = line.match(/^ {6}NODE_OPTIONS:\s*['"]?[^\n]*--max-old-space-size=(\d+)/);
      if (jobEnvHeap && !step) { jobHeap = jobEnvHeap[1]; continue; }
      const stepStart = line.match(/^ {6}- (?:name:\s*(.*)|run:|uses:)/);
      if (stepStart) { flush(); step = { name: (stepStart[1] ?? line.trim()).trim(), lines: [line] }; continue; }
      if (step) step.lines.push(line);
    }
    flush();
  }
  return found;
}

describe('every CI build of the app uses the production build heap', () => {
  const heap = productionHeap();
  const steps = buildSteps();

  it('finds the build steps to check at all', () => {
    // A gate that scans nothing reports green forever.
    expect(steps.map((s) => s.file)).toEqual(expect.arrayContaining(['test.yml', 'e2e-smoke.yml']));
  });

  it.each(steps.map((s) => [`${s.file} › ${s.job} › ${s.step}`, s] as const))('%s', (_label, s) => {
    expect({ step: `${s.file} › ${s.step}`, heap: s.heap }).toEqual({ step: `${s.file} › ${s.step}`, heap });
  });
});
