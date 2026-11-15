/**
 * Class gate: no real calling-provider resource id is committed. The repository is public.
 *
 * The bug this catches: the develop work committed the real ElevenLabs agent ids (the unified
 * SARGVISION agent, the Riya clone and its source), an agent branch id and the phone-number ids
 * behind the company caller ID, in launchers, `.env.example`, docs and test fixtures. With the
 * agent's authentication off, anyone holding an agent id can talk to it on the company's
 * account. Real ids belong in the operator's shell (RIYA_REAL_AGENT_ID, RIYA_REAL_PHONE_NUMBER_ID,
 * RIYA_REAL_SOURCE_AGENT_ID, RIYA_STAGING_PHONE_NUMBER_ID) or in Secret Manager.
 *
 * The gate scans every tracked text file in the app, the agents service and the mock CRM for an
 * ElevenLabs-shaped id (`agent_…`, `phnum_…`, `agtbrch_…`, 20 or more characters after the
 * prefix). An id passes only if it is visibly made up: its body starts with 0000, fake,
 * localtest, other or source.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const MONOREPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const SCANNED = ['sahayakai-main', 'sahayakai-agents', 'mock-school-crm'];
const BINARY = /\.(png|jpe?g|gif|webp|ico|svg|ulaw|wav|mp3|mp4|webm|pdf|woff2?|ttf|otf|zip|gz|lock)$|package-lock\.json$/i;
const PROVIDER_ID = /\b(agent|phnum|agtbrch)_([0-9a-z]{20,})\b/g;
const MADE_UP = /^(([0-9])\2{3}|fake|localtest|other|source|simulated)/;

function trackedTextFiles(): string[] {
    const out = execFileSync('git', ['ls-files', '-z', ...SCANNED], { cwd: MONOREPO_ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return out.split('\0').filter((f) => f && !BINARY.test(f));
}

describe('no real calling-provider id is committed', () => {
    const files = trackedTextFiles();

    it('scans the tracked files at all', () => {
        // A gate that scans nothing reports green forever.
        expect(files.length).toBeGreaterThan(1000);
        expect(files).toContain('sahayakai-main/.env.example');
    });

    it('recognises a real-shaped id and lets a made-up one through', () => {
        const real = 'agent_' + '1234m5abcdefghijklmnopqrstuv';
        const fake = 'agent_' + 'fakeagent1'.padEnd(28, '0');
        expect([...real.matchAll(PROVIDER_ID)].map((m) => MADE_UP.test(m[2]))).toEqual([false]);
        expect([...fake.matchAll(PROVIDER_ID)].map((m) => MADE_UP.test(m[2]))).toEqual([true]);
    });

    it('every provider id in a tracked file is visibly made up', () => {
        const found: string[] = [];
        for (const file of files) {
            let text: string;
            try {
                text = readFileSync(path.join(MONOREPO_ROOT, file), 'utf8');
            } catch {
                continue; // a submodule or a file removed in the working tree
            }
            for (const m of text.matchAll(PROVIDER_ID)) {
                if (MADE_UP.test(m[2])) continue;
                // Report the place and the kind, never the id itself (CI logs are public too).
                found.push(`${file}: ${m[1]}_…${m[2].slice(-2)}`);
            }
        }
        expect(found).toEqual([]);
    });
});
