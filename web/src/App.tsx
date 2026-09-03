import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { deepMergePrefs, loadPrefs, prefBoolean, prefString, prefStringArray, savePrefs, type Prefs } from './prefs';

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
type AlertItem = { id: string; tag: string; region: string; type: string; title: string; url: string; date: string; summary?: string; created_at: string };
type AlertFeed = { generated_at: string; items: AlertItem[] };
type Quicklink = { name: string; url: string };
type SourceFeed = { generated_at: string; source: string; status: SourceState; items: Item[] };
type SessionUser = { username: string; is_admin: boolean };
type AdminUser = SessionUser & { id: number; created_at: string };

const sourceNames: Record<string, string> = {
  weibo: '微博', bilibili: 'B站', github: 'GitHub', youtube: 'YouTube', douyin: '抖音',
  telegram: 'Telegram', gongkao: '公考', xiaohongshu: '小红书雷达', papers: '顶刊',
  nowcoder: '牛客', jobs: '岗位', ai: 'AI动态', tools: '工具更新', qiuzhao: '秋招', alerts: '预警',
};
const itemSourceName = (source: string) => source === 'conf_deadlines' ? '会议 Deadline' : sourceNames[source] ?? source;

const defaultTabs = ['all', ...Object.keys(sourceNames), 'trends'];
const tabLabel = (tab: string) => tab === 'all' ? '全部' : tab === 'trends' ? '趋势' : sourceNames[tab] ?? tab;

const tabOrderFromPrefs = (prefs: Prefs) => {
  const allowed = new Set(defaultTabs.slice(1));
  const stored = prefStringArray(prefs, 'tab_order').filter((tab) => allowed.has(tab));
  const ordered = [...new Set(stored)];
  return ['all', ...ordered, ...defaultTabs.slice(1).filter((tab) => !ordered.includes(tab))];
};

const hiddenTabsFromPrefs = (prefs: Prefs) => {
  const allowed = new Set(defaultTabs.slice(1));
  return [...new Set(prefStringArray(prefs, 'tab_hidden').filter((tab) => allowed.has(tab)))];
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
  return <svg viewBox="0 0 74 32" className="h-8 w-[74px] shrink-0" aria-label="近七天排名走势">
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
  const paper = item.source === 'papers' || (item.source === 'feed' && item.extra?.tab === 'papers');
  const topicHits = Array.isArray(item.extra?.topic_hit) ? item.extra.topic_hit.map(String) : [];
  const keywordHits = Array.isArray(item.extra?.keyword_hit) ? item.extra.keyword_hit.map(String) : [];
  const paperHighlighted = paper && (topicHits.length > 0 || keywordHits.length > 0);
  return <article role="link" tabIndex={0} onClick={() => openItem(item)} onKeyDown={(event) => event.key === 'Enter' && openItem(item)}
    className={`group grid w-full min-w-0 max-w-full cursor-pointer grid-cols-[42px_minmax(0,1fr)] gap-3 overflow-hidden rounded-2xl border bg-[var(--card)] p-4 shadow-sm transition focus:outline-none focus:ring-2 focus:ring-cyan-400 sm:grid-cols-[58px_minmax(0,1fr)_auto] sm:gap-5 sm:p-5 ${highlight === false ? 'border-transparent opacity-30' : 'border-[var(--line)] hover:-translate-y-0.5 hover:border-cyan-400'} ${highlight ? 'ring-2 ring-orange-400' : ''} ${paperHighlighted ? 'border-l-4 border-l-amber-400' : ''}`}>
    <div className="font-mono text-2xl font-bold text-[var(--rank)]">{String(item.rank).padStart(2, '0')}</div>
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] font-bold">
        <span className="rounded bg-[var(--soft)] px-2 py-1">{item.source === 'feed' ? String(item.extra?.feed_name || 'RSSHub') : itemSourceName(item.source)}</span>
        {item.source === 'feed' && <span className="rounded bg-cyan-100 px-2 py-1 text-cyan-900">feed</span>}
        {paperHighlighted && <span className="rounded bg-amber-100 px-2 py-1 text-amber-900">🔖 方向命中</span>}
        {item.is_new && <span className="rounded bg-lime-300 px-2 py-1 text-slate-950">新</span>}
        {(item.cluster_size ?? 0) >= 2 && <button className="rounded bg-orange-100 px-2 py-1 text-orange-700" onClick={(event) => onCluster?.(event, item)}>🔥 全网 {item.cluster_size} 平台</button>}
      </div>
      <h2 className="break-words text-lg font-extrabold leading-snug tracking-tight group-hover:text-cyan-600 sm:text-xl">{item.title_zh || item.title}</h2>
      {item.title_zh && item.title_zh !== item.title && <details className="mt-1 text-xs text-[var(--muted)]" onClick={(event) => event.stopPropagation()}><summary className="cursor-pointer truncate">原标题 · {item.title}</summary></details>}
      {item.summary_zh && <p className={`mt-2 break-words text-sm leading-6 text-[var(--muted)] ${paper ? 'line-clamp-3' : 'line-clamp-2'}`}>{item.summary_zh}</p>}
      {paper && <div className="mt-3 flex min-w-0 max-w-full flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted)]"><span className="min-w-0 break-words">{String(item.extra?.journal || item.extra?.field || '论文')} · {item.published_at?.slice(0, 10) || '日期待定'}</span>{topicHits.map((topic) => <span key={topic} className="max-w-full break-words rounded-full bg-amber-200 px-2 py-0.5 font-bold text-amber-900">{topic}</span>)}{keywordHits.map((keyword) => <span key={keyword} className="max-w-full break-words rounded-full bg-[var(--soft)] px-2 py-0.5">{keyword}</span>)}</div>}
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

function GongkaoView({ items, sites, provinces, onProvincesChange }: { items: Item[]; sites: OfficialSite[]; provinces: string[]; onProvincesChange: (next: string[]) => void }) {
  const allProvinces = useMemo(() => Array.from(new Set(items.map((item) => String(item.extra?.province || '全国')))).sort(), [items]);
  const allTypes = useMemo(() => {
    const found = Array.from(new Set(items.map((item) => String(item.extra?.exam_type || '其他')))).sort();
    return ['国考', '选调生', '省考', ...found.filter((type) => !['国考', '选调生', '省考'].includes(type))];
  }, [items]);
  const [examType, setExamType] = useState('all');
  const filtered = items.filter((item) => (!provinces.length || provinces.includes(String(item.extra?.province || '全国'))) && (examType === 'all' || item.extra?.exam_type === examType));
  const groups: Record<string, Item[]> = { '报名进行中': [], '即将报名（7天）': [], '即将笔试（14天）': [], '近期公告': [] };
  for (const item of filtered) {
    const start = daysFromNow(item.extra?.startSignUpTime), end = daysFromNow(item.extra?.endSignUpTime), write = daysFromNow(item.extra?.startWriteTime);
    const group = start !== null && start <= 0 && end !== null && end >= 0 ? '报名进行中' : start !== null && start > 0 && start <= 7 ? '即将报名（7天）' : write !== null && write >= 0 && write <= 14 ? '即将笔试（14天）' : '近期公告';
    groups[group].push(item);
  }
  const toggle = (province: string) => onProvincesChange(provinces.includes(province) ? provinces.filter((value) => value !== province) : [...provinces, province]);
  return <div className="grid gap-8">
    <section className="rounded-2xl border border-[var(--line)] bg-[var(--card)] p-4"><div className="mb-3 flex flex-wrap items-center gap-2"><b className="mr-2 text-sm">省份多选</b>{allProvinces.map((province) => <button key={province} onClick={() => toggle(province)} className={`rounded-full px-3 py-1.5 text-xs font-bold ${provinces.includes(province) ? 'bg-cyan-500 text-white' : 'bg-[var(--soft)]'}`}>{province}</button>)}{provinces.length > 0 && <button onClick={() => onProvincesChange([])} className="text-xs text-cyan-600">清空</button>}</div><div className="flex flex-wrap items-center gap-2"><b className="mr-2 text-sm">考试类型</b>{['国考', '选调生', '省考'].map((type) => <button key={type} onClick={() => setExamType((current) => current === type ? 'all' : type)} className={`rounded-full px-3 py-1.5 text-xs font-bold ${examType === type ? 'bg-[var(--ink)] text-[var(--paper)]' : 'bg-[var(--soft)]'}`}>{type}</button>)}<select aria-label="考试类型" value={examType} onChange={(event) => setExamType(event.target.value)} className="rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"><option value="all">全部类型</option>{allTypes.map((type) => <option key={type}>{type}</option>)}</select></div></section>
    {Object.entries(groups).map(([title, entries]) => <section key={title}><h2 className="mb-3 text-xl font-black">{title} <span className="font-mono text-xs text-[var(--muted)]">{entries.length}</span></h2><div className="grid gap-3">{entries.map((item) => { const left = daysFromNow(item.extra?.endSignUpTime); const universityHits = Array.isArray(item.extra?.target_university_hit) ? item.extra.target_university_hit.map(String) : []; const cityHits = Array.isArray(item.extra?.city_focus_hit) ? item.extra.city_focus_hit.map(String) : []; return <button key={item.url} onClick={() => openItem(item)} className={`grid gap-3 rounded-2xl border bg-[var(--card)] p-4 text-left hover:border-cyan-400 sm:grid-cols-[auto_1fr_auto] sm:items-center ${universityHits.length ? 'border-rose-400 ring-1 ring-rose-300' : cityHits.length ? 'border-amber-400 ring-1 ring-amber-300' : 'border-[var(--line)]'}`}><span className="w-fit rounded-lg bg-cyan-100 px-3 py-2 text-xs font-black text-cyan-900">{String(item.extra?.province || '全国')}</span><span><span className="mb-1 flex flex-wrap items-center gap-2"><b>{item.title_zh || item.title}</b>{item.is_new && <i className="not-italic rounded bg-lime-300 px-1.5 text-[10px] font-black text-slate-950">新</i>}{cityHits.map((name) => <i key={name} className="not-italic rounded bg-amber-300 px-2 py-0.5 text-[10px] font-black text-amber-950">重点城市 · {name}</i>)}{universityHits.map((name) => <i key={name} className="not-italic rounded bg-rose-500 px-2 py-0.5 text-[10px] font-black text-white">你的学校 · {name}</i>)}{item.extra?.subsource === 'scs' && <i className="not-italic rounded bg-red-100 px-2 py-0.5 text-[10px] font-black text-red-700">国家公务员局</i>}{item.extra?.subsource === 'xuandiao' && <i className="not-italic rounded bg-violet-100 px-2 py-0.5 text-[10px] font-black text-violet-700">官方选调</i>}</span><small className="text-[var(--muted)]">{String(item.extra?.exam_type || '其他')} · 报名 {md(item.extra?.startSignUpTime)}～{md(item.extra?.endSignUpTime)} · 笔试 {md(item.extra?.startWriteTime)}</small></span>{left !== null && left >= 0 && <span className={`text-sm font-black ${left <= 2 ? 'text-rose-500' : 'text-orange-500'}`}>距截止 {left} 天</span>}</button>; })}{entries.length === 0 && <p className="rounded-xl border border-dashed border-[var(--line)] p-4 text-xs text-[var(--muted)]">当前筛选下暂无项目</p>}</div></section>)}
    <section><h2 className="mb-1 text-xl font-black">官方人事考试入口</h2><p className="mb-4 text-xs text-[var(--muted)]">全国 34 个入口，已逐个验证可访问</p><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{sites.map((site) => <a key={`${site.province}-${site.url}`} href={site.url} target="_blank" rel="noreferrer" className="rounded-xl border border-[var(--line)] bg-[var(--card)] p-3 hover:border-cyan-400"><b className="block text-xs text-cyan-600">{site.province}</b><span className="text-sm">{site.name} ↗</span></a>)}</div></section>
  </div>;
}

function DeadlineBoard({ items, state }: { items: Item[]; state?: SourceState }) {
  return <section className="mb-8 w-full min-w-0 max-w-full overflow-hidden rounded-2xl border border-violet-200 bg-violet-50/70 p-4 text-slate-950 dark:border-violet-900 dark:bg-violet-950/20 dark:text-white">
    <div className="mb-3 flex min-w-0 items-start justify-between gap-3"><div className="min-w-0"><h2 className="text-lg font-black">会议 Deadline</h2><p className="break-words text-xs opacity-60">生信 / ML 顶会投稿窗口，临近截止优先</p></div><span className={`mt-2 h-2 w-2 shrink-0 rounded-full ${state?.status === 'ok' ? 'bg-emerald-400' : 'bg-orange-400'}`} title={state?.status || 'not_run'} /></div>
    <div className="grid min-w-0 gap-2 sm:grid-cols-2">{items.map((item) => <button key={item.url + item.published_at} onClick={() => openItem(item)} className="w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-violet-200 bg-white/80 p-3 text-left transition hover:border-violet-500 dark:border-violet-900 dark:bg-black/20"><span className="flex min-w-0 items-center justify-between gap-3"><b className="min-w-0 flex-1 truncate text-sm">{item.title}</b><i className={`shrink-0 whitespace-nowrap not-italic text-xs font-black ${Number(item.extra?.days_left ?? 999) <= 7 ? 'text-rose-500' : 'text-violet-600 dark:text-violet-300'}`}>{item.hot_value}</i></span><small className="mt-1 block max-w-full truncate opacity-60">{item.summary_zh}</small></button>)}</div>
    {items.length === 0 && <p className="rounded-xl border border-dashed border-violet-200 p-4 text-xs opacity-60">{state?.status === 'degraded' ? '会议源暂时不可用，不影响论文列表。' : '近期没有关注会议的截止日期。'}</p>}
  </section>;
}

function PapersView({ items, deadlines, deadlineState, onlyPriority, onToggle, unavailable, error, onCluster }: { items: Item[]; deadlines: Item[]; deadlineState?: SourceState; onlyPriority: boolean; onToggle: () => void; unavailable: boolean; error?: string | null; onCluster: (event: React.MouseEvent, item: Item) => void }) {
  const tier = (item: Item) => item.source === 'feed' ? 'RSSHub' : String(item.extra?.tier || (['arxiv', 'biorxiv', 'medrxiv'].includes(String(item.extra?.subsource)) ? '预印本' : '英文顶刊'));
  const sections: Array<[string, string]> = [['英文顶刊', 'PubMed 正刊'], ['中文核心', 'Crossref · 中文核心期刊'], ['预印本', 'arXiv + bioRxiv'], ['RSSHub', 'Nature / Science / HF 每日论文等订阅源']];
  return <div className="w-full min-w-0 max-w-full"><DeadlineBoard items={deadlines} state={deadlineState} /><div className="mb-5 flex min-w-0 flex-col items-start gap-3 text-xs text-[var(--muted)] sm:flex-row sm:items-center sm:justify-between"><span className="break-words">论文雷达 · 按研究方向与算法关键词排序</span><div className="flex max-w-full flex-wrap items-center gap-3"><span className="whitespace-nowrap">{items.length} 条</span><label className="flex max-w-full cursor-pointer items-center gap-1.5 rounded-full bg-[var(--soft)] px-3 py-1.5 font-bold text-[var(--ink)]"><input type="checkbox" checked={onlyPriority} onChange={onToggle} className="shrink-0 accent-amber-500" /><span>只看我的方向</span></label></div></div><div className="grid min-w-0 gap-9">{sections.map(([name, subtitle]) => { const entries = items.filter((item) => tier(item) === name); return <section key={name} className="min-w-0"><div className="mb-3 flex min-w-0 items-end justify-between gap-3"><div className="min-w-0"><h2 className="text-xl font-black">{name}</h2><p className="break-words text-xs text-[var(--muted)]">{subtitle}</p></div><span className="shrink-0 font-mono text-xs text-[var(--muted)]">{entries.length}</span></div><div className="grid min-w-0 gap-3">{entries.map((item) => <HotCard key={`${item.rank}-${item.url}`} item={item} onCluster={onCluster} />)}{entries.length === 0 && items.length > 0 && <p className="rounded-xl border border-dashed border-[var(--line)] p-4 text-xs text-[var(--muted)]">当前没有{name}条目</p>}</div></section>; })}{items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">这个来源暂不可用</p><p className="mt-2 text-xs">{error || '采集端已安全降级，不影响其它来源。'}</p></> : '当前筛选下暂无论文。'}</div>}</div></div>;
}

function AiView({ items, unavailable, error }: { items: Item[]; unavailable: boolean; error?: string | null }) {
  return <section><div className="mb-5 flex items-end justify-between"><div><h2 className="text-2xl font-black">AI动态</h2><p className="mt-1 text-xs text-[var(--muted)]">机器之心、量子位、PaperWeekly 等订阅源</p></div><span className="font-mono text-xs text-[var(--muted)]">{items.length}</span></div><div className="grid gap-3">{items.map((item) => <a key={item.url} href={item.url} target="_blank" rel="noreferrer" className="rounded-2xl border border-[var(--line)] bg-[var(--card)] p-5 shadow-sm transition hover:-translate-y-0.5 hover:border-cyan-400"><div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--muted)]"><b className="rounded bg-[var(--soft)] px-2 py-1 text-[var(--ink)]">{String(item.extra?.feed_name || 'RSSHub')}</b><time>{item.published_at ? new Date(item.published_at).toLocaleString('zh-CN') : '时间待定'}</time></div><h3 className="mt-3 text-lg font-black leading-snug">{item.title_zh || item.title}</h3>{item.summary_zh && <p className="mt-2 line-clamp-3 text-sm leading-6 text-[var(--muted)]">{item.summary_zh}</p>}<small className="mt-3 block font-bold text-cyan-600">查看原文 ↗</small></a>)}{items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">RSSHub 暂不可用</p><p className="mt-2 text-xs">{error || '未配置或订阅路由暂时失败，不影响其它来源。'}</p></> : '暂时没有 AI 动态。'}</div>}</div></section>;
}

function ToolsView({ items, unavailable, error }: { items: Item[]; unavailable: boolean; error?: string | null }) {
  return <section><div className="mb-5 flex items-end justify-between"><div><h2 className="text-2xl font-black">工具更新</h2><p className="mt-1 text-xs text-[var(--muted)]">常用科研工具的新版本与 release notes</p></div><span className="font-mono text-xs text-[var(--muted)]">{items.length}</span></div><div className="overflow-hidden rounded-2xl border border-[var(--line)] bg-[var(--card)]">{items.map((item, index) => <a key={item.url} href={item.url} target="_blank" rel="noreferrer" className={`grid gap-2 p-4 transition hover:bg-[var(--soft)] sm:grid-cols-[140px_1fr_auto] sm:items-center ${index ? 'border-t border-[var(--line)]' : ''}`}><b className="text-sm text-cyan-600">{String(item.extra?.feed_name || '工具更新').replace(/\s*发版$/, '')}</b><span className="min-w-0"><strong className="block truncate text-sm">{item.title_zh || item.title}</strong>{item.summary_zh && <small className="mt-1 block line-clamp-2 text-[var(--muted)]">{item.summary_zh}</small>}</span><time className="text-xs text-[var(--muted)]">{item.published_at?.slice(0, 10) || '日期待定'} · release ↗</time></a>)}{items.length === 0 && <div className="p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">RSSHub 暂不可用</p><p className="mt-2 text-xs">{error || '未配置或订阅路由暂时失败，不影响其它来源。'}</p></> : '暂时没有工具更新。'}</div>}</div></section>;
}

function JobsView({ items, quicklinks, unavailable, error }: { items: Item[]; quicklinks: Quicklink[]; unavailable: boolean; error?: string | null }) {
  return <div className="grid gap-8"><section className="rounded-3xl border border-[var(--line)] bg-[var(--card)] p-5 sm:p-7"><span className="rounded-full bg-cyan-100 px-3 py-1 text-xs font-black text-cyan-900">单细胞 / AI4Science</span><h2 className="mt-4 text-2xl font-black">大厂岗位雷达</h2><p className="mt-2 text-sm text-[var(--muted)]">腾讯与字节职位按关键词命中数优先；以下公司暂以官网直达补充。</p><div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{quicklinks.map((link) => <a key={link.url} href={link.url} target="_blank" rel="noreferrer" className="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm font-bold transition hover:border-cyan-400">{link.name} ↗</a>)}</div></section><section><div className="mb-3 flex items-end justify-between"><div><h2 className="text-xl font-black">在招岗位</h2><p className="text-xs text-[var(--muted)]">同名岗位合并，多关键词命中的排在前面</p></div><span className="font-mono text-xs text-[var(--muted)]">{items.length}</span></div><div className="grid gap-3">{items.map((item) => { const hits = Array.isArray(item.extra?.keywords_hit) ? item.extra.keywords_hit.map(String) : []; const city = String(item.extra?.city || ''); return <article key={item.url} className="rounded-2xl border border-[var(--line)] bg-[var(--card)] p-5 shadow-sm"><div className="flex flex-wrap items-center gap-2 text-xs"><b className="rounded bg-[var(--soft)] px-2 py-1">{String(item.extra?.company || '招聘')}</b>{city && <span className="text-[var(--muted)]">📍 {city}</span>}{hits.map((hit) => <span key={hit} className="rounded-full bg-cyan-100 px-2 py-0.5 font-bold text-cyan-900">{hit}</span>)}</div><h3 className="mt-3 text-lg font-black">{item.title_zh || item.title}</h3>{item.summary_zh && <p className="mt-2 line-clamp-3 text-sm leading-6 text-[var(--muted)]">{item.summary_zh}</p>}<div className="mt-4 flex items-center justify-between"><time className="text-xs text-[var(--muted)]">{item.published_at?.slice(0, 10) || '更新日期待定'}</time><a href={item.url} target="_blank" rel="noreferrer" className="rounded-full bg-[var(--ink)] px-4 py-2 text-xs font-black text-[var(--paper)]">投递 →</a></div></article>; })}{items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">岗位接口暂不可用</p><p className="mt-2 text-xs">{error || '招聘站点已触发安全降级，官网直达仍可使用。'}</p></> : '当前关键词暂未命中岗位，可先使用上方官网直达。'}</div>}</div></section></div>;
}

function AlertsView({ feed }: { feed: AlertFeed }) {
  return <section className="mx-auto max-w-4xl">
    <div className="mb-5 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100"><b>关键期预警</b><p className="mt-1 text-xs opacity-70">百度云监听五省选调、人社局公告与核心补贴政策页；选调每 6 小时，补贴每 12 小时。</p></div>
    <div className="grid gap-3">{feed.items.map((alert) => <a key={alert.id} href={alert.url} target="_blank" rel="noreferrer" className="rounded-2xl border border-[var(--line)] bg-[var(--card)] p-5 transition hover:-translate-y-0.5 hover:border-amber-400"><span className="flex flex-wrap items-center gap-2 text-[11px] font-black"><i className="not-italic rounded bg-red-500 px-2 py-1 text-white">{alert.tag}</i><i className="not-italic rounded bg-[var(--soft)] px-2 py-1">{alert.type}</i><time className="ml-auto text-[var(--muted)]">{alert.date}</time></span><h2 className="mt-3 text-lg font-black">{alert.title}</h2>{alert.summary && <p className="mt-2 text-sm leading-6 text-[var(--muted)]">{alert.summary}</p>}<small className="mt-3 block text-amber-600">查看官方原文 ↗</small></a>)}{feed.items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]"><p className="font-bold text-[var(--ink)]">暂无新预警</p><p className="mt-2 text-xs">首次运行只建立基线；之后出现的新公告或实质政策变化会显示在这里。</p></div>}</div>
  </section>;
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

const readError = async (response: Response, fallback: string) => {
  try {
    const body = await response.json() as { detail?: string };
    return body.detail || fallback;
  } catch {
    return fallback;
  }
};

function LoginScreen({ onAuthenticated }: { onAuthenticated: () => Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      const response = await fetch('/api/login', {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        setError(await readError(response, '登录失败，请检查用户名和密码'));
        return;
      }
      await onAuthenticated();
    } catch {
      setError('暂时无法连接登录服务');
    } finally {
      setSubmitting(false);
    }
  };
  return <main className="grid min-h-screen place-items-center bg-[var(--paper)] px-4 text-[var(--ink)]">
    <form onSubmit={submit} className="w-full max-w-sm rounded-3xl border border-[var(--line)] bg-[var(--card)] p-7 shadow-2xl sm:p-9">
      <span className="font-mono text-[10px] tracking-[0.22em] text-cyan-500">SIGNAL / NOISE</span>
      <h1 className="mt-4 text-3xl font-black tracking-tight">信息差日报</h1>
      <p className="mt-2 text-sm text-[var(--muted)]">登录后查看你的信息流与同步设置</p>
      <label className="mt-7 block text-xs font-bold">用户名<input aria-label="用户名" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required className="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-base outline-none focus:border-cyan-400" /></label>
      <label className="mt-4 block text-xs font-bold">密码<input aria-label="密码" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required className="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-base outline-none focus:border-cyan-400" /></label>
      {error && <p role="alert" className="mt-4 rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950/30 dark:text-rose-300">{error}</p>}
      <button disabled={submitting} className="mt-6 w-full rounded-xl bg-[var(--ink)] px-5 py-3.5 font-black text-[var(--paper)] disabled:opacity-50">{submitting ? '正在登录…' : '登录'}</button>
    </form>
  </main>;
}

function AdminPanel() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState('');
  const loadUsers = useCallback(async () => {
    const response = await fetch('/api/admin/users', { credentials: 'include' });
    if (response.ok) setUsers(((await response.json()) as { users: AdminUser[] }).users);
  }, []);
  useEffect(() => { void loadUsers(); }, [loadUsers]);
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError('');
    const response = await fetch('/api/admin/users', {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, is_admin: isAdmin }),
    });
    if (!response.ok) return setError(await readError(response, '创建用户失败'));
    setUsername(''); setPassword(''); setIsAdmin(false); await loadUsers();
  };
  const remove = async (name: string) => {
    if (!window.confirm(`确定删除用户 ${name}？`)) return;
    const response = await fetch(`/api/admin/users/${encodeURIComponent(name)}`, { method: 'DELETE', credentials: 'include' });
    if (!response.ok) return setError(await readError(response, '删除用户失败'));
    await loadUsers();
  };
  const resetPassword = async (name: string) => {
    const next = window.prompt(`为 ${name} 设置新密码（最多 72 字节）`);
    if (!next) return;
    const response = await fetch(`/api/admin/users/${encodeURIComponent(name)}/password`, {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: next }),
    });
    if (!response.ok) setError(await readError(response, '重置密码失败'));
  };
  return <section className="mt-7 border-t border-[var(--line)] pt-6">
    <h3 className="text-lg font-black">用户管理</h3>
    <p className="mt-1 text-xs text-[var(--muted)]">仅管理员可见，密码不会显示或返回。</p>
    <div className="mt-4 grid gap-2">{users.map((entry) => <div key={entry.id} className="rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3"><div className="flex items-center gap-2"><b className="min-w-0 flex-1 truncate">{entry.username}</b>{entry.is_admin && <span className="rounded bg-cyan-100 px-2 py-0.5 text-[10px] font-black text-cyan-900">管理员</span>}</div><small className="mt-1 block text-[var(--muted)]">创建于 {new Date(entry.created_at).toLocaleString('zh-CN')}</small><div className="mt-2 flex gap-2"><button type="button" onClick={() => void resetPassword(entry.username)} className="rounded-full bg-[var(--soft)] px-3 py-1 text-xs font-bold">重置密码</button><button type="button" onClick={() => void remove(entry.username)} className="rounded-full bg-rose-50 px-3 py-1 text-xs font-bold text-rose-600 dark:bg-rose-950/30">删除</button></div></div>)}</div>
    <form onSubmit={create} className="mt-4 grid gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4"><b className="text-sm">新建用户</b><input aria-label="新用户名" value={username} onChange={(event) => setUsername(event.target.value)} required placeholder="用户名" className="rounded-lg border border-[var(--line)] bg-[var(--card)] px-3 py-2" /><input aria-label="新用户密码" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required placeholder="密码" className="rounded-lg border border-[var(--line)] bg-[var(--card)] px-3 py-2" /><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={isAdmin} onChange={(event) => setIsAdmin(event.target.checked)} />设为管理员</label><button className="rounded-lg bg-[var(--ink)] px-4 py-2 text-sm font-black text-[var(--paper)]">创建账号</button></form>
    {error && <p role="alert" className="mt-3 text-xs text-rose-500">{error}</p>}
  </section>;
}

function App() {
  const [authState, setAuthState] = useState<'checking' | 'anonymous' | 'authenticated'>('checking');
  const [user, setUser] = useState<SessionUser | null>(null);
  const [feed, setFeed] = useState<Feed | null>(null);
  const [trends, setTrends] = useState<Trends | null>(null);
  const [sites, setSites] = useState<OfficialSite[]>([]);
  const [alerts, setAlerts] = useState<AlertFeed>({ generated_at: '', items: [] });
  const [jobQuicklinks, setJobQuicklinks] = useState<Quicklink[]>([]);
  const [prefs, setPrefs] = useState<Prefs>(() => loadPrefs());
  const [prefsReady, setPrefsReady] = useState(false);
  const [settingsUpdatedAt, setSettingsUpdatedAt] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [active, setActive] = useState('all');
  const [highlightCluster, setHighlightCluster] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const syncGeneration = useRef(0);

  const updatePrefs = useCallback((patch: Prefs) => setPrefs((current) => {
    const next = { ...current, ...patch };
    savePrefs(next);
    return next;
  }), []);
  const removePrefs = useCallback((keys: string[]) => setPrefs((current) => {
    const next = { ...current };
    for (const key of keys) delete next[key];
    savePrefs(next);
    return next;
  }), []);

  const bootstrap = useCallback(async () => {
    setAuthState('checking');
    setPrefsReady(false);
    try {
      const meResponse = await fetch('/api/me', { credentials: 'include', cache: 'no-store' });
      if (meResponse.status === 401) {
        setUser(null); setFeed(null); setAuthState('anonymous'); return;
      }
      if (!meResponse.ok) throw new Error('auth unavailable');
      setUser(await meResponse.json() as SessionUser);
      setAuthState('authenticated');
      const readJson = async <T,>(url: string, fallback: T): Promise<T> => {
        const response = await fetch(url, { credentials: 'include', cache: 'no-store' });
        return response.ok ? response.json() as Promise<T> : fallback;
      };
      const emptySourceFeed = (source: string): SourceFeed => ({ generated_at: '', source, status: { source, status: 'not_run', item_count: 0 }, items: [] });
      const [nextFeed, nextAi, nextTools, nextPapers, nextJobs, nextTrends, nextSites, nextAlerts, nextJobLinks, remoteSettings] = await Promise.all([
        readJson<Feed>('/data/all.json', { generated_at: '', items: [] }),
        readJson<SourceFeed>('/data/ai.json', emptySourceFeed('ai')),
        readJson<SourceFeed>('/data/tools.json', emptySourceFeed('tools')),
        readJson<SourceFeed>('/data/papers.json', emptySourceFeed('papers')),
        readJson<SourceFeed>('/data/jobs.json', emptySourceFeed('jobs')),
        readJson<Trends | null>('/data/trends.json', null),
        readJson<{ sites: OfficialSite[] }>('/data/gongkao_official_sites.json', { sites: [] }),
        readJson<AlertFeed>('/data/alerts.json', { generated_at: '', items: [] }),
        readJson<{ items: Quicklink[] }>('/data/job_quicklinks.json', { items: [] }),
        readJson<{ prefs: Prefs; updated_at: string | null } | null>('/api/settings', null),
      ]);
      const additions = [
        ...nextAi.items, ...nextTools.items,
        ...nextPapers.items.filter((item) => item.source === 'feed'),
        ...nextJobs.items.filter((item) => item.source === 'feed'),
      ];
      const seenItems = new Set<string>();
      const mergedItems = [...nextFeed.items, ...additions].filter((item) => {
        const key = `${item.source}\0${item.url}`;
        if (seenItems.has(key)) return false;
        seenItems.add(key); return true;
      });
      const sourceStates = new Map((nextFeed.sources ?? []).map((entry) => [entry.source, entry]));
      for (const extraFeed of [nextAi, nextTools, nextPapers, nextJobs]) sourceStates.set(extraFeed.source, extraFeed.status);
      setFeed({ ...nextFeed, items: mergedItems, sources: [...sourceStates.values()] }); setTrends(nextTrends); setSites(nextSites.sites); setAlerts(nextAlerts); setJobQuicklinks(nextJobLinks.items);
      const localPrefs = loadPrefs();
      const merged = remoteSettings ? deepMergePrefs(localPrefs, remoteSettings.prefs) : localPrefs;
      savePrefs(merged); setPrefs(merged); setSettingsUpdatedAt(remoteSettings?.updated_at ?? null); setPrefsReady(true);
    } catch {
      setUser(null); setFeed(null); setAuthState('anonymous');
    }
  }, []);

  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    if (authState !== 'authenticated' || !prefsReady) return;
    const generation = ++syncGeneration.current;
    setSyncing(true);
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch('/api/settings', {
          method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prefs }),
        });
        if (response.ok && generation === syncGeneration.current) {
          const result = await response.json() as { updated_at: string };
          setSettingsUpdatedAt(result.updated_at);
        }
      } catch {
        // The local copy remains authoritative while the sync service is unavailable.
      } finally {
        if (generation === syncGeneration.current) setSyncing(false);
      }
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [authState, prefs, prefsReady]);

  const dark = prefString(prefs, 'theme', 'dark') !== 'light';
  const tabOrder = useMemo(() => tabOrderFromPrefs(prefs), [prefs]);
  const hiddenTabs = useMemo(() => hiddenTabsFromPrefs(prefs), [prefs]);
  const papersOnlyPriority = prefBoolean(prefs, 'papers_only_priority');
  const gongkaoProvinces = prefStringArray(prefs, 'gongkao_provinces');
  const alertsLastSeen = prefString(prefs, 'alerts_last_seen');

  useEffect(() => { document.documentElement.classList.toggle('dark', dark); }, [dark]);
  useEffect(() => { if (hiddenTabs.includes(active)) setActive('all'); }, [active, hiddenTabs]);
  const latestAlert = alerts.items.reduce((latest, alert) => alert.created_at > latest ? alert.created_at : latest, '');
  const hasUnreadAlerts = alerts.items.some((alert) => alert.created_at > alertsLastSeen);
  useEffect(() => {
    if (active === 'alerts' && latestAlert && latestAlert !== alertsLastSeen) updatePrefs({ alerts_last_seen: latestAlert });
  }, [active, alertsLastSeen, latestAlert, updatePrefs]);
  useEffect(() => {
    if (!settingsOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setSettingsOpen(false); };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [settingsOpen]);

  const logout = async () => {
    try { await fetch('/api/logout', { method: 'POST', credentials: 'include' }); } catch { /* local logout still applies */ }
    if ('caches' in globalThis) {
      const keys = await globalThis.caches.keys();
      await Promise.all(keys.filter((key) => key.startsWith('hot-gap-')).map((key) => globalThis.caches.delete(key)));
    }
    setUser(null); setFeed(null); setTrends(null); setPrefsReady(false); setSettingsOpen(false); setAuthState('anonymous');
  };

  if (authState === 'checking') return <main className="grid min-h-screen place-items-center bg-[var(--paper)] text-sm font-bold text-[var(--muted)]">正在验证登录状态…</main>;
  if (authState === 'anonymous') return <LoginScreen onAuthenticated={bootstrap} />;

  const state = feed?.sources?.find((source) => source.source === active);
  const deadlineState = feed?.sources?.find((source) => source.source === 'conf_deadlines');
  const deadlines = feed?.items.filter((item) => item.source === 'conf_deadlines') ?? [];
  const unavailable = active in sourceNames && !['alerts', 'qiuzhao'].includes(active) && state?.status !== 'ok';
  const matchesTab = (item: Item, tab: string) => tab === 'all'
    ? item.source !== 'feed' || item.extra?.tab === 'hot'
    : item.source === tab || (item.source === 'feed' && item.extra?.tab === tab);
  const sourceItems = unavailable ? [] : feed?.items.filter((item) => matchesTab(item, active)) ?? [];
  const items = active === 'papers' && papersOnlyPriority ? sourceItems.filter((item) => Number(item.extra?.priority_rank ?? 999) < 999) : sourceItems;
  const seen = new Set<string>();
  const allItems = (feed?.items ?? []).filter((item) => matchesTab(item, 'all'));
  const pinned = allItems.filter((item) => { if ((item.cluster_size ?? 0) < 3 || !item.cluster_id || seen.has(item.cluster_id)) return false; seen.add(item.cluster_id); return true; });
  const moveTab = (tab: string, direction: -1 | 1) => {
    const index = tabOrder.indexOf(tab), target = index + direction;
    if (index < 1 || target < 1 || target >= tabOrder.length) return;
    const next = [...tabOrder]; [next[index], next[target]] = [next[target], next[index]];
    updatePrefs({ tab_order: next });
  };
  const toggleTab = (tab: string) => updatePrefs({ tab_hidden: hiddenTabs.includes(tab) ? hiddenTabs.filter((value) => value !== tab) : [...hiddenTabs, tab] });
  const restoreTabs = () => removePrefs(['tab_order', 'tab_hidden']);
  const showCluster = (event: React.MouseEvent, item: Item) => { event.stopPropagation(); if (!item.cluster_id) return; setActive('all'); setHighlightCluster((current) => current === item.cluster_id ? null : item.cluster_id ?? null); };
  return <main className="min-h-screen w-full min-w-0 max-w-full overflow-x-hidden bg-[var(--paper)] text-[var(--ink)] transition-colors">
    <header className="border-b border-[var(--line)] bg-[var(--header)] text-white"><div className="mx-auto max-w-6xl px-4 pb-7 pt-5 sm:px-6 sm:pb-10 sm:pt-8"><div className="flex flex-wrap items-center justify-between gap-3"><span className="font-mono text-[11px] tracking-[0.2em] text-cyan-300">SIGNAL / NOISE · P2</span><div className="flex flex-wrap items-center justify-end gap-2"><span className="rounded-full bg-white/10 px-3 py-1.5 text-xs">{user?.username}{user?.is_admin ? ' · 管理员' : ''}</span><button type="button" aria-label="调整导航标签" aria-expanded={settingsOpen} className="rounded-full border border-white/20 px-3 py-1.5 text-xs transition hover:bg-white/10" onClick={() => setSettingsOpen(true)}>⚙ 设置</button><button type="button" className="rounded-full border border-white/20 px-3 py-1.5 text-xs transition hover:bg-white/10" onClick={() => updatePrefs({ theme: dark ? 'light' : 'dark' })}>{dark ? '☀ 浅色' : '◐ 深色'}</button><button type="button" onClick={() => void logout()} className="rounded-full border border-white/20 px-3 py-1.5 text-xs transition hover:bg-white/10">退出</button></div></div><div className="mt-8 grid gap-5 sm:grid-cols-[1fr_auto] sm:items-end"><div><p className="mb-2 text-sm text-slate-400">看见一条热搜，也看见全网正在汇聚的信号。</p><h1 className="text-4xl font-black tracking-[-0.06em] sm:text-6xl">信息差<span className="text-cyan-300">日报</span></h1></div><div className="border-l-2 border-lime-300 pl-3 text-xs leading-5 text-slate-300"><div>{feed?.sources?.filter((source) => source.status === 'ok').length ?? 0}/{feed?.sources?.length ?? 0} 来源正常 · {feed?.sources?.filter((source) => source.status !== 'ok').length ?? 0} 降级</div><div>{feed?.generated_at ? `更新于 ${new Date(feed.generated_at).toLocaleString('zh-CN')}` : '正在同步最新信号…'}</div></div></div></div></header>
    <nav className="sticky top-0 z-10 w-full max-w-full overflow-x-auto border-b border-[var(--line)] bg-[var(--paper)]/95 backdrop-blur"><div className="mx-auto flex w-max min-w-full max-w-6xl gap-1 px-4 py-3 sm:px-6">{tabOrder.filter((tab) => !hiddenTabs.includes(tab)).map((tab) => { const tabState = feed?.sources?.find((source) => source.source === tab); return <button key={tab} onClick={() => { setActive(tab); setHighlightCluster(null); }} className={`flex items-center gap-1.5 whitespace-nowrap rounded-full px-4 py-2 text-sm font-bold ${active === tab ? 'bg-[var(--ink)] text-[var(--paper)]' : 'text-[var(--muted)] hover:bg-[var(--soft)]'}`}>{tab === 'all' ? `全部 ${allItems.length}` : tabLabel(tab)}{tab === 'alerts' && hasUnreadAlerts && <span aria-label="有未读预警" className="h-2 w-2 animate-pulse rounded-full bg-red-500" />}{tabState && <span title={tabState.status} className={`h-1.5 w-1.5 rounded-full ${tabState.status === 'ok' ? 'bg-emerald-400' : 'bg-orange-400'}`} />}</button>; })}</div></nav>
    {settingsOpen && <div className="fixed inset-0 z-50"><button type="button" aria-label="关闭导航设置" className="absolute inset-0 h-full w-full bg-slate-950/60 backdrop-blur-sm" onClick={() => setSettingsOpen(false)} /><aside role="dialog" aria-modal="true" aria-labelledby="tab-settings-title" className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-[var(--line)] bg-[var(--card)] text-[var(--ink)] shadow-2xl"><div className="flex items-start justify-between border-b border-[var(--line)] px-5 py-5"><div><h2 id="tab-settings-title" className="text-xl font-black">导航设置</h2><p className="mt-1 text-xs text-[var(--muted)]">{syncing ? '正在同步…' : settingsUpdatedAt ? `已同步 · 上次 ${new Date(settingsUpdatedAt).toLocaleString('zh-CN')}` : '偏好保存在本机，服务恢复后会自动同步'}</p></div><button type="button" aria-label="关闭" className="rounded-full bg-[var(--soft)] px-3 py-1.5 text-sm font-bold" onClick={() => setSettingsOpen(false)}>×</button></div><div className="flex-1 overflow-y-auto p-4"><div className="mb-3 flex items-center justify-between rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"><span className="font-bold">全部</span><span className="rounded-full bg-[var(--soft)] px-2.5 py-1 text-[11px] text-[var(--muted)]">固定首位</span></div><div className="grid gap-2">{tabOrder.slice(1).map((tab, index, adjustable) => { const hidden = hiddenTabs.includes(tab); return <div key={tab} className={`flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-3 ${hidden ? 'opacity-65' : ''}`}><span className="min-w-0 flex-1 truncate font-bold">{tabLabel(tab)}</span><button type="button" aria-label={`${tabLabel(tab)}上移`} disabled={index === 0} onClick={() => moveTab(tab, -1)} className="h-8 w-8 rounded-full bg-[var(--soft)] text-sm font-black disabled:cursor-not-allowed disabled:opacity-30">↑</button><button type="button" aria-label={`${tabLabel(tab)}下移`} disabled={index === adjustable.length - 1} onClick={() => moveTab(tab, 1)} className="h-8 w-8 rounded-full bg-[var(--soft)] text-sm font-black disabled:cursor-not-allowed disabled:opacity-30">↓</button><label className="flex cursor-pointer items-center gap-1.5 rounded-full bg-[var(--soft)] px-2.5 py-1.5 text-xs font-bold"><input type="checkbox" checked={!hidden} onChange={() => toggleTab(tab)} className="accent-cyan-500" /><span>{hidden ? '隐藏' : '显示'}</span></label></div>; })}</div><button type="button" onClick={restoreTabs} className="mt-4 w-full rounded-full border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm font-bold transition hover:border-cyan-400">恢复默认</button>{user?.is_admin && <AdminPanel />}</div></aside></div>}
    <section className="mx-auto w-full min-w-0 max-w-6xl px-4 py-5 sm:px-6 sm:py-8">
      {active === 'all' && pinned.length > 0 && <section className="mb-9 rounded-3xl border border-orange-200 bg-gradient-to-br from-orange-50 to-amber-50 p-5 text-slate-950 dark:border-orange-900 dark:from-orange-950/30 dark:to-amber-950/20 dark:text-white"><div className="mb-4"><span className="rounded-full bg-orange-500 px-3 py-1 text-xs font-black text-white">全网都在关注</span><p className="mt-2 text-xs opacity-60">至少 3 个平台同时出现的热点信号</p></div><div className="grid gap-3 sm:grid-cols-2">{pinned.map((item) => <button key={item.cluster_id} onClick={(event) => showCluster(event, item)} className="rounded-2xl bg-white/70 p-4 text-left shadow-sm dark:bg-black/20"><b className="line-clamp-2">{item.title_zh || item.title}</b><small className="mt-2 block text-orange-600">{item.cluster_size} 个平台正在讨论 →</small></button>)}</div></section>}
      {active === 'trends' ? <TrendView trends={trends} /> : active === 'alerts' ? <AlertsView feed={alerts} /> : active === 'xiaohongshu' ? <XhsView items={items} /> : active === 'gongkao' ? <GongkaoView items={items} sites={sites} provinces={gongkaoProvinces} onProvincesChange={(next) => updatePrefs({ gongkao_provinces: next })} /> : active === 'papers' ? <PapersView items={items} deadlines={deadlines} deadlineState={deadlineState} onlyPriority={papersOnlyPriority} onToggle={() => updatePrefs({ papers_only_priority: !papersOnlyPriority })} unavailable={unavailable} error={state?.error} onCluster={showCluster} /> : active === 'jobs' ? <JobsView items={items} quicklinks={jobQuicklinks} unavailable={unavailable} error={state?.error} /> : active === 'ai' ? <AiView items={items} unavailable={unavailable} error={state?.error} /> : active === 'tools' ? <ToolsView items={items} unavailable={unavailable} error={state?.error} /> : active === 'qiuzhao' ? <QiuzhaoLinks /> : <><div className="mb-4 flex items-center justify-between gap-3 text-xs text-[var(--muted)]"><span>{active === 'all' ? '全网信号流' : `${sourceNames[active]}热榜`}</span>{highlightCluster ? <button className="rounded-full bg-orange-100 px-3 py-1 font-bold text-orange-700" onClick={() => setHighlightCluster(null)}>正在高亮同簇 · 清除</button> : <span>{items.length} 条</span>}</div><div className="grid gap-3">{items.map((item) => <HotCard key={`${item.source}-${item.rank}-${item.url}`} item={item} onCluster={showCluster} highlight={highlightCluster ? item.cluster_id === highlightCluster : undefined} />)}{feed && items.length === 0 && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-[var(--muted)]">{unavailable ? <><p className="font-bold text-[var(--ink)]">这个来源暂不可用</p><p className="mt-2 text-xs">{state?.error || '采集端已安全降级，不影响其它来源。'}</p></> : '这个来源暂时没有数据。'}</div>}</div></>}
    </section>
  </main>;
}

export default App;
