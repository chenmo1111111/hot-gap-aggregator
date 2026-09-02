import { useEffect, useMemo, useState } from 'react';

type HistoryPoint = { run_at: string; rank: number };
type Item = {
  source: string; rank: number; title: string; title_zh: string; url: string;
  hot_value?: string | null; summary_zh?: string | null; thumbnail?: string | null;
  published_at?: string | null;
  days_on_board?: number; is_new?: boolean; rank_delta?: number | 'new';
  cluster_id?: string | null; cluster_size?: number; rank_history?: HistoryPoint[];
  extra?: Record<string, unknown>;
};
type SourceState = { source: string; status: string; item_count: number; error?: string | null; duration_ms?: number };
type Feed = { generated_at: string; sources?: SourceState[]; items: Item[] };
type Trends = { generated_at: string; rising: Item[]; new_today: Item[]; dropped: Item[]; longest_on_board: Item[] };
type OfficialSite = { province: string; name: string; url: string };

const sourceNames: Record<string, string> = {
  weibo: '微博', bilibili: 'B站', github: 'GitHub', youtube: 'YouTube', douyin: '抖音',
  telegram: 'Telegram', gongkao: '公考', xiaohongshu: '小红书雷达', papers: '顶刊',
  nowcoder: '牛客', qiuzhao: '秋招',
};
const itemSourceName = (source: string) => source === 'conf_deadlines' ? '会议 Deadline' : sourceNames[source] ?? source;

const defaultTabs = ['all', ...Object.keys(sourceNames), 'trends'];
const tabLabel = (tab: string) => tab === 'all' ? '全部' : tab === 'trends' ? '趋势' : sourceNames[tab] ?? tab;

const readStoredTabs = (key: 'tab_order' | 'tab_hidden') => {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(value) ? value.filter((tab): tab is string => typeof tab === 'string') : [];
  } catch {
    return [];
  }
};

const loadTabOrder = () => {
  const allowed = new Set(defaultTabs.slice(1));
  const stored = readStoredTabs('tab_order').filter((tab) => allowed.has(tab));
  const ordered = [...new Set(stored)];
  return ['all', ...ordered, ...defaultTabs.slice(1).filter((tab) => !ordered.includes(tab))];
};

const loadHiddenTabs = () => {
  const allowed = new Set(defaultTabs.slice(1));
  return [...new Set(readStoredTabs('tab_hidden').filter((tab) => allowed.has(tab)))];
};

const openItem = (item: Item) => window.open(item.url, '_blank', 'noopener,noreferrer');
const day = (value: unknown) => {
  if (!value) return null;
  const raw = typeof value === 'number' || /^\d+$/.test(String(value))
    ? new Date(Number(value) > 10_000_000_000 ? Number(value) : Number(value) * 1000)
    : new Date(String(value));
  return Number.isNaN(raw.getTime()) ? null : raw;
};
const md = (value: unknown) => {
  const date = day(value);
  return date ? `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` : '待定';
};
const daysFromNow = (value: unknown) => {
  const target = day(value);
  if (!target) return null;
  const now = new Date();
  now.setHours(0, 0, 0, 0);
  target.setHours(0, 0, 0, 0);
  return Math.round((target.getTime() - now.getTime()) / 86400000);
};

function Sparkline({ history = [] }: { history?: HistoryPoint[] }) {
  if (history.length < 2) return <span className="text-[10px] opacity-50">暂无走势</span>;
  const ranks = history.map((point) => point.rank);
  const max = Math.max(...ranks, 2);
  const points = history.map((point, index) => ({
    x: 3 + (index * 68) / Math.max(history.length - 1, 1),
    y: 4 + ((point.rank - 1) / Math.max(max - 1, 1)) * 24,
    rank: point.rank,
  }));
  return <svg viewBox="0 0 74 32" className="h-8 w-[74px] overflow-visible" aria-label="近七天排名走势">
    <path d="M3 28H71" stroke="currentColor" strokeOpacity=".12" />
    {points.slice(1).map((point, index) => {
      const before = points[index];
      const width = 1.2 + 2.5 * (1 - ((before.rank + point.rank) / 2 - 1) / Math.max(max - 1, 1));
      return <line key={index} x1={before.x} y1={before.y} x2={point.x} y2={point.y} stroke="currentColor" strokeWidth={width} strokeLinecap="round" />;
    })}
    <circle cx={points.at(-1)?.x} cy={points.at(-1)?.y} r="2.2" fill="currentColor" />
  </svg>;
}

function HotCard({ item, highlight, onCluster }: { item: Item; highlight?: boolean; onCluster?: (event: React.MouseEvent, item: Item) => void }) {
  const paper = item.source === 'papers';
  const topicHits = Array.isArray(item.extra?.topic_hit) ? item.extra.topic_hit.map(String) : [];
  const keywordHits = Array.isArray(item.extra?.keyword_hit) ? item.extra.keyword_hit.map(String) : [];
  const paperHighlighted = paper && (topicHits.length > 0 || keywordHits.length > 0);
  return <article role="link" tabIndex={0} onClick={() => openItem(item)} onKeyDown={(event) => event.key === 'Enter' && openItem(item)}
    className={`group grid cursor-pointer grid-cols-[42px_1fr] gap-3 rounded-2xl border bg-[var(--card)] p-4 shadow-sm transition focus:outline-none focus:ring-2 focus:ring-cyan-400 sm:grid-cols-[58px_1fr_auto] sm:gap-5 sm:p-5 ${highlight === false ? 'border-transparent opacity-30' : 'border-[var(--line)] hover:-translate-y-0.5 hover:border-cyan-400'} ${highlight ? 'ring-2 ring-orange-400' : ''} ${paperHighlighted ? 'border-l-4 border-l-amber-400' : ''}`}>
    <div className="font-mono text-2xl font-bold text-[var(--rank)]">{String(item.rank).padStart(2, '0')}</div>
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] font-bold">
        <span className="rounded bg-[var(--soft)] px-2 py-1">{itemSourceName(item.source)}</span>
        {paperHighlighted && <span className="rounded bg-amber-100 px-2 py-1 text-amber-900">🔖 方向命中</span>}
        {item.is_new && <span className="rounded bg-lime-300 px-2 py-1 text-slate-950">新</span>}
        {(item.cluster_size ?? 0) >= 2 && <button className="rounded bg-orange-100 px-2 py-1 text-orange-700" onClick={(event) => onCluster?.(event, item)}>🔥 全网 {item.cluster_size} 平台</button>}
      </div>
      <h2 className="text-lg font-extrabold leading-snug tracking-tight group-hover:text-cyan-600 sm:text-xl">{item.title_zh || item.title}</h2>
      {item.title_zh && item.title_zh !== item.title && <details className="mt-1 text-xs text-[var(--muted)]" onClick={(event) => event.stopPropagation()}><summary className="cursor-pointer truncate">原标题 · {item.title}</summary></details>}
      {item.summary_zh && <p className={`mt-2 text-sm leading-6 text-[var(--muted)] ${paper ? 'line-clamp-3' : 'line-clamp-2'}`}>{item.summary_zh}</p>}
      {paper && <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted)]"><span>{String(item.extra?.journal || item.extra?.field || '论文')} · {item.published_at?.slice(0, 10) || '日期待定'}</span>{topicHits.map((topic) => <span key={topic} className="rounded-full bg-amber-200 px-2 py-0.5 font-bold text-amber-900">{topic}</span>)}{keywordHits.map((keyword) => <span key={keyword} className="rounded-full bg-[var(--soft)] px-2 py-0.5">{keyword}</span>)}</div>}
    </div>
    <div className="col-start-2 flex items-center justify-between gap-4 text-xs text-[var(--muted)] sm:col-start-auto sm:flex-col sm:items-end">
      <span>{item.hot_value ? item.source === 'conf_deadlines' ? item.hot_value : `热度 ${item.hot_value}` : '查看原文 ↗'}</span>
      <div className="flex items-center gap-3 text-cyan-600"><Sparkline history={item.rank_history} /><span>{typeof item.rank_delta === 'number' && item.rank_delta !== 0 ? (item.rank_delta > 0 ? `↑${item.rank_delta}` : `↓${Math.abs(item.rank_delta)}`) : ''}</span></div>
    </div>
  </article>;
}

function TrendView({ trends }: { trends: Trends | null }) {
  const sections: Array<[keyof Omit<Trends, 'generated_at'>, string, string]> = [
    ['rising', '上升最快', '排名跃升幅度最大'], ['new_today', '今日新晋', '今天首次进入榜单'],
    ['dropped', '跌出榜单', '昨日在榜，今日离场'], ['longest_on_board', '霸榜王', '连续在榜时间最长'],
  ];
  return <div className="grid gap-8 lg:grid-cols-2">
    {sections.map(([key, title, subtitle]) => <section key={key}>
      <div className="mb-3 flex items-end justify-between"><div><h2 className="text-xl font-black">{title}</h2><p className="text-xs text-[var(--muted)]">{subtitle}</p></div><span className="font-mono text-xs text-[var(--muted)]">{trends?.[key].length ?? 0}</span></div>
      <div className="grid gap-2">{(trends?.[key] ?? []).map((item) => <button key={`${item.source}-${item.url}`} onClick={() => openItem(item)} className="grid grid-cols-[32px_1fr_auto] items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--card)] p-3 text-left hover:border-cyan-400">
        <span className="font-mono text-sm text-[var(--rank)]">{String(item.rank).padStart(2, '0')}</span><span className="min-w-0"><b className="block truncate text-sm">{item.title_zh || item.title}</b><small className="text-[var(--muted)]">{itemSourceName(item.source)}{key === 'longest_on_board' ? ` · ${item.days_on_board ?? 1} 天` : ''}</small></span><Sparkline history={item.rank_history} />
      </button>)}</div>
    </section>)}
  </div>;
}

function XhsView({ items }: { items: Item[] }) {
  const groups = items.reduce<Record<string, Item[]>>((result, item) => {
    const keyword = String(item.extra?.keyword || '未分组');
    (result[keyword] ??= []).push(item);
    return result;
  }, {});
  return <div className="grid gap-8"><div className="rounded-2xl border border-pink-200 bg-pink-50 p-4 text-sm text-pink-900 dark:border-pink-950 dark:bg-pink-950/30 dark:text-pink-200"><b>关键词雷达 · 非官方热榜</b><span className="ml-2 opacity-75">按配置词抓取搜索页并按点赞数排序，不代表小红书全站排名。</span></div>
    {Object.entries(groups).map(([keyword, entries]) => <section key={keyword}><h2 className="mb-3 text-xl font-black">#{keyword}</h2><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{entries?.map((item) => <button key={item.url} onClick={() => openItem(item)} className="overflow-hidden rounded-2xl border border-[var(--line)] bg-[var(--card)] text-left transition hover:-translate-y-0.5 hover:border-pink-400">
      {item.thumbnail && <img src={item.thumbnail} alt="" className="aspect-[4/3] w-full object-cover" loading="lazy" referrerPolicy="no-referrer" />}
      <span className="block p-4"><b className="line-clamp-2">{item.title_zh || item.title}</b><small className="mt-2 flex justify-between text-[var(--muted)]"><span>{String(item.extra?.author || '未知作者')}</span><span>♥ {String(item.extra?.likes || item.hot_value || '0')}</span></small></span>
    </button>)}</div></section>)}
  </div>;
}

function GongkaoView({ items, sites }: { items: Item[]; sites: OfficialSite[] }) {
  const allProvinces = useMemo(() => Array.from(new Set(items.map((item) => String(item.extra?.province || '全国')))).sort(), [items]);
  const allTypes = useMemo(() => {
    const found = Array.from(new Set(items.map((item) => String(item.extra?.exam_type || '其他')))).sort();
    return ['国考', '选调生', ...found.filter((type) => type !== '国考' && type !== '选调生')];
  }, [items]);
  const [provinces, setProvinces] = useState<string[]>(() => JSON.parse(localStorage.getItem('gongkao_provinces') || '[]'));
  const [examType, setExamType] = useState('all');
  useEffect(() => localStorage.setItem('gongkao_provinces', JSON.stringify(provinces)), [provinces]);
  const filtered = items.filter((item) => (!provinces.length || provinces.includes(String(item.extra?.province || '全国'))) && (examType === 'all' || item.extra?.exam_type === examType));
  const groups: Record<string, Item[]> = { '报名进行中': [], '即将报名（7天）': [], '即将笔试（14天）': [], '近期公告': [] };
  for (const item of filtered) {
    const start = daysFromNow(item.extra?.startSignUpTime), end = daysFromNow(item.extra?.endSignUpTime), write = daysFromNow(item.extra?.startWriteTime);
    const group = start !== null && start <= 0 && end !== null && end >= 0 ? '报名进行中' : start !== null && start > 0 && start <= 7 ? '即将报名（7天）' : write !== null && write >= 0 && write <= 14 ? '即将笔试（14天）' : '近期公告';
    groups[group].push(item);
  }
  const toggle = (province: string) => setProvinces((current) => current.includes(province) ? current.filter((value) => value !== province) : [...current, province]);
  return <div className="grid gap-8">
    <section className="rounded-2xl border border-[var(--line)] bg-[var(--card)] p-4"><div className="mb-3 flex flex-wrap items-center gap-2"><b className="mr-2 text-sm">省份多选</b>{allProvinces.map((province) => <button key={province} onClick={() => toggle(province)} className={`rounded-full px-3 py-1.5 text-xs font-bold ${provinces.includes(province) ? 'bg-cyan-500 text-white' : 'bg-[var(--soft)]'}`}>{province}</button>)}{provinces.length > 0 && <button onClick={() => setProvinces([])} className="text-xs text-cyan-600">清空</button>}</div><div className="flex flex-wrap items-center gap-2"><b className="mr-2 text-sm">考试类型</b>{['国考', '选调生'].map((type) => <button key={type} onClick={() => setExamType((current) => current === type ? 'all' : type)} className={`rounded-full px-3 py-1.5 text-xs font-bold ${examType === type ? 'bg-[var(--ink)] text-[var(--paper)]' : 'bg-[var(--soft)]'}`}>{type}</button>)}<select aria-label="考试类型" value={examType} onChange={(event) => setExamType(event.target.value)} className="rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"><option value="all">全部类型</option>{allTypes.map((type) => <option key={type}>{type}</option>)}</select></div></section>
    {Object.entries(groups).map(([title, entries]) => <section key={title}><h2 className="mb-3 text-xl font-black">{title} <span className="font-mono text-xs text-[var(--muted)]">{entries.length}</span></h2><div className="grid gap-3">{entries.map((item) => { const left = daysFromNow(item.extra?.endSignUpTime); const universityHits = Array.isArray(item.extra?.target_university_hit) ? item.extra.target_university_hit.map(String) : []; return <button key={item.url} onClick={() => openItem(item)} className={`grid gap-3 rounded-2xl border bg-[var(--card)] p-4 text-left hover:border-cyan-400 sm:grid-cols-[auto_1fr_auto] sm:items-center ${universityHits.length ? 'border-rose-400 ring-1 ring-rose-300' : 'border-[var(--line)]'}`}><span className="w-fit rounded-lg bg-cyan-100 px-3 py-2 text-xs font-black text-cyan-900">{String(item.extra?.province || '全国')}</span><span><span className="mb-1 flex flex-wrap items-center gap-2"><b>{item.title_zh || item.title}</b>{item.is_new && <i className="not-italic rounded bg-lime-300 px-1.5 text-[10px] font-black text-slate-950">新</i>}{universityHits.map((name) => <i key={name} className="not-italic rounded bg-rose-500 px-2 py-0.5 text-[10px] font-black text-white">定向 · {name}</i>)}{item.extra?.subsource === 'scs' && <i className="not-italic rounded bg-red-100 px-2 py-0.5 text-[10px] font-black text-red-700">国家公务员局</i>}</span><small className="text-[var(--muted)]">{String(item.extra?.exam_type || '其他')} · 报名 {md(item.extra?.startSignUpTime)}～{md(item.extra?.endSignUpTime)} · 笔试 {md(item.extra?.startWriteTime)}</small></span>{left !== null && left >= 0 && <span className={`text-sm font-black ${left <= 2 ? 'text-rose-500' : 'text-orange-500'}`}>距截止 {left} 天</span>}</button>; })}{entries.length === 0 && <p className="rounded-xl border border-dashed border-[var(--line)] p-4 text-xs text-[var(--muted)]">当前筛选下暂无项目</p>}</div></section>)}
    <section><h2 className="mb-1 text-xl font-black">官方人事考试入口</h2><p className="mb-4 text-xs text-[var(--muted)]">全国 34 个入口，已逐个验证可访问</p><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{sites.map((site) => <a key={`${site.province}-${site.url}`} href={site.url} target="_blank" rel="noreferrer" className="rounded-xl border border-[var(--line)] bg-[var(--card)] p-3 hover:border-cyan-400"><b className="block text-xs text-cyan-600">{site.province}</b><span className="text-sm">{site.name} ↗</span></a>)}</div></section>
  </div>;
}

function DeadlineBoard({ items, state }: { items: Item[]; state?: SourceState }) {
  return <section className="mb-8 rounded-2xl border border-violet-200 bg-violet-50/70 p-4 text-slate-950 dark:border-violet-900 dark:bg-violet-950/20 dark:text-white">
    <div className="mb-3 flex items-end justify-between"><div><h2 className="text-lg font-black">会议 Deadline</h2><p className="text-xs opacity-60">生信 / ML 顶会投稿窗口，临近截止优先</p></div><span className={`h-2 w-2 rounded-full ${state?.status === 'ok' ? 'bg-emerald-400' : 'bg-orange-400'}`} title={state?.status || 'not_run'} /></div>
    <div className="grid gap-2 sm:grid-cols-2">{items.map((item) => <button key={item.url + item.published_at} onClick={() => openItem(item)} className="rounded-xl border border-violet-200 bg-white/80 p-3 text-left transition hover:border-violet-500 dark:border-violet-900 dark:bg-black/20"><span className="flex items-center justify-between gap-3"><b className="truncate text-sm">{item.title}</b><i className={`shrink-0 not-italic text-xs font-black ${Number(item.extra?.days_left ?? 999) <= 7 ? 'text-rose-500' : 'text-violet-600 dark:text-violet-300'}`}>{item.hot_value}</i></span><small className="mt-1 block truncate opacity-60">{item.summary_zh}</small></button>)}</div>
    {items.length === 0 && <p className="rounded-xl border border-dashed border-violet-200 p-4 text-xs opacity-60">{state?.status === 'degraded' ? '会议源暂时不可用，不影响论文列表。' : '近期没有关注会议的截止日期。'}</p>}
  </section>;
}

function PapersView({ items, deadlines, deadlineState, onlyPriority, onToggle, unavailable, error, onCluster }: { items: Item[]; deadlines: Item[]; deadlineState?: SourceState; onlyPriority: boolean; onToggle: () => void; unavailable: boolean; error?: string | null; onCluster: (event: React.MouseEvent, item: Item) => void }) {
  return <><DeadlineBoard items={deadlines} state={deadlineState} /><div className="mb-4 flex items-center justify-between gap-3 text-xs text-[var(--muted)]"><span>顶刊论文</span><div className="flex items-center gap-3"><span>{items.length} 条</span><label className="flex cursor-pointer items-center gap-1.5 rounded-full bg-[var(--soft)] px-3 py-1.5 font-bold text-[var(--ink)]"><input type="checkbox" checked={onlyPriority} onChange={onToggle} className="accent-amber-500" />只看我的方向</label></div></div><div className="grid gap-3">{items.map((item) => <HotCard key={`${item.rank}-${item.url}`} item={item} onCluster={onCluster} />)}{items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">这个来源暂不可用</p><p className="mt-2 text-xs">{error || '采集端已安全降级，不影响其它来源。'}</p></> : '当前筛选下暂无论文。'}</div>}</div></>;
}

function QiuzhaoLinks() {
  return <section className="mx-auto max-w-3xl">
    <div className="rounded-3xl border border-[var(--line)] bg-[var(--card)] p-6 shadow-sm sm:p-10">
      <span className="inline-flex rounded-full bg-[var(--soft)] px-3 py-1 text-xs font-black text-[var(--muted)]">27届 · 校园招聘</span>
      <h2 className="mt-5 text-2xl font-black tracking-tight sm:text-3xl">27秋招信息</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--muted)]">27秋招信息（每日更新，数据在飞书表格，点击直达）</p>
      <div className="mt-7 grid gap-3">
        <a href="https://yal2at57cvq.feishu.cn/base/GtSLbyyR3aCENOsJYC6cdlsVnih?table=tblH4au5rnBcqHgJ&view=vewMjMLWkM" target="_blank" rel="noreferrer" className="rounded-2xl bg-[var(--ink)] px-6 py-4 text-center text-base font-black text-[var(--paper)] transition hover:-translate-y-0.5 hover:opacity-90 sm:text-lg">打开 27届秋招表 →</a>
        <div className="grid gap-3 sm:grid-cols-2">
          <a href="https://yal2at57cvq.feishu.cn/base/GtSLbyyR3aCENOsJYC6cdlsVnih" target="_blank" rel="noreferrer" className="rounded-2xl border border-[var(--line)] bg-[var(--paper)] px-5 py-3 text-center text-sm font-bold transition hover:border-cyan-400">完整表格库 ↗</a>
        </div>
      </div>
      <p className="mt-5 text-xs text-[var(--muted)]">需登录你的飞书账号，未登录会显示无权限</p>
    </div>
  </section>;
}

function App() {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [trends, setTrends] = useState<Trends | null>(null);
  const [sites, setSites] = useState<OfficialSite[]>([]);
  const [active, setActive] = useState('all');
  const [dark, setDark] = useState(() => localStorage.theme !== 'light');
  const [highlightCluster, setHighlightCluster] = useState<string | null>(null);
  const [tabOrder, setTabOrder] = useState(loadTabOrder);
  const [hiddenTabs, setHiddenTabs] = useState(loadHiddenTabs);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [papersOnlyPriority, setPapersOnlyPriority] = useState(() => localStorage.getItem('papers_only_priority') === 'true');
  useEffect(() => { Promise.all([
    fetch('./data/all.json').then((r) => r.json()), fetch('./data/trends.json').then((r) => r.json()).catch(() => null),
    fetch('./data/gongkao_official_sites.json').then((r) => r.json()).catch(() => ({ sites: [] })),
  ]).then(([nextFeed, nextTrends, nextSites]) => { setFeed(nextFeed); setTrends(nextTrends); setSites(nextSites.sites); }).catch(() => setFeed({ generated_at: '', items: [] })); }, []);
  useEffect(() => { document.documentElement.classList.toggle('dark', dark); localStorage.theme = dark ? 'dark' : 'light'; }, [dark]);
  useEffect(() => { if (hiddenTabs.includes(active)) setActive('all'); }, [active, hiddenTabs]);
  useEffect(() => {
    if (!settingsOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setSettingsOpen(false); };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [settingsOpen]);
  const state = feed?.sources?.find((source) => source.source === active);
  const deadlineState = feed?.sources?.find((source) => source.source === 'conf_deadlines');
  const deadlines = useMemo(() => feed?.items.filter((item) => item.source === 'conf_deadlines') ?? [], [feed]);
  const unavailable = active in sourceNames && state?.status !== 'ok';
  const items = useMemo(() => {
    if (unavailable) return [];
    const sourceItems = feed?.items.filter((item) => active === 'all' || item.source === active) ?? [];
    return active === 'papers' && papersOnlyPriority
      ? sourceItems.filter((item) => Number(item.extra?.priority_rank ?? 999) < 999)
      : sourceItems;
  }, [feed, active, unavailable, papersOnlyPriority]);
  const pinned = useMemo(() => { const seen = new Set<string>(); return (feed?.items ?? []).filter((item) => { if ((item.cluster_size ?? 0) < 3 || !item.cluster_id || seen.has(item.cluster_id)) return false; seen.add(item.cluster_id); return true; }); }, [feed]);
  const moveTab = (tab: string, direction: -1 | 1) => setTabOrder((current) => {
    const index = current.indexOf(tab), target = index + direction;
    if (index < 1 || target < 1 || target >= current.length) return current;
    const next = [...current];
    [next[index], next[target]] = [next[target], next[index]];
    localStorage.setItem('tab_order', JSON.stringify(next));
    return next;
  });
  const toggleTab = (tab: string) => setHiddenTabs((current) => {
    const next = current.includes(tab) ? current.filter((value) => value !== tab) : [...current, tab];
    localStorage.setItem('tab_hidden', JSON.stringify(next));
    return next;
  });
  const restoreTabs = () => {
    localStorage.removeItem('tab_order');
    localStorage.removeItem('tab_hidden');
    setTabOrder([...defaultTabs]);
    setHiddenTabs([]);
  };
  const togglePapersOnlyPriority = () => setPapersOnlyPriority((current) => {
    localStorage.setItem('papers_only_priority', String(!current));
    return !current;
  });
  const showCluster = (event: React.MouseEvent, item: Item) => { event.stopPropagation(); if (!item.cluster_id) return; setActive('all'); setHighlightCluster((current) => current === item.cluster_id ? null : item.cluster_id ?? null); };
  return <main className="min-h-screen bg-[var(--paper)] text-[var(--ink)] transition-colors">
    <header className="border-b border-[var(--line)] bg-[var(--header)] text-white"><div className="mx-auto max-w-6xl px-4 pb-7 pt-5 sm:px-6 sm:pb-10 sm:pt-8"><div className="flex items-center justify-between"><span className="font-mono text-[11px] tracking-[0.2em] text-cyan-300">SIGNAL / NOISE · P2</span><div className="flex items-center gap-2"><button type="button" aria-label="调整导航标签" aria-expanded={settingsOpen} className="rounded-full border border-white/20 px-3 py-1.5 text-xs transition hover:bg-white/10" onClick={() => setSettingsOpen(true)}>⚙ 设置</button><button type="button" className="rounded-full border border-white/20 px-3 py-1.5 text-xs transition hover:bg-white/10" onClick={() => setDark((value) => !value)}>{dark ? '☀ 浅色' : '◐ 深色'}</button></div></div><div className="mt-8 grid gap-5 sm:grid-cols-[1fr_auto] sm:items-end"><div><p className="mb-2 text-sm text-slate-400">看见一条热搜，也看见全网正在汇聚的信号。</p><h1 className="text-4xl font-black tracking-[-0.06em] sm:text-6xl">信息差<span className="text-cyan-300">日报</span></h1></div><div className="border-l-2 border-lime-300 pl-3 text-xs leading-5 text-slate-300"><div>{feed?.sources?.filter((source) => source.status === 'ok').length ?? 0}/{feed?.sources?.length ?? 0} 来源正常 · {feed?.sources?.filter((source) => source.status !== 'ok').length ?? 0} 降级</div><div>{feed?.generated_at ? `更新于 ${new Date(feed.generated_at).toLocaleString('zh-CN')}` : '正在同步最新信号…'}</div></div></div></div></header>
    <nav className="sticky top-0 z-10 overflow-x-auto border-b border-[var(--line)] bg-[var(--paper)]/95 backdrop-blur"><div className="mx-auto flex max-w-6xl gap-1 px-4 py-3 sm:px-6">{tabOrder.filter((tab) => !hiddenTabs.includes(tab)).map((tab) => { const tabState = feed?.sources?.find((source) => source.source === tab); return <button key={tab} onClick={() => { setActive(tab); setHighlightCluster(null); }} className={`flex items-center gap-1.5 whitespace-nowrap rounded-full px-4 py-2 text-sm font-bold ${active === tab ? 'bg-[var(--ink)] text-[var(--paper)]' : 'text-[var(--muted)] hover:bg-[var(--soft)]'}`}>{tab === 'all' ? `全部 ${feed?.items.length ?? ''}` : tabLabel(tab)}{tabState && <span title={tabState.status} className={`h-1.5 w-1.5 rounded-full ${tabState.status === 'ok' ? 'bg-emerald-400' : 'bg-orange-400'}`} />}</button>; })}</div></nav>
    {settingsOpen && <div className="fixed inset-0 z-50"><button type="button" aria-label="关闭导航设置" className="absolute inset-0 h-full w-full bg-slate-950/60 backdrop-blur-sm" onClick={() => setSettingsOpen(false)} /><aside role="dialog" aria-modal="true" aria-labelledby="tab-settings-title" className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-[var(--line)] bg-[var(--card)] text-[var(--ink)] shadow-2xl"><div className="flex items-start justify-between border-b border-[var(--line)] px-5 py-5"><div><h2 id="tab-settings-title" className="text-xl font-black">导航设置</h2><p className="mt-1 text-xs text-[var(--muted)]">调整顺序，隐藏暂时不关心的标签</p></div><button type="button" aria-label="关闭" className="rounded-full bg-[var(--soft)] px-3 py-1.5 text-sm font-bold" onClick={() => setSettingsOpen(false)}>×</button></div><div className="flex-1 overflow-y-auto p-4"><div className="mb-3 flex items-center justify-between rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"><span className="font-bold">全部</span><span className="rounded-full bg-[var(--soft)] px-2.5 py-1 text-[11px] text-[var(--muted)]">固定首位</span></div><div className="grid gap-2">{tabOrder.slice(1).map((tab, index, adjustable) => { const hidden = hiddenTabs.includes(tab); return <div key={tab} className={`flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-3 ${hidden ? 'opacity-65' : ''}`}><span className="min-w-0 flex-1 truncate font-bold">{tabLabel(tab)}</span><button type="button" aria-label={`${tabLabel(tab)}上移`} disabled={index === 0} onClick={() => moveTab(tab, -1)} className="h-8 w-8 rounded-full bg-[var(--soft)] text-sm font-black disabled:cursor-not-allowed disabled:opacity-30">↑</button><button type="button" aria-label={`${tabLabel(tab)}下移`} disabled={index === adjustable.length - 1} onClick={() => moveTab(tab, 1)} className="h-8 w-8 rounded-full bg-[var(--soft)] text-sm font-black disabled:cursor-not-allowed disabled:opacity-30">↓</button><label className="flex cursor-pointer items-center gap-1.5 rounded-full bg-[var(--soft)] px-2.5 py-1.5 text-xs font-bold"><input type="checkbox" checked={!hidden} onChange={() => toggleTab(tab)} className="accent-cyan-500" /><span>{hidden ? '隐藏' : '显示'}</span></label></div>; })}</div></div><div className="border-t border-[var(--line)] p-4"><button type="button" onClick={restoreTabs} className="w-full rounded-full border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm font-bold transition hover:border-cyan-400">恢复默认</button></div></aside></div>}
    <section className="mx-auto max-w-6xl px-4 py-5 sm:px-6 sm:py-8">
      {active === 'all' && pinned.length > 0 && <section className="mb-9 rounded-3xl border border-orange-200 bg-gradient-to-br from-orange-50 to-amber-50 p-5 text-slate-950 dark:border-orange-900 dark:from-orange-950/30 dark:to-amber-950/20 dark:text-white"><div className="mb-4"><span className="rounded-full bg-orange-500 px-3 py-1 text-xs font-black text-white">全网都在关注</span><p className="mt-2 text-xs opacity-60">至少 3 个平台同时出现的热点信号</p></div><div className="grid gap-3 sm:grid-cols-2">{pinned.map((item) => <button key={item.cluster_id} onClick={(event) => showCluster(event, item)} className="rounded-2xl bg-white/70 p-4 text-left shadow-sm dark:bg-black/20"><b className="line-clamp-2">{item.title_zh || item.title}</b><small className="mt-2 block text-orange-600">{item.cluster_size} 个平台正在讨论 →</small></button>)}</div></section>}
      {active === 'trends' ? <TrendView trends={trends} /> : active === 'xiaohongshu' ? <XhsView items={items} /> : active === 'gongkao' ? <GongkaoView items={items} sites={sites} /> : active === 'papers' ? <PapersView items={items} deadlines={deadlines} deadlineState={deadlineState} onlyPriority={papersOnlyPriority} onToggle={togglePapersOnlyPriority} unavailable={unavailable} error={state?.error} onCluster={showCluster} /> : active === 'qiuzhao' ? <QiuzhaoLinks /> : <><div className="mb-4 flex items-center justify-between gap-3 text-xs text-[var(--muted)]"><span>{active === 'all' ? '全网信号流' : `${sourceNames[active]}热榜`}</span>{highlightCluster ? <button className="rounded-full bg-orange-100 px-3 py-1 font-bold text-orange-700" onClick={() => setHighlightCluster(null)}>正在高亮同簇 · 清除</button> : <span>{items.length} 条</span>}</div><div className="grid gap-3">{items.map((item) => <HotCard key={`${item.source}-${item.rank}-${item.url}`} item={item} onCluster={showCluster} highlight={highlightCluster ? item.cluster_id === highlightCluster : undefined} />)}{feed && items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">这个来源暂不可用</p><p className="mt-2 text-xs">{state?.error || '采集端已安全降级，不影响其它来源。'}</p></> : '这个来源暂时没有数据。'}</div>}</div></>}
    </section>
  </main>;
}

export default App;
