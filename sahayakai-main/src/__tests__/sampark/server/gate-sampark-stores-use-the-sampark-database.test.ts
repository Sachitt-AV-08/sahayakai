/**
 * Class gate (Sampark class gate 5, extended): Sampark data is stored only in the Sampark
 * database, behind assertSamparkEnvironmentSafe.
 *
 * The bug this catches: the Google Calendar store opened the app's default database with
 * getDb(), so on a staging service or a developer machine (SAMPARK_FIRESTORE_DATABASE =
 * sampark-nonprod, or no emulator) schools' encrypted OAuth tokens, OAuth state and synced
 * events landed in the production default database while every other Sampark store went to the
 * Sampark database.
 *
 * The gate is on the class: every Sampark server or library module that opens Firestore must
 * either go through the Sampark database (samparkFirestore, getSamparkRepo, or the guarded
 * factories) or be declared below as reading the app's own records, with the reason.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const APP_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

/** Modules that read the APP's own collections from the default database, not Sampark data. */
const APP_DATA_READERS: Record<string, string> = {
    'src/server/sampark/auth.ts': 'organizations and members: who is a school admin or teacher',
    'src/server/sampark/calendar.ts': "users: staff display names on the school calendar",
};

const OPENS_FIRESTORE = /getFirestore\(|\bgetDb\b|samparkFirestore\(/;
const GUARDED = /assertSamparkEnvironmentSafe\(|samparkFirestore\(|getSamparkRepo\(/;

function samparkModules(): string[] {
    const out = execFileSync('git', ['ls-files', 'src/lib/sampark', 'src/server/sampark'], { cwd: APP_ROOT, encoding: 'utf8' });
    return out.split('\n').filter((f) => /\.tsx?$/.test(f) && !f.includes('__tests__'));
}

describe('gate: Sampark stores use the Sampark database', () => {
    const openers = samparkModules().filter((f) => OPENS_FIRESTORE.test(readFileSync(path.join(APP_ROOT, f), 'utf8')));

    it('finds the modules that open Firestore at all', () => {
        expect(openers).toEqual(expect.arrayContaining(['src/lib/sampark/repo/factory.ts', 'src/server/sampark/google-calendar.ts']));
    });

    it('every module that opens Firestore is guarded or a declared reader of app data', () => {
        const unguarded = openers.filter((f) => !(f in APP_DATA_READERS) && !GUARDED.test(readFileSync(path.join(APP_ROOT, f), 'utf8')));
        expect(unguarded).toEqual([]);
    });
});
