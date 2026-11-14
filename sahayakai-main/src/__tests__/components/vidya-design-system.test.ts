/** @jest-environment node */
/**
 * VIDYA follows the design system (docs/design-system §5.12): saffron while idle, listening and
 * speaking; indigo only while connecting/thinking; the teacher home shows VidyaPresence at
 * size="workspace". Regression: the WebGL sphere turned leaf-green while speaking and the home
 * page drew that sphere instead of the design-system orb.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { VIDYA_SPHERE_PALETTES } from '@/components/vidya/vidya-sphere';

const read = (p: string) => readFileSync(join(process.cwd(), p), 'utf8');

describe('VIDYA colours and size follow the design system', () => {
    it('saffron for every state except thinking (indigo)', () => {
        expect(VIDYA_SPHERE_PALETTES).toEqual({ idle: 'saffron', listening: 'saffron', thinking: 'indigo', speaking: 'saffron', error: 'saffron' });
    });

    it('the sphere never uses the leaf (green) speaking palette', () => {
        const src = read('src/components/vidya/vidya-sphere.tsx');
        expect(src).not.toMatch(/--vidya-speaking-\d/);
        expect(src).not.toMatch(/var\(--orb-live-speaking\)/);
    });

    it('the teacher home renders VidyaPresence at size="workspace" for live voice', () => {
        const src = read('src/components/dashboard/dashboard-home.tsx');
        expect(src).toMatch(/<VidyaPresence\s+size="workspace"/);
    });
});
