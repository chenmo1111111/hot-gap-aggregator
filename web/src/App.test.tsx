import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

const dataResponse = (url: string) => {
  if (url.endsWith('/data/all.json')) return json({ generated_at: '2026-09-03T00:00:00Z', sources: [], items: [] });
  if (url.endsWith('/data/server-gongkao.json')) return json({ generated_at: '', source: 'gongkao_official', status: { source: 'gongkao_official', status: 'not_run', item_count: 0 }, items: [] });
  if (url.endsWith('/data/ai.json')) return json({ generated_at: '', source: 'ai', status: { source: 'ai', status: 'ok', item_count: 1 }, items: [{ source: 'feed', rank: 1, title: '新的 AI 研究动态', title_zh: '新的 AI 研究动态', url: 'https://example.test/ai', summary_zh: '来自机器之心的摘要', published_at: '2026-09-03T00:00:00Z', extra: { tab: 'ai', feed_name: '机器之心' } }] });
  if (url.endsWith('/data/tools.json')) return json({ generated_at: '', source: 'tools', status: { source: 'tools', status: 'ok', item_count: 1 }, items: [{ source: 'feed', rank: 1, title: 'scanpy 1.12.0', title_zh: 'scanpy 1.12.0', url: 'https://example.test/tool', summary_zh: '性能优化', published_at: '2026-09-02T00:00:00Z', extra: { tab: 'tools', feed_name: 'scanpy 发版' } }] });
  if (url.endsWith('/data/papers.json')) return json({ generated_at: '', source: 'papers', status: { source: 'papers', status: 'ok', item_count: 3 }, items: [
    { source: 'papers', rank: 1, title: 'Rare cell clustering', title_zh: '稀有细胞聚类', url: 'https://example.test/preprint', published_at: '2026-09-03', extra: { tier: '预印本', subsource: 'biorxiv', topic_hit: ['稀有细胞'], keyword_hit: ['GNN'] } },
    { source: 'papers', rank: 2, title: '核心论文', title_zh: '核心论文', url: 'https://example.test/core', published_at: '2026-09-02', extra: { tier: '中文核心', subsource: 'crossref', journal: '计算机应用' } },
    { source: 'feed', rank: 3, title: 'Nature paper', title_zh: 'Nature 论文', url: 'https://example.test/nature', published_at: '2026-09-01', extra: { tab: 'papers', feed_name: 'Nature' } },
    { source: 'papers', rank: 4, title: 'BIB paper', title_zh: 'BIB 论文', url: 'https://example.test/bib', published_at: '2026-09-04', extra: { tier: '英文顶刊', subsource: 'crossref', journal: 'Briefings in bioinformatics', keyword_hit: ['benchmark'] } },
  ] });
  if (url.endsWith('/data/jobs.json')) return json({ generated_at: '', source: 'jobs', status: { source: 'jobs', status: 'ok', item_count: 0 }, items: [] });
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

  it('renders RSSHub AI and tool entries in their logical tabs', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'reader', is_admin: false });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('reader');
    fireEvent.click(screen.getByRole('button', { name: 'AI动态' }));
    expect(await screen.findByText('新的 AI 研究动态')).toBeInTheDocument();
    expect(screen.getByText('机器之心')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '工具更新' }));
    expect(await screen.findByText('scanpy 1.12.0')).toBeInTheDocument();
    expect(screen.getByText('性能优化')).toBeInTheDocument();
  });

  it('merges server-owned official notices with Fenbi without overwriting either side', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'reader', is_admin: false });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      if (url.endsWith('/data/all.json')) return json({
        generated_at: '2026-09-03T00:00:00Z',
        sources: [{ source: 'gongkao', status: 'degraded', item_count: 1, error: 'Fenbi temporarily unavailable' }],
        items: [{ source: 'gongkao', rank: 1, title: '粉笔时间线', title_zh: '粉笔时间线', url: 'https://fenbi.test/1', extra: { subsource: 'timeline', exam_type: '省考', province: '山东' } }],
      });
      if (url.endsWith('/data/server-gongkao.json')) return json({
        generated_at: '2026-09-04T00:00:00Z', source: 'gongkao_official',
        status: { source: 'gongkao_official', status: 'ok', item_count: 2 },
        items: [
          { source: 'gongkao', rank: 1, title: '中央机关公告', title_zh: '中央机关公告', url: 'https://scs.test/1', extra: { subsource: 'scs', exam_type: '国考', province: '全国' } },
          { source: 'gongkao', rank: 2, title: '黑龙江选调公告', title_zh: '黑龙江选调公告', url: 'https://xuandiao.test/1', extra: { subsource: 'xuandiao', exam_type: '选调生', province: '黑龙江' } },
        ],
      });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('reader');
    fireEvent.click(screen.getByRole('button', { name: '公考' }));
    expect(await screen.findByText('中央机关公告')).toBeInTheDocument();
    expect(screen.getByText('黑龙江选调公告')).toBeInTheDocument();
    expect(screen.getByText('粉笔时间线')).toBeInTheDocument();
    expect(screen.getByText('国家公务员局')).toBeInTheDocument();
    expect(screen.getByText('官方选调')).toBeInTheDocument();
  });

  it('renders papers in three expanded sections with source and match chips', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'reader', is_admin: false });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      if (url.endsWith('/data/all.json')) return json({
        generated_at: '2026-09-03T00:00:00Z',
        sources: [{ source: 'papers', status: 'ok', item_count: 2 }],
        items: [
          { source: 'papers', rank: 1, title: 'Rare cell clustering', title_zh: '稀有细胞聚类', url: 'https://example.test/preprint', published_at: '2026-09-03', extra: { tier: '预印本', subsource: 'biorxiv', topic_hit: ['稀有细胞'], keyword_hit: ['GNN'] } },
          { source: 'papers', rank: 2, title: '核心论文', title_zh: '核心论文', url: 'https://example.test/core', published_at: '2026-09-02', extra: { tier: '中文核心', subsource: 'crossref', journal: '计算机应用' } },
          { source: 'papers', rank: 3, title: 'BIB paper', title_zh: 'BIB 论文', url: 'https://example.test/bib', published_at: '2026-09-04', extra: { tier: '英文顶刊', subsource: 'crossref', journal: 'Briefings in bioinformatics', keyword_hit: ['benchmark'] } },
        ],
      });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('reader');
    fireEvent.click(screen.getByRole('button', { name: '顶刊' }));

    expect(await screen.findByRole('heading', { name: '英文顶刊' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '中文核心' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '预印本' })).toBeInTheDocument();
    expect(screen.getByText(/默认展开/)).toBeInTheDocument();
    expect(screen.getByText('bioRxiv')).toBeInTheDocument();
    expect(screen.getByText('稀有细胞')).toBeInTheDocument();
    expect(screen.getByText('GNN')).toBeInTheDocument();
    expect(screen.getByText('Nature 论文')).toBeInTheDocument();
    expect(screen.getByText('期刊 · Briefings in Bioinformatics (BIB)')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('期刊筛选'), { target: { value: 'Briefings in bioinformatics' } });
    expect(screen.getByText('BIB 论文')).toBeInTheDocument();
    expect(screen.queryByText('核心论文')).not.toBeInTheDocument();
    expect(screen.queryByText('稀有细胞聚类')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('期刊筛选'), { target: { value: 'all' } });
    fireEvent.click(screen.getByRole('button', { name: /^中文核心 1$/ }));
    expect(screen.getByText('核心论文')).toBeInTheDocument();
    expect(screen.queryByText('BIB 论文')).not.toBeInTheDocument();
    expect(screen.queryByText('稀有细胞聚类')).not.toBeInTheDocument();
  });

  it('keeps hot-list items before utility sources in the all tab', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'reader', is_admin: false });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      if (url.endsWith('/data/all.json')) return json({
        generated_at: '2026-09-04T00:00:00Z',
        sources: [{ source: 'weibo', status: 'ok', item_count: 1 }, { source: 'gongkao', status: 'ok', item_count: 1 }],
        items: [
          { source: 'weibo', rank: 1, title: '微博第一热搜', title_zh: '微博第一热搜', url: 'https://example.test/hot' },
          { source: 'gongkao', rank: 1, title: '粉笔考试公告', title_zh: '粉笔考试公告', url: 'https://example.test/gongkao', extra: { subsource: 'fenbi' } },
        ],
      });
      if (url.endsWith('/data/server-gongkao.json')) return json({
        generated_at: '2026-09-04T00:00:00Z', source: 'gongkao_official',
        status: { source: 'gongkao_official', status: 'ok', item_count: 1 },
        items: [{ source: 'gongkao', rank: 1, title: '官方选调公告', title_zh: '官方选调公告', url: 'https://example.test/official', extra: { subsource: 'xuandiao' } }],
      });
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('reader');
    const text = document.body.textContent || '';
    expect(text.indexOf('微博第一热搜')).toBeLessThan(text.indexOf('官方选调公告'));
    expect(text.indexOf('官方选调公告')).toBeLessThan(text.indexOf('粉笔考试公告'));
  });

  it('refreshes data when the installed app returns to the foreground', async () => {
    let now = 1_000;
    let allRequests = 0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json({ username: 'reader', is_admin: false });
      if (url === '/api/settings') return json({ prefs: {}, updated_at: null });
      if (url.endsWith('/data/all.json')) {
        allRequests += 1;
        return json({
          generated_at: `2026-09-05T0${allRequests}:00:00Z`,
          sources: [{ source: 'weibo', status: 'ok', item_count: 1 }],
          items: [{ source: 'weibo', rank: 1, title: `前台刷新版本 ${allRequests}`, title_zh: `前台刷新版本 ${allRequests}`, url: `https://example.test/version-${allRequests}` }],
        });
      }
      return dataResponse(url) ?? json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    expect(await screen.findByText('前台刷新版本 1')).toBeInTheDocument();
    now += 2 * 60 * 1000;
    window.dispatchEvent(new Event('focus'));
    expect(await screen.findByText('前台刷新版本 2')).toBeInTheDocument();
    expect(screen.queryByText('前台刷新版本 1')).not.toBeInTheDocument();
  });
});
