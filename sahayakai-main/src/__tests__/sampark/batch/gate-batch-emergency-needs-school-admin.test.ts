/** @jest-environment node */
/**
 * Class gate: an emergency purpose sent to a whole class is a school decision. Only a school
 * admin may start it; class notices stay with the teacher who manages the class.
 *
 * The bug this catches: through class-wide batch calls any teacher assigned to a class could
 * send an emergency closure (catalogue D4: principal-approved, may ring from 06:00, may bypass
 * notice consent) to every family in the class, which the same teacher could not do through a
 * single Riya call. The rule follows the catalogue's `emergency` flag, so a future emergency
 * purpose is covered the moment it becomes batchable.
 */
import type { RiyaFacts } from '@/lib/sampark/conversation/facts';
import { BATCH_PURPOSES, BATCH_REASONS } from '@/lib/sampark/batch/reasons';
import { purposeSpec } from '@/lib/sampark/catalogue';
import { riyaPurposeSpec } from '@/lib/sampark/conversation/riya-purposes';

import { ADMIN, code, createFrom, HOLIDAY, setup, TEACHER_7A } from './_harness';

/** Facts for every batchable emergency purpose (add one here when a new one appears). */
const EMERGENCY_FACTS: Partial<Record<string, RiyaFacts>> = {
    emergency_closure: { purpose: 'emergency_closure', date: '2026-10-08', reason: 'rain_landslide', busesRunning: false },
};

const emergencyPurposes = BATCH_PURPOSES.filter((p) => purposeSpec(riyaPurposeSpec(p).samparkPurpose).emergency);
const reasonFor = (purpose: string) => BATCH_REASONS.find((r) => r.purpose === purpose)!.id;

describe('gate: emergency batches need a school admin', () => {
    it('covers the emergency closure at all, and every emergency purpose has facts here', () => {
        expect(emergencyPurposes).toContain('emergency_closure');
        expect(emergencyPurposes.filter((p) => !EMERGENCY_FACTS[p])).toEqual([]);
    });

    it.each(emergencyPurposes)('%s: a class teacher is refused, a school admin may start it', async (purpose) => {
        const t = await setup();
        const over = { reason: reasonFor(purpose) as never, facts: EMERGENCY_FACTS[purpose]! };
        expect(await code(createFrom(t, over, TEACHER_7A))).toBe('SCHOOL_ADMIN_REQUIRED');
        expect(await code(createFrom(t, over, ADMIN))).toBe('OK');
    });

    it('a class notice (holiday) stays with the teacher who manages the class', async () => {
        const t = await setup();
        expect(await code(createFrom(t, { facts: HOLIDAY }, TEACHER_7A))).toBe('OK');
    });
});
