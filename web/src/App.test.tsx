import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

const dataResponse = (url: string) => {
  if (url.endsWith('/data/all.json')) return json({ generated_at: '2026-09-03T00:00:00Z', sources: [], items: [] });
  if (url.endsWith('/data/trends.json')) return json({ generated_at: '', rising: [], new_today: [], dropped: [], longest_on_board: [] });
  if (url.endsWith('/data/gongkao_official_sites.json')) return json({ sites: [] });
  if (url.endsWith('/data/alerts.json')) return json({ generated_at: '', items: [] });
  if (url.endsWith('/data/job_quicklinks.json')) return json({ items: [] });
  return null;
};

describe('authenticated app bootstrap', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it('shows only the login page and never loads data when unauthenticated', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ detail: '未登录' }, 401);
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    expect(await screen.findByRole('heading', { name: '信息差日报' })).toBeInTheDocument();
    expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/data/'))).toBe(false);
  });

  it('logs in, then loads protected feeds and hides admin tools for a normal user', async () => {
    let authenticated = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/login') { authenticated = true; return json({ username: 'tester', is_admin: false }); }
      if (url === '/api/me') return authenticated ? json({ username: 'tester', is_admin: false }) : json({}, 401);
      if (url === '/api/settings') return json({ prefs: { theme: 'light' }, updated_at: '2026-09-03T00:00:00Z' });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByLabelText('用户名');
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'tester' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    expect(await screen.findByText('tester')).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === '/data/all.json')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: '调整导航标签' }));
    expect(screen.queryByText('用户管理')).not.toBeInTheDocument();
  });

  it('shows the user administration section only to admins', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'admin', is_admin: true });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      if (url === '/api/admin/users') return json({ users: [{ id: 1, username: 'admin', is_admin: true, created_at: '2026-09-03T00:00:00Z' }] });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('admin · 管理员');
    fireEvent.click(screen.getByRole('button', { name: '调整导航标签' }));
    expect(await screen.findByText('用户管理')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建账号' })).toBeInTheDocument();
  });
});
