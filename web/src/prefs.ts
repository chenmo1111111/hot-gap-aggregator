export type Prefs = Record<string, unknown>;

export const PREFS_STORAGE_KEY = 'prefs';

const objectValue = (value: unknown): Prefs => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as Prefs
  : {};

const parseJson = (value: string | null): unknown => {
  if (value === null) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
};

export const deepMergePrefs = (local: Prefs, remote: Prefs): Prefs => {
  const merged: Prefs = { ...local };
  for (const [key, value] of Object.entries(remote)) {
    const localValue = merged[key];
    merged[key] = value !== null && typeof value === 'object' && !Array.isArray(value)
      && localValue !== null && typeof localValue === 'object' && !Array.isArray(localValue)
      ? deepMergePrefs(localValue as Prefs, value as Prefs)
      : value;
  }
  return merged;
};

export const savePrefs = (prefs: Prefs, storage: Storage = localStorage) => {
  storage.setItem(PREFS_STORAGE_KEY, JSON.stringify(prefs));
};

export const loadPrefs = (storage: Storage = localStorage): Prefs => {
  const prefs = objectValue(parseJson(storage.getItem(PREFS_STORAGE_KEY)));
  const legacyJsonKeys = ['tab_order', 'tab_hidden', 'gongkao_provinces', 'qiuzhao_filters'];
  for (const key of legacyJsonKeys) {
    if (!(key in prefs)) {
      const value = parseJson(storage.getItem(key));
      if (value !== undefined) prefs[key] = value;
    }
  }
  if (!("papers_only_priority" in prefs)) {
    const oldValue = storage.getItem('papers_only_priority') ?? storage.getItem('papers_only_my_topics');
    if (oldValue !== null) prefs.papers_only_priority = oldValue === 'true';
  }
  if (!("theme" in prefs) && storage.getItem('theme')) prefs.theme = storage.getItem('theme');
  if (!("alerts_last_seen" in prefs) && storage.getItem('alerts_last_seen')) {
    prefs.alerts_last_seen = storage.getItem('alerts_last_seen');
  }
  savePrefs(prefs, storage);
  for (const key of [...legacyJsonKeys, 'papers_only_priority', 'papers_only_my_topics', 'theme', 'alerts_last_seen']) {
    storage.removeItem(key);
  }
  return prefs;
};

export const prefString = (prefs: Prefs, key: string, fallback = '') => typeof prefs[key] === 'string'
  ? prefs[key] as string
  : fallback;

export const prefBoolean = (prefs: Prefs, key: string, fallback = false) => typeof prefs[key] === 'boolean'
  ? prefs[key] as boolean
  : fallback;

export const prefStringArray = (prefs: Prefs, key: string): string[] => Array.isArray(prefs[key])
  ? (prefs[key] as unknown[]).filter((value): value is string => typeof value === 'string')
  : [];
