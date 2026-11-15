/**
 * Class gate: no real person's mobile number is committed. The repository is public.
 *
 * The bug this catches: a local test roster with nine team members' own mobile numbers, and an
 * intern's own test phone in launchers, docs and server code, were committed to `develop` and
 * stayed public for two weeks. Real numbers belong in the operator's shell or in git-ignored
 * local files (scripts/sampark/sargvision-roster.local.json, RIYA_REAL_TEST_NUMBER).
 *
 * The gate scans every tracked text file in the app, the agents service and the mock CRM for an
 * Indian mobile number (+91 / 91 / bare ten digits starting 6-9, with or without spaces or
 * dashes). A number passes only if it is visibly made up (98000…, repeated digits, mostly zeros)
 * or is on the short allow-list below, each entry with its reason. Anything else fails here, on
 * the pull request that adds it.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const MONOREPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const SCANNED = ['sahayakai-main', 'sahayakai-agents', 'mock-school-crm'];
const BINARY = /\.(png|jpe?g|gif|webp|ico|svg|ulaw|wav|mp3|mp4|webm|pdf|woff2?|ttf|otf|zip|gz|lock)$|package-lock\.json$/i;

/** Numbers that may appear, each with the reason. Keep this list short. */
const ALLOWED: Record<string, string> = {
    '7965853645': "Riya's outbound caller ID on the company's Vobiz trunk (a company number)",
    '6363740720': "the founder's own number, used by consent as the Twilio verified caller ID and demo-call destination",
};

/** Visibly made-up: the repo's test ranges, one digit repeated, mostly zeros, or a counting run. */
function madeUp(n: string): boolean {
    if (/^(98000|98111|98222|98333|98444|98555|98123|98765|90000|80000|70000|60000|91234)/.test(n)) return true;
    if (/(\d)\1{6,}/.test(n)) return true;
    if ((n.match(/0/g) ?? []).length >= 6) return true;
    return n === '9876543210' || n === '9123456789';
}

const MOBILE = /(?<![\w+])(?:\+?91[ -]?)?([6-9]\d{4}[ -]?\d{5})(?![\w])/g;

function trackedTextFiles(): string[] {
    const out = execFileSync('git', ['ls-files', '-z', ...SCANNED], { cwd: MONOREPO_ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return out.split('\0').filter((f) => f && !BINARY.test(f));
}

describe('no real person’s mobile number is committed', () => {
    const files = trackedTextFiles();

    it('scans the tracked files at all', () => {
        // A gate that scans nothing reports green forever.
        expect(files.length).toBeGreaterThan(1000);
        expect(files).toContain('sahayakai-main/package.json');
    });

    it('every mobile number in a tracked file is made up or on the allow-list', () => {
        const found: string[] = [];
        for (const file of files) {
            let text: string;
            try {
                text = readFileSync(path.join(MONOREPO_ROOT, file), 'utf8');
            } catch {
                continue; // a submodule or a file removed in the working tree
            }
            for (const m of text.matchAll(MOBILE)) {
                const n = m[1].replace(/[ -]/g, '');
                if (madeUp(n) || ALLOWED[n]) continue;
                // Report the place, never the number itself (CI logs are public too).
                found.push(`${file}: …${n.slice(-2)}`);
            }
        }
        expect(found).toEqual([]);
    });
});
