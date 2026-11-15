/**
 * CLASS GATE — every refusal a teacher can get from the parent-call routes has a translated label.
 *
 * Bug class: a refusal code whose only explanation is the route's English `error` prose. The
 * modal used to show `err.error` as the toast description, so a teacher using the Hindi or
 * Bengali UI got a translated "Call failed" over an English sentence. The instances were
 * PARENT_OPTED_OUT, SUPPRESSION_CHECK_FAILED and the rewritten OUTSIDE_CALLING_HOURS.
 *
 * Layers:
 *  1. DISCOVERY — every literal `code` the call and outreach routes can return (directly, or
 *     through call-suppression.ts and calling-hours.ts) has a label in CALL_REFUSAL_LABELS. The
 *     only non-literal codes allowed are the two provider-failure pass-throughs, listed below;
 *     a new computed code fails here.
 *  2. LOCALES — each label is a key in every src/locales/*.json, translated (not the English
 *     key again) and written in that language's script.
 *  3. MODAL — the call toast describes the error through callErrorDescription, which uses the
 *     label for a known code.
 */

import fs from 'fs';
import path from 'path';

import { CALL_REFUSAL_LABELS, callErrorDescription } from '@/lib/attendance/call-refusal-messages';
import { CallRequestError } from '@/lib/attendance/call-errors';
import { OUTSIDE_CALLING_HOURS_CODE } from '@/lib/calling-hours';
import { PARENT_OPTED_OUT_CODE } from '@/lib/call-suppression';

const SRC = path.resolve(__dirname, '../..');
const read = (rel: string) => fs.readFileSync(path.join(SRC, rel), 'utf8');

/** Where the codes a teacher can receive from these routes are written. */
const CODE_SOURCES = [
    'app/api/attendance/call/route.ts',
    'app/api/attendance/outreach/route.ts',
    'lib/call-suppression.ts',
    'lib/calling-hours.ts',
];

/** Computed codes: provider failures passed through from the provider's own classification. */
const COMPUTED_CODES_ALLOWED = [
    'result.failure.category.toUpperCase()', // Vobiz failure category (lib/vobiz/client.ts)
    'failure.code', // Twilio failure classification (lib/twilio-errors.ts)
];

function discoverCodes(): { literal: Set<string>; computed: string[] } {
    const literal = new Set<string>();
    const computed: string[] = [];
    for (const rel of CODE_SOURCES) {
        const src = read(rel);
        for (const m of src.matchAll(/\bcode:\s*([^,}\n]+)/g)) {
            const expr = m[1].trim().replace(/;$/, '');
            const lit = expr.match(/^['"]([A-Z][A-Z0-9_]*)['"]$/);
            if (lit) literal.add(lit[1]);
            else if (/^[A-Z][A-Z0-9_]*_CODE$/.test(expr) || /^typeof [A-Z_]+_CODE$/.test(expr)) continue; // a constant below
            else computed.push(`${rel}: ${expr}`);
        }
        for (const m of src.matchAll(/export const [A-Z][A-Z0-9_]*_CODE\s*=\s*['"]([A-Z][A-Z0-9_]*)['"]/g)) literal.add(m[1]);
    }
    return { literal, computed };
}

const SCRIPT: Record<string, RegExp> = {
    hindi: /[ऀ-ॿ]/g,
    marathi: /[ऀ-ॿ]/g,
    bengali: /[ঀ-৿]/g,
    punjabi: /[਀-੿]/g,
    gujarati: /[઀-૿]/g,
    odia: /[଀-୿]/g,
    tamil: /[஀-௿]/g,
    telugu: /[ఀ-౿]/g,
    kannada: /[ಀ-೿]/g,
    malayalam: /[ഀ-ൿ]/g,
};
const LOCALE_DIR = path.join(SRC, 'locales');
const LOCALES = fs.readdirSync(LOCALE_DIR).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, ''));

describe('every refusal code has a label', () => {
    const { literal, computed } = discoverCodes();

    it('finds the codes (guards against the scan matching nothing)', () => {
        for (const c of ['OUTSIDE_CALLING_HOURS', 'PARENT_OPTED_OUT', 'SUPPRESSION_CHECK_FAILED', 'PHONE_MISMATCH', 'INTERNAL']) {
            expect(literal).toContain(c);
        }
    });

    it('each literal code maps to a label', () => {
        expect([...literal].filter((c) => !CALL_REFUSAL_LABELS[c])).toEqual([]);
    });

    it('no code is computed except the provider-failure pass-throughs', () => {
        expect(computed.filter((c) => !COMPUTED_CODES_ALLOWED.some((a) => c.endsWith(`: ${a}`)))).toEqual([]);
    });

    it("the client module's literals match the server constants", () => {
        expect(CALL_REFUSAL_LABELS[OUTSIDE_CALLING_HOURS_CODE]).toBeDefined();
        expect(CALL_REFUSAL_LABELS[PARENT_OPTED_OUT_CODE]).toBeDefined();
    });
});

describe('every label is translated in every locale', () => {
    expect(LOCALES.length).toBeGreaterThanOrEqual(10);
    const labels = [...new Set(Object.values(CALL_REFUSAL_LABELS))];

    it.each(LOCALES)('%s', (locale) => {
        const dict = JSON.parse(fs.readFileSync(path.join(LOCALE_DIR, `${locale}.json`), 'utf8')) as Record<string, string>;
        const problems: string[] = [];
        for (const label of labels) {
            const value = dict[label];
            if (typeof value !== 'string' || !value.trim()) problems.push(`missing: ${label}`);
            else if (value === label) problems.push(`untranslated: ${label}`);
            else if ((value.match(SCRIPT[locale]) ?? []).length < 8) problems.push(`not in ${locale} script: ${label}`);
        }
        expect(problems).toEqual([]);
    });
});

describe('the modal shows the label, not the English error', () => {
    it('callErrorDescription uses the translated label for a known code and the message otherwise', () => {
        const t = (k: string) => `T(${k})`;
        const refused = new CallRequestError('English prose from the route', 'PARENT_OPTED_OUT');
        expect(callErrorDescription(refused, t)).toBe(`T(${CALL_REFUSAL_LABELS.PARENT_OPTED_OUT})`);
        expect(callErrorDescription(new CallRequestError('Provider said no', 'NETWORK'), t)).toBe('Provider said no');
        expect(callErrorDescription(new Error('plain'), t)).toBe('plain');
    });

    it('the call toast is described through callErrorDescription', () => {
        const modal = read('components/attendance/contact-parent-modal.tsx');
        expect(modal).toMatch(/from ["']@\/lib\/attendance\/call-refusal-messages["']/);
        expect(modal).toMatch(/toast\(\{ title: t\("Call failed"\), description: callErrorDescription\(err, t\)/);
        expect(modal).not.toMatch(/title: t\("Call failed"\), description: err\.message/);
    });
});
