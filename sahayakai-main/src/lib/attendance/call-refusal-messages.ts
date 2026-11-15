/**
 * What a teacher reads when a parent call is refused, in the teacher's own language.
 *
 * The call and outreach routes answer a refusal with `{ error, code }`. `error` is English
 * prose for logs and API callers; the modal shows the translated label for `code` instead, so a
 * teacher using the Hindi or Bengali UI is not handed an English explanation under a translated
 * title. Labels are English keys translated in src/locales/*.json
 * (gate: src/__tests__/attendance/gate-call-refusal-labels.test.ts).
 *
 * Client-safe: no server imports. The codes are literals here and the gate checks they match the
 * server's constants.
 */

import { ROUTINE_END_CEILING_HOUR, ROUTINE_START_FLOOR_HOUR } from '@/lib/sampark/policy/routine-hours';

const hourLabel = (hour: number) => `${String(hour).padStart(2, '0')}:00`;

/** Refusal code -> locale key. */
export const CALL_REFUSAL_LABELS: Readonly<Record<string, string>> = {
    OUTSIDE_CALLING_HOURS:
        `Calls to parents are only placed between ${hourLabel(ROUTINE_START_FLOOR_HOUR)} and ` +
        `${hourLabel(ROUTINE_END_CEILING_HOUR)} IST. Please try again during calling hours.`,
    PARENT_OPTED_OUT:
        'This parent asked not to be called again, so we have not placed the call. ' +
        'Please reach them another way, such as a WhatsApp message or a note home.',
    SUPPRESSION_CHECK_FAILED: "We could not check this parent's call preferences just now. Please try again in a minute.",
    PHONE_MISMATCH: "The parent's phone on this outreach no longer matches the student record. Start a new outreach.",
    INTERNAL: 'Something went wrong',
};

/** The toast text for a failed call: the translated label when the route sent a known code. */
export function callErrorDescription(err: unknown, t: (key: string) => string): string {
    const code = (err as { code?: unknown } | null)?.code;
    const label = typeof code === 'string' ? CALL_REFUSAL_LABELS[code] : undefined;
    if (label) return t(label);
    return err instanceof Error ? err.message : String(err);
}
