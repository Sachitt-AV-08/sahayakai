/**
 * Class gate: no real person's mobile number is committed. The repository is public.
 *
 * The bug this catches: a local test roster with nine team members' own mobile numbers, and an
 * intern's own test phone in launchers, docs and server code, were committed to `develop` and
 * stayed public for two weeks. Real numbers belong in the operator's shell or in git-ignored
 * local files (scripts/sampark/sargvision-roster.local.json, RIYA_REAL_TEST_NUMBER).
 *
 * The first version of this gate had three holes, each closed here and pinned by a self-test:
 *  - it saw only ten digits in a row or split 5-5, so '+91 98 7654 3210', '+91 987 654 3210',
 *    '098765 43210', '%2B91…' and 'phone_98…' all passed (the repo's own runbook writes the
 *    caller ID 2-4-4);
 *  - it waved through whole allocatable series (98111…, 98765…, 80000…, any number with six
 *    zeros), about 1.3 million numbers that can belong to real people;
 *  - it scanned three directories of a public monorepo that tracks many more.
 *
 * Now it scans every tracked text file in the monorepo except the generated files listed below,
 * for an Indian mobile number in any common form: an optional +91 / 91 / 0091 / %2B91 / (+91) /
 * trunk 0 prefix, then ten digits starting 6-9 in up to four groups separated by a space, a
 * hyphen or a no-break space. A number passes only if it is visibly made up (the 98000 test range,
 * one digit seven times running, or a counting run of eight) or is on the short allow-list below,
 * each entry with its reason. Anything else fails here, on the pull request that adds it.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const MONOREPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

/** Binary formats, by extension: they hold no text a person could paste a number into. */
const BINARY = /\.(png|jpe?g|gif|webp|ico|icns|ulaw|wav|mp3|mp4|m4a|webm|pdf|woff2?|ttf|otf|zip|gz|jar|keystore|jks|xlsx|pptx|docx|skill)$/i;

/**
 * Generated files, each with its reason. Every one is produced from sources this gate does scan,
 * so a number can only reach them through a scanned file; their own digit runs are hashes.
 */
const GENERATED: Array<[RegExp, string]> = [
    [/(^|\/)package-lock\.json$|(^|\/)[^/]+\.lock$/, 'package-manager lockfiles: versions and integrity hashes, written by npm / pub'],
    [/\.g\.dart$/, 'Dart build_runner output, generated from the scanned .dart sources'],
    [/(^|\/)qa\/results\/playwright-report\//, 'the Playwright HTML report: a generated bundle of hashed assets'],
    [/(^|\/)public\/(sw|workbox-[^/]+)\.js$/, 'the next-pwa service worker build: lists of hashed chunk names'],
];

/** Numbers that may appear, each with the reason. Keep this list short. */
const ALLOWED: Record<string, string> = {
    '7965853645': "Riya's outbound caller ID on the company's Vobiz trunk (a company number)",
    '6363740720': "the founder's own number, used by consent as the Twilio verified caller ID and demo-call destination",
};

/**
 * Visibly made up: the repo's one test range (98000 xxxxx), one digit seven times running
 * (98111 11111), or a counting run of eight digits (98123 45678, 98765 43210).
 */
function madeUp(n: string): boolean {
    if (n.startsWith('98000')) return true;
    if (/(\d)\1{6}/.test(n)) return true;
    for (let i = 0; i + 8 <= n.length; i += 1) {
        const run = n.slice(i, i + 8);
        if ('0123456789'.includes(run) || '9876543210'.includes(run)) return true;
    }
    return false;
}

const SEP = '[ \\-\\u00a0]';
const MOBILE = new RegExp(
    // Not inside a longer number, a hex hash or a decimal; a '+' always starts the prefix.
    String.raw`(?<![\da-fA-F.+])` +
        String.raw`(?:(?:%2B|\+|00)?\(?\+?91\)?${SEP}{0,2}|0${SEP}?)?` +
        String.raw`([6-9](?:${SEP}?\d){9})` +
        String.raw`(?![\da-fA-F])`,
    'g',
);

/** The ten-digit mobile numbers written in `text`, in order. */
function mobileNumbers(text: string): string[] {
    const found: string[] = [];
    for (const m of text.matchAll(MOBILE)) {
        const groups = m[1].split(new RegExp(SEP));
        // A phone is written in at most four groups of two or more digits; more is SVG path data
        // or a table of digits.
        if (groups.length > 4 || groups.some((g) => g.length < 2)) continue;
        found.push(groups.join(''));
    }
    return found;
}

function tracked(): string[] {
    const out = execFileSync('git', ['ls-files', '-z'], { cwd: MONOREPO_ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return out.split('\0').filter(Boolean);
}

describe('no real person’s mobile number is committed', () => {
    const all = tracked();
    const files = all.filter((f) => !BINARY.test(f) && !GENERATED.some(([re]) => re.test(f)));

    it('scans every part of the monorepo', () => {
        // A gate that scans nothing, or a shrinking part, reports green forever.
        expect(files.length).toBeGreaterThan(4000);
        for (const dir of ['sahayakai-main/', 'sahayakai-agents/', 'mock-school-crm/', 'sahayakai-flutter/', 'sahayakai_mobile/', 'sahayakai-replica/', 'outputs/', 'docs/', '.github/', '.agent/']) {
            expect(files.some((f) => f.startsWith(dir))).toBe(true);
        }
        expect(files).toContain('CLAUDE.md');
        expect(files).toContain('sahayakai-main/package.json');
    });

    it('every exclusion is in use, so none outlives its reason', () => {
        for (const [re, reason] of GENERATED) {
            expect({ reason, excludes: all.some((f) => re.test(f)) }).toEqual({ reason, excludes: true });
        }
    });

    it('finds a number in every form the repo and its users write', () => {
        // Built at run time so this file holds no such number itself.
        const n = '94340' + '12345';
        const [a, b] = [n.slice(0, 5), n.slice(5)];
        const forms = [
            n,
            `+91${n}`,
            `91${n}`,
            `0${n}`,
            `0${a} ${b}`,
            `tel:0${n}`,
            `phone_${n}`,
            `To=%2B91${n}&From=x`,
            `(+91) ${a}-${b}`,
            `+91-${a}-${b}`,
            `+91 ${a} ${b}`,
            `+91 ${n.slice(0, 2)} ${n.slice(2, 6)} ${n.slice(6)}`,
            `+91 ${n.slice(0, 3)} ${n.slice(3, 6)} ${n.slice(6)}`,
            `+91 ${n.slice(0, 4)} ${n.slice(4, 7)} ${n.slice(7)}`,
            `+91 ${a} ${b}`,
            `0091 ${a} ${b}`,
        ];
        for (const form of forms) expect({ form, found: mobileNumbers(`x ${form} y`) }).toEqual({ form, found: [n] });
    });

    it('does not mistake hashes, decimals, timestamps or path data for a number', () => {
        const n = '94340' + '12345';
        expect(mobileNumbers(`9d${n}ab`)).toEqual([]); // inside a hex hash
        expect(mobileNumbers(`0.${n}`)).toEqual([]);
        expect(mobileNumbers(`12${n}`)).toEqual([]); // part of a longer number
        expect(mobileNumbers(`${n}000`)).toEqual([]);
        expect(mobileNumbers('<path d="M10 2a8 8 0 100 16 8 8 0 000-16z"/>')).toEqual([]);
    });

    it('treats only visibly made-up numbers as made up', () => {
        expect(['98000' + '12345', '98111' + '11111', '98123' + '45678', '98765' + '43210', '91234' + '56789'].map(madeUp)).toEqual([true, true, true, true, true]);
        // The old exempt series are ordinary numbers again.
        expect(['98111', '98765', '98123', '91234', '80000', '70000', '90000'].map((p) => madeUp(p + '13579'))).toEqual([false, false, false, false, false, false, false]);
        expect(madeUp('9' + '0'.repeat(6) + '135')).toBe(false); // six zeros is no longer enough
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
            for (const n of mobileNumbers(text)) {
                if (madeUp(n) || ALLOWED[n]) continue;
                // Report the place, never the number itself (CI logs are public too).
                found.push(`${file}: …${n.slice(-2)}`);
            }
        }
        expect(found).toEqual([]);
    });
});
