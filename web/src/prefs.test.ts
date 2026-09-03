import { beforeEach, describe, expect, it } from 'vitest';
import { deepMergePrefs, loadPrefs, PREFS_STORAGE_KEY } from './prefs';

describe('prefs migration and merge', () => {
  beforeEach(() => localStorage.clear());

  it('migrates scattered legacy keys into one prefs object', () => {
    localStorage.setItem('tab_order', JSON.stringify(['all', 'papers']));
    localStorage.setItem('tab_hidden', JSON.stringify(['nowcoder']));
    localStorage.setItem('gongkao_provinces', JSON.stringify(['山东']));
    localStorage.setItem('papers_only_priority', 'true');
    localStorage.setItem('theme', 'light');

    const prefs = loadPrefs();

    expect(prefs).toMatchObject({
      tab_order: ['all', 'papers'], tab_hidden: ['nowcoder'], gongkao_provinces: ['山东'],
      papers_only_priority: true, theme: 'light',
    });
    expect(localStorage.getItem('tab_order')).toBeNull();
    expect(JSON.parse(localStorage.getItem(PREFS_STORAGE_KEY) || '{}')).toEqual(prefs);
  });

  it('deep merges remote values over local while keeping local-only keys', () => {
    expect(deepMergePrefs(
      { theme: 'dark', filters: { province: '山东', type: '国考' }, local_only: true },
      { theme: 'light', filters: { province: '辽宁' } },
    )).toEqual({ theme: 'light', filters: { province: '辽宁', type: '国考' }, local_only: true });
  });
});
