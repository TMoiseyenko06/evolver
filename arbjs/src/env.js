/**
 * Minimal `.env` loader — the JS counterpart of `evolver/env.py`, and the reason
 * this package has no npm dependencies.
 *
 * Walks up from the current directory for a `.env`, reads `KEY=VALUE` lines into
 * `process.env`, and lets real environment variables win over the file.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

export function findDotenv(start = process.cwd()) {
    let dir = resolve(start);
    for (;;) {
        const candidate = join(dir, '.env');
        if (existsSync(candidate)) return candidate;
        const parent = dirname(dir);
        if (parent === dir) return null;
        dir = parent;
    }
}

export function loadDotenv({ path = null, override = false } = {}) {
    const file = path || findDotenv();
    if (!file || !existsSync(file)) return {};

    const loaded = {};
    for (const raw of readFileSync(file, 'utf8').split(/\r?\n/)) {
        let line = raw.trim();
        if (!line || line.startsWith('#')) continue;
        if (line.startsWith('export ')) line = line.slice('export '.length).trimStart();
        const eq = line.indexOf('=');
        if (eq < 0) continue;
        const key = line.slice(0, eq).trim();
        let value = line.slice(eq + 1).trim();
        if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === "'" || value[0] === '"')) {
            value = value.slice(1, -1);
        }
        if (!key) continue;
        if (override || process.env[key] === undefined) {
            process.env[key] = value;
            loaded[key] = value;
        }
    }
    return loaded;
}
