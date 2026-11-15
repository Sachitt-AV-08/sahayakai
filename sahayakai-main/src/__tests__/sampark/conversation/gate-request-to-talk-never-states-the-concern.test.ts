/**
 * Class gate (founder decision, 23 Oct 2026; plan §2A): a concern call is a request to talk. It
 * never states the concern, on the call or to the calling provider.
 *
 * Before this gate the Riya academic and behaviour workflows sent ElevenLabs, and said on the
 * call, the concern itself ("Your child's recent marks in Mathematics have dropped…", "Your child
 * has been disturbing other students…"). The rule belongs to the catalogue purpose, not to one
 * workflow, so the gate walks every Riya workflow whose Sampark purpose is in
 * REQUEST_TO_TALK_PURPOSES, builds it for every concern, subject and observation staff can
 * select, and fails if any variable sent to the provider or the announcement shown to staff
 * carries a concern sentence, an observation phrase or a subject name. A new concern workflow
 * added later is covered the moment its purpose maps to a request-to-talk purpose.
 */
import { REQUEST_TO_TALK_PURPOSES } from '@/lib/sampark/catalogue';
import { buildRiyaVariables, type RiyaFacts } from '@/lib/sampark/conversation/facts';
import {
    ACADEMIC_CONCERNS,
    BEHAVIOUR_OBSERVATIONS,
    EXAM_SUBJECTS,
    RIYA_PURPOSES,
    riyaPurposeSpec,
    type AcademicConcern,
    type BehaviourObservation,
    type ExamSubject,
    type RiyaPurpose,
} from '@/lib/sampark/conversation/riya-purposes';

const NOW = new Date('2026-10-07T05:30:00Z'); // Wednesday 11:00 IST
const SCHOOL = { spokenName: { English: 'Hillview Demo School', Hindi: '', Bengali: '', Nepali: '' }, holidays: [] };
const STUDENT = { spokenFirstName: { English: 'Pema', Hindi: '', Bengali: '', Nepali: '' }, grade: 7, section: 'B' };

const SUBJECTS = Object.keys(EXAM_SUBJECTS) as ExamSubject[];
const CONCERNS = Object.keys(ACADEMIC_CONCERNS) as AcademicConcern[];
const OBSERVATIONS = Object.keys(BEHAVIOUR_OBSERVATIONS) as BehaviourObservation[];

/** Every fact set staff can choose for a concern workflow (one and two of each list). */
function factsFor(purpose: RiyaPurpose): RiyaFacts[] {
    if (purpose === 'academic_performance') {
        const subjectSets: ExamSubject[][] = [...SUBJECTS.map((s) => [s]), [SUBJECTS[0], SUBJECTS[1]]];
        return CONCERNS.flatMap((concern) => subjectSets.map((subjects) => ({ purpose, subjects, concern })));
    }
    if (purpose === 'classroom_behaviour') {
        return [...OBSERVATIONS.map((o) => [o]), OBSERVATIONS.slice(0, 2), OBSERVATIONS.slice(0, 3)].map((observations) => ({ purpose, observations }));
    }
    throw new Error(`no fact generator for request-to-talk workflow ${purpose}: add one here`);
}

/** Words that would state the concern: each concern sentence's fixed parts, each observation, each subject. */
const FORBIDDEN: string[] = [
    ...Object.values(ACADEMIC_CONCERNS).flatMap((s) => s.split('{subjects}').map((part) => part.trim()).filter((p) => p.length > 12)),
    ...Object.values(BEHAVIOUR_OBSERVATIONS),
    ...Object.values(EXAM_SUBJECTS),
];

/** Whole-word, case-insensitive: "Art" must not match "part". */
function wholePhrase(phrase: string): RegExp {
    return new RegExp(`\\b${phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i');
}

const concernWorkflows = RIYA_PURPOSES.filter((p) => REQUEST_TO_TALK_PURPOSES.has(riyaPurposeSpec(p).samparkPurpose));

describe('gate: a request-to-talk call never states the concern', () => {
    it('covers the concern workflows at all', () => {
        // A gate that checks nothing reports green forever.
        expect(concernWorkflows).toEqual(expect.arrayContaining(['academic_performance', 'classroom_behaviour']));
    });

    it.each(concernWorkflows)('%s sends and says no concern, observation or subject', (purpose) => {
        for (const facts of factsFor(purpose)) {
            const r = buildRiyaVariables({ purpose, facts, school: SCHOOL, student: STUDENT, now: NOW });
            if (!r.ok) throw new Error(`${purpose} refused ${JSON.stringify(facts)}: ${JSON.stringify(r.problems)}`);
            const said = [...Object.values(r.variables), r.announcement].join(' \n ');
            const leaked = FORBIDDEN.filter((phrase) => wholePhrase(phrase).test(said));
            expect({ facts, leaked }).toEqual({ facts, leaked: [] });
        }
    });
});
