/**
 * CLASS GATE — a concern call is a request to talk on every path that speaks to a parent.
 *
 * Founder decision, 23 Oct 2026: a call about a concern never states the concern. It says the
 * class teacher would like to speak with the parent and asks for a good time.
 *
 * Bug class: a route that speaks to a parent (or hands a voice pipeline what to speak) reads the
 * teacher's write-up, the reason, the subject or the marks straight off the outreach record, so
 * the concern is read out by a machine before the parent has spoken to a person. The instances
 * were the Twilio TwiML route (greeting + `spokenScript`, then an agent given the reason, the
 * message and the marks) and `/api/attendance/call-context` (returned `generatedMessage`,
 * `reason` and `subject` to the voice pipeline).
 *
 * Layers:
 *  1. BEHAVIOURAL — for every request-to-talk reason (each concern reason in the TS enum, a
 *     missing reason, an empty one, an unknown one), with a sentinel in every concern field,
 *     neither route's output contains a sentinel, the marks or the reason, and both serve the
 *     fixed line. A positive_feedback control proves the sentinels would be seen.
 *  2. LANGUAGES — the line exists, in the right script, for every language the routes speak.
 *  3. STRUCTURAL — discovered, not listed:
 *     - a route that reads `spokenScript`, `generatedMessage` or `performanceContext` must be on
 *       the teacher-facing allow-list (its output never reaches a parent); parent-facing routes
 *       read them only through parentCallContent, which applies the rule;
 *     - a route that speaks on a call (`<Say>`) or serves the voice pipeline (`x-internal-key`)
 *       must be a known parent-facing route or carry a stated exemption.
 */

import fs from 'fs';
import path from 'path';
import { NextResponse } from 'next/server';

import {
    CONCERN_REASONS,
    MESSAGE_DELIVERY_REASONS,
    REQUEST_TO_TALK_LINE,
    isRequestToTalk,
    requestToTalkLine,
    twilioSpokenLanguage,
} from '@/lib/attendance/request-to-talk';
import requestToTalkScript from '@/locales/call-scripts/request-to-talk.json';
import { TWILIO_LANGUAGE_MAP } from '@/types/attendance';
import type { Language } from '@/types';

const SRC = path.resolve(__dirname, '../..');
const rel = (abs: string) => path.relative(SRC, abs).split(path.sep).join('/');

// ── Fake Firestore (enough for the TwiML GET/POST and call-context) ─────────────────────────

type Data = Record<string, any>;
const store = new Map<string, Data>();

class DocRef {
    constructor(public readonly path: string) {}
    get firestore() { return db; }
    get id() { return this.path.split('/').pop()!; }
    collection(name: string) { return new CollRef(`${this.path}/${name}`); }
    async get() { return snap(this); }
    async set(data: Data) { store.set(this.path, { ...data }); }
    async update(patch: Data) { store.set(this.path, { ...(store.get(this.path) ?? {}), ...patch }); }
}
class CollRef {
    constructor(private readonly path: string) {}
    doc(id: string) { return new DocRef(`${this.path}/${id}`); }
}
function snap(ref: DocRef) {
    const data = store.get(ref.path);
    return { exists: data !== undefined, id: ref.id, ref, data: () => data };
}
const db: any = {
    collection: (name: string) => new CollRef(name),
    runTransaction: async (fn: (tx: any) => Promise<unknown>) =>
        fn({
            get: async (ref: DocRef) => snap(ref),
            set: (ref: DocRef, data: Data) => { store.set(ref.path, { ...data }); },
            update: (ref: DocRef, patch: Data) => { store.set(ref.path, { ...(store.get(ref.path) ?? {}), ...patch }); },
        }),
};

jest.mock('@/lib/firebase-admin', () => ({ getDb: async () => db }));
jest.mock('@/lib/twilio-validate', () => ({
    validateTwilioSignature: () => true,
    validateTwilioSignaturePost: () => true,
}));
jest.mock('@/ai/flows/parent-call-agent', () => ({ generateAgentReply: jest.fn() }));
jest.mock('@/lib/voice-pipeline/config', () => ({ getVoicePipelineConfig: () => ({ orchestratorWsUrl: '' }) }));
jest.mock('@/lib/logger', () => ({ logger: { info: jest.fn(), warn: jest.fn(), error: jest.fn() } }));

// The agent ECHOES everything it was given, so anything the route hands it is in the reply the
// parent hears. A concern field reaching the agent is therefore visible in the TwiML.
const agentInputs: unknown[] = [];
jest.mock('@/lib/sidecar/dispatch', () => ({
    dispatchParentCallReply: async (input: unknown) => {
        agentInputs.push(input);
        return { reply: JSON.stringify(input), shouldEndCall: false, followUpQuestion: null, source: 'genkit', decision: {} };
    },
}));

// Imported after the mocks.
import { GET as twimlGET, POST as twimlPOST } from '@/app/api/attendance/twiml/route';
import { GET as callContextGET } from '@/app/api/attendance/call-context/route';

const OUTREACH = 'parent_outreach/o1';
const MARKS = '13/97';

/** A record with a sentinel in every field that carries the concern. */
function concernRecord(reason: string | undefined, parentLanguage: Language = 'English'): Data {
    return {
        teacherUid: 't1',
        studentName: 'Aarav',
        className: 'Class 5',
        teacherName: 'Mrs Rao',
        schoolName: 'Hillview Demo School',
        parentLanguage,
        reason,
        generatedMessage: 'SENTINEL-generatedMessage: Aarav has been absent for five days.',
        spokenScript: 'SENTINEL-spokenScript: Aarav has been absent for five days.',
        subject: 'SENTINEL-subject',
        teacherNote: 'SENTINEL-teacherNote',
        reasonContext: 'SENTINEL-reasonContext',
        performanceContext: {
            subjectBreakdown: [{ subject: 'SENTINEL-perf-subject', name: 'SENTINEL-perf-name', marksObtained: 13, maxMarks: 97, percentage: 13 }],
            latestPercentage: 13.4,
            isAtRisk: true,
        },
        transcript: [],
        turnCount: 1,
    };
}

const unescapeXml = (xml: string) =>
    xml.replace(/&apos;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

function twilioRequest(form?: FormData) {
    return {
        url: 'https://app.test/api/attendance/twiml?outreachId=o1',
        headers: { get: () => null },
        formData: async () => form,
    } as any;
}

async function twimlGetText(): Promise<string> {
    return unescapeXml(await (await twimlGET(twilioRequest())).text());
}

async function twimlPostText(): Promise<string> {
    const form = new FormData();
    form.set('SpeechResult', 'haan ji, boliye');
    form.set('CallSid', 'CA-gate');
    return unescapeXml(await (await twimlPOST(twilioRequest(form))).text());
}

const jsonSpy = jest.spyOn(NextResponse, 'json');
async function callContext(): Promise<Record<string, unknown>> {
    process.env.VOICE_PIPELINE_INTERNAL_KEY = 'gate-key';
    const res = await callContextGET({
        url: 'https://app.test/api/attendance/call-context?outreachId=o1',
        headers: { get: (k: string) => (k === 'x-internal-key' ? 'gate-key' : null) },
    } as any);
    expect(res.status).toBe(200);
    const calls = jsonSpy.mock.calls;
    return calls[calls.length - 1][0] as Record<string, unknown>;
}

function tsOutreachReasons(): string[] {
    const src = fs.readFileSync(path.join(SRC, 'types/attendance.ts'), 'utf8');
    const union = src.match(/export type OutreachReason\s*=([^;]+);/);
    if (!union) throw new Error('OutreachReason not found in src/types/attendance.ts');
    return Array.from(union[1].matchAll(/'([^']+)'/g), (m) => m[1]);
}

const REQUEST_TO_TALK_REASONS: (string | undefined)[] = [
    ...tsOutreachReasons().filter((r) => !MESSAGE_DELIVERY_REASONS.has(r)),
    undefined,
    '',
    'a_reason_added_later',
];

function expectNoConcern(output: string, reason: string | undefined) {
    expect(output).not.toContain('SENTINEL');
    expect(output).not.toContain(MARKS);
    if (reason) {
        expect(output).not.toContain(reason);
        expect(output.toLowerCase()).not.toContain(reason.replace(/_/g, ' '));
    }
}

beforeEach(() => {
    store.clear();
    agentInputs.length = 0;
    jest.spyOn(console, 'error').mockImplementation(() => undefined);
    jest.spyOn(console, 'warn').mockImplementation(() => undefined);
});

// ── 1. Behavioural ──────────────────────────────────────────────────────────────────────────

describe('every outreach reason is classified', () => {
    it('the TS enum is exactly the concern reasons plus the message-delivery reasons', () => {
        const ts = tsOutreachReasons();
        expect(ts.length).toBeGreaterThan(0);
        expect(new Set(ts)).toEqual(new Set([...CONCERN_REASONS, ...MESSAGE_DELIVERY_REASONS]));
        for (const r of ['consecutive_absences', 'poor_performance', 'behavioral_concern']) {
            expect(CONCERN_REASONS.has(r)).toBe(true);
            expect(isRequestToTalk(r)).toBe(true);
        }
    });
});

describe.each(REQUEST_TO_TALK_REASONS.map((r) => [String(r), r] as const))('request to talk, reason=%s', (_label, reason) => {
    beforeEach(() => store.set(OUTREACH, concernRecord(reason)));

    it('TwiML greeting speaks the fixed line and no concern content', async () => {
        const xml = await twimlGetText();
        expectNoConcern(xml, reason);
        expect(xml).toContain(REQUEST_TO_TALK_LINE);
    });

    it('TwiML conversation turn gives the agent no concern content', async () => {
        const xml = await twimlPostText();
        expect(agentInputs).toHaveLength(1);
        expectNoConcern(xml, reason);
        expectNoConcern(JSON.stringify(agentInputs[0]), reason);
        expect(xml).toContain(REQUEST_TO_TALK_LINE);
    });

    it('call-context hands the voice pipeline the fixed line and no concern content', async () => {
        const body = await callContext();
        expectNoConcern(JSON.stringify(body), reason);
        expect(body.generatedMessage).toBe(REQUEST_TO_TALK_LINE);
        expect(body.reason).toBe('');
        expect(body.subject).toBe('');
        expect(body.requestToTalk).toBe(true);
    });
});

describe('control: a good-news call still delivers the teacher message', () => {
    // Proves the sentinels above would have been seen had they leaked.
    beforeEach(() => store.set(OUTREACH, concernRecord('positive_feedback')));

    it('TwiML greeting speaks the spoken script', async () => {
        expect(await twimlGetText()).toContain('SENTINEL-spokenScript');
    });

    it('TwiML agent gets the message, subject and marks', async () => {
        const xml = await twimlPostText();
        for (const s of ['SENTINEL-generatedMessage', 'SENTINEL-subject', MARKS]) expect(xml).toContain(s);
    });

    it('call-context returns the message', async () => {
        expect((await callContext()).generatedMessage).toContain('SENTINEL-generatedMessage');
    });
});

// ── 2. Languages ────────────────────────────────────────────────────────────────────────────

/** Letters of each language's own script (Devanagari danda allowed everywhere). */
const SCRIPT: Record<Language, RegExp> = {
    English: /^[A-Za-z\s.,?'’]+$/,
    Hindi: /^[ऀ-ॿ\s.,?‌‍]+$/,
    Marathi: /^[ऀ-ॿ\s.,?‌‍]+$/,
    Bengali: /^[ঀ-৿।॥\s.,?‌‍]+$/,
    Punjabi: /^[਀-੿।॥\s.,?‌‍]+$/,
    Gujarati: /^[઀-૿।॥\s.,?‌‍]+$/,
    Odia: /^[଀-୿।॥\s.,?‌‍]+$/,
    Tamil: /^[஀-௿\s.,?‌‍]+$/,
    Telugu: /^[ఀ-౿\s.,?‌‍]+$/,
    Kannada: /^[ಀ-೿\s.,?‌‍]+$/,
    Malayalam: /^[ഀ-ൿ\s.,?‌‍]+$/,
};
const LANGUAGES = Object.keys(TWILIO_LANGUAGE_MAP) as Language[];

describe('the line exists in every language the routes speak', () => {
    const lines = requestToTalkScript.lines as Record<string, string>;

    it.each(LANGUAGES)('%s has its own line in its own script', (language) => {
        const line = lines[language];
        expect(typeof line).toBe('string');
        expect(line).toMatch(SCRIPT[language]);
        if (language !== 'English') expect(line).not.toBe(REQUEST_TO_TALK_LINE);
    });

    it.each(LANGUAGES)('%s: TwiML speaks the line of the voice it uses, call-context the parent language', async (language) => {
        store.set(OUTREACH, concernRecord('consecutive_absences', language));
        expect(await twimlGetText()).toContain(requestToTalkLine(twilioSpokenLanguage(language)));
        expect((await callContext()).generatedMessage).toBe(requestToTalkLine(language));
    });

    it('Odia on Twilio follows its Hindi voice and Hindi call menu', () => {
        expect(twilioSpokenLanguage('Odia')).toBe('Hindi');
        expect(twilioSpokenLanguage('Bengali')).toBe('Bengali');
    });
});

// ── 3. Structural ───────────────────────────────────────────────────────────────────────────

function stripComments(src: string): string {
    return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
}
function routes(dir = path.join(SRC, 'app/api'), out: string[] = []): string[] {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) routes(p, out);
        else if (e.name === 'route.ts') out.push(p);
    }
    return out;
}
const ROUTES = routes().map((abs) => ({ rel: rel(abs), code: stripComments(fs.readFileSync(abs, 'utf8')) }));

/** Reads a concern field of an outreach record, directly or through the spoken-script helper. */
const READS_CONCERN_FIELD =
    /\.(spokenScript|generatedMessage|performanceContext)\b|\[\s*['"](spokenScript|generatedMessage|performanceContext)['"]\s*\]|\bresolveSpokenMessage\s*\(/;
/** Speaks on a call, or serves the voice pipeline. */
const FACES_A_CALL = /<Say\b|<Speak\b|x-internal-key/;

/** Routes that speak to a parent or hand a voice pipeline what to speak. Covered by layer 1. */
const PARENT_FACING = ['app/api/attendance/twiml/route.ts', 'app/api/attendance/call-context/route.ts'];

/** Routes that may read concern fields because nothing they produce reaches a parent. */
const TEACHER_FACING: Record<string, string> = {
    'app/api/ai/parent-message/route.ts': 'drafts the message for the teacher to review in the Contact-Parent modal',
    'app/api/attendance/outreach/route.ts': "stores the teacher's draft on the outreach record; returns only its id",
    'app/api/attendance/transcript-sync/route.ts': "passes the message to the post-call summary written for the teacher",
    'app/api/attendance/twiml-status/route.ts': "passes the message to the post-call summary written for the teacher",
};

/** Routes that face a call but speak nothing from an outreach record. */
const FACES_A_CALL_WITHOUT_OUTREACH_CONTENT: Record<string, string> = {
    'app/api/demo-call/twiml/route.ts': "public lead magnet: a fixed script to the visitor's own number, no outreach record",
    'app/api/attendance/transcript-sync/route.ts': 'receives transcripts from the voice pipeline and returns no speakable content',
};

describe('structural: concern fields reach a parent only through parentCallContent', () => {
    it('finds the routes (guards against the scan matching nothing)', () => {
        expect(ROUTES.length).toBeGreaterThan(20);
        for (const f of PARENT_FACING) expect(ROUTES.map((r) => r.rel)).toContain(f);
    });

    it('every route that reads spokenScript, generatedMessage or performanceContext is teacher-facing', () => {
        const offenders = ROUTES.filter((r) => READS_CONCERN_FIELD.test(r.code) && !TEACHER_FACING[r.rel]).map(
            (r) => `${r.rel}: reads a concern field but is not on the teacher-facing allow-list`,
        );
        expect(offenders).toEqual([]);
    });

    it('the allow-list is current: no parent-facing route on it, and each entry still reads a concern field', () => {
        for (const f of PARENT_FACING) expect(TEACHER_FACING[f]).toBeUndefined();
        for (const [f, why] of Object.entries(TEACHER_FACING)) {
            const route = ROUTES.find((r) => r.rel === f);
            expect({ f, exists: !!route, reads: !!route && READS_CONCERN_FIELD.test(route.code) }).toEqual({ f, exists: true, reads: true });
            expect(why.length).toBeGreaterThan(20);
        }
    });

    it.each(PARENT_FACING)('%s goes through parentCallContent and reads no subject or reason itself', (f) => {
        const code = ROUTES.find((r) => r.rel === f)!.code;
        expect(code).toMatch(/from\s+['"]@\/lib\/attendance\/request-to-talk['"]/);
        expect(code).toMatch(/\bparentCallContent\s*\(/);
        expect(code).not.toMatch(/\bdata\??\.(subject|reason|teacherNote|reasonContext)\b/);
    });

    it('every route that faces a call is parent-facing or names why it speaks no outreach content', () => {
        const offenders = ROUTES.filter(
            (r) => FACES_A_CALL.test(r.code) && !PARENT_FACING.includes(r.rel) && !FACES_A_CALL_WITHOUT_OUTREACH_CONTENT[r.rel],
        ).map((r) => `${r.rel}: speaks on a call or serves the voice pipeline but is not covered`);
        expect(offenders).toEqual([]);
    });
});
