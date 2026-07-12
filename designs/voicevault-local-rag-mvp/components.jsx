(function () {
var VV_useEffect = React.useEffect;

const VV_ICONS = {
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></>,
  collect: <><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></>,
  chat: <><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/><path d="M8 9h8M8 13h5"/></>,
  activity: <><path d="M3 12h4l2.5-7 5 14 2.5-7h4"/></>,
  plus: <><path d="M12 5v14M5 12h14"/></>,
  arrow: <><path d="M5 12h14M13 6l6 6-6 6"/></>,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  alert: <><path d="M12 3 2.8 19a1.4 1.4 0 0 0 1.2 2h16a1.4 1.4 0 0 0 1.2-2Z"/><path d="M12 9v4M12 17h.01"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
  sparkle: <><path d="m12 3 1.2 3.5L17 8l-3.8 1.5L12 13l-1.2-3.5L7 8l3.8-1.5Z"/><path d="m18.5 14 .7 2.1 2.3.9-2.3.9-.7 2.1-.7-2.1-2.3-.9 2.3-.9Z"/></>,
  external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/></>,
  close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  play: <path d="m8 5 11 7-11 7Z"/>,
  refresh: <><path d="M20 7h-5V2"/><path d="M20 7a8 8 0 1 0 1 8"/></>,
  shield: <><path d="M12 3 4 6v5c0 5 3.4 8.5 8 10 4.6-1.5 8-5 8-10V6Z"/><path d="m9 12 2 2 4-4"/></>,
  terminal: <><path d="m5 7 4 4-4 4M11 17h8"/></>,
  chevron: <path d="m9 18 6-6-6-6"/>,
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/></>,
  filter: <><path d="M4 5h16M7 12h10M10 19h4"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
  link: <><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/></>,
  eye: <><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></>,
  personAdd: <><circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M19 8v6M16 11h6"/></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/></>,
  file: <><path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></>,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/></>,
};

function Icon({ name, size = 18, className = "" }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {VV_ICONS[name] || VV_ICONS.info}
    </svg>
  );
}

function Brand({ compact = false }) {
  return (
    <div className={`brand ${compact ? "brand-compact" : ""}`}>
      <span className="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
      {!compact && <span><strong>声迹</strong><small>VoiceVault</small></span>}
    </div>
  );
}

function Button({ variant = "primary", icon, children, className = "", ...props }) {
  return (
    <button className={`button button-${variant} ${className}`} {...props}>
      {icon && <Icon name={icon} size={17} />}
      <span>{children}</span>
    </button>
  );
}

function StatusPill({ tone = "neutral", children, dot = true, icon }) {
  return <span className={`status-pill status-${tone}`}>{icon ? <Icon name={icon} size={14} /> : dot ? <i aria-hidden="true"></i> : null}{children}</span>;
}

function Avatar({ person, size = "md" }) {
  const name = person?.name || "?";
  return <span className={`avatar avatar-${size} avatar-${person?.tone || "teal"}`} aria-hidden="true">{person?.initials || name.slice(0, 1)}</span>;
}

function PageIntro({ eyebrow, title, description, actions }) {
  return (
    <header className="page-intro">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

function MetricCard({ label, value, meta, icon, tone = "teal" }) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <span className="metric-icon"><Icon name={icon} size={18} /></span>
      <div><p>{label}</p><strong>{value}</strong><small>{meta}</small></div>
    </article>
  );
}

function Progress({ value, tone = "teal", label }) {
  return (
    <div className="progress-wrap" aria-label={label || `进度 ${value}%`}>
      <div className="progress-track"><i className={`progress-${tone}`} style={{ width: `${Math.max(0, Math.min(value, 100))}%` }}></i></div>
      {label && <small>{label}</small>}
    </div>
  );
}

function Modal({ open, title, description, onClose, children, footer, wide = false }) {
  VV_useEffect(() => {
    if (!open) return;
    const handle = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <header>
          <div><p className="eyebrow">本机资料库</p><h2 id="modal-title">{title}</h2>{description && <p>{description}</p>}</div>
          <button className="icon-button" onClick={onClose} aria-label="关闭对话框"><Icon name="close" /></button>
        </header>
        <div className="modal-content">{children}</div>
        {footer && <footer>{footer}</footer>}
      </section>
    </div>
  );
}

function Toast({ toast, onClose }) {
  VV_useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(onClose, 3200);
    return () => clearTimeout(timer);
  }, [toast, onClose]);
  if (!toast) return null;
  return (
    <div className={`toast toast-${toast.tone || "success"}`} role="status">
      <Icon name={toast.tone === "warning" ? "alert" : "check"} size={18} />
      <span><strong>{toast.title}</strong>{toast.detail && <small>{toast.detail}</small>}</span>
      <button onClick={onClose} aria-label="关闭提示"><Icon name="close" size={16} /></button>
    </div>
  );
}

const NAV_ITEMS = [
  { id: "workspace", label: "工作台", short: "工作台", icon: "activity" },
  { id: "people", label: "人物", short: "人物", icon: "users" },
  { id: "collect", label: "采集", short: "采集", icon: "collect" },
  { id: "knowledge", label: "知识库", short: "知识库", icon: "database" },
  { id: "ask", label: "问答", short: "问答", icon: "chat" },
  { id: "extensions", label: "研究扩展", short: "扩展", icon: "layers" },
  { id: "runtime", label: "系统", short: "系统", icon: "server" },
];

function AppShell({ page, pageTitle, connected, lastSync, onNavigate, children }) {
  return <div className="app-shell"><Sidebar page={page} onNavigate={onNavigate}/><div className="app-main"><TopStatus pageTitle={pageTitle} connected={connected} lastSync={lastSync}/><main>{children}</main></div><MobileNav page={page} onNavigate={onNavigate}/></div>;
}

function PageHeader(props) { return <PageIntro {...props}/>; }

function EmptyState({ icon = "search", title, description, action }) {
  return <section className="empty-state panel"><span className="ask-orbit"><Icon name={icon} size={26}/></span><h2>{title}</h2><p>{description}</p>{action}</section>;
}

function SuccessNotice({ title, description }) {
  return <div className="success-notice"><Icon name="check"/><span><strong>{title}</strong><small>{description}</small></span></div>;
}

function AccountList({ accounts, empty }) {
  if (!accounts?.length) return empty || null;
  return <div className="account-list">{accounts.map((account) => <div key={account.account_id}><span className="platform-badge">{account.platform}</span><span><strong>@{account.external_user_id}</strong><small>{account.readiness === "ready" ? "已确认公开归档" : account.readiness}</small></span><StatusPill tone={account.readiness === "ready" ? "success" : "warning"}>{account.readiness}</StatusPill></div>)}</div>;
}

function CollectionStepper({ steps, currentId }) {
  const current = Math.max(0, steps.findIndex((step) => step.id === currentId));
  return <section className="stepper" aria-label="采集任务进度">{steps.map((step, index) => <div key={step.id} className={`${index <= current ? "is-done" : ""} ${index === current ? "is-current" : ""}`}><i>{index < current ? <Icon name="check" size={14}/> : index + 1}</i><span>{step.label}</span></div>)}</section>;
}

function CitationChip({ id, active, onSelect }) {
  return <button className={`citation ${active ? "is-active" : ""}`} onMouseEnter={() => onSelect(id)} onFocus={() => onSelect(id)} onClick={() => onSelect(id)}>{id}</button>;
}

function EvidenceCard({ evidence, person, active, onSelect }) {
  return <article data-evidence={evidence.evidence_id} className={`evidence-card ${active ? "is-active" : ""}`} onClick={() => onSelect(evidence.evidence_id)}><header><span className="evidence-id">{evidence.evidence_id}</span><span><strong>{person?.name || evidence.person_id}</strong><small>{evidence.platform} @{person?.account || evidence.account_id?.slice(0, 8)}</small></span><StatusPill tone={evidence.observation_status === "available" ? "success" : "warning"} dot={false}>{evidence.observation_status || "已冻结"}</StatusPill></header><h3>帖子 {evidence.post_id}</h3><blockquote>“{evidence.excerpt}”</blockquote><div className="evidence-meta"><span><Icon name="calendar" size={13}/>{evidence.published_at || "时间未知"}</span><span><Icon name="layers" size={13}/>当前版本</span></div><footer><span>证据编号 <strong>{evidence.evidence_id}</strong></span>{evidence.canonical_url ? <a href={evidence.canonical_url} target="_blank" rel="noreferrer">查看原帖<Icon name="external" size={14}/></a> : <span>无原帖链接</span>}</footer></article>;
}

function QuestionComposer({ query, onQueryChange, persons, selectedIds, onTogglePerson, retrievalMode, onSubmit, busy }) {
  return <section className="panel ask-composer"><div className="composer-main"><label><span>研究问题</span><textarea value={query} onChange={(event) => onQueryChange(event.target.value)} rows="2" placeholder="输入需要比较或综合的问题…"></textarea></label><Button icon={retrievalMode === "hybrid" ? "sparkle" : "search"} onClick={onSubmit} disabled={busy}>{busy ? "正在检索真实证据" : "检索并创建问答"}</Button></div><div className="composer-options"><div className="person-picker"><span>人物知识库</span><div>{persons.map((person) => <button key={person.id} className={selectedIds.includes(person.id) ? "is-selected" : ""} onClick={() => onTogglePerson(person.id)}><Avatar person={person} size="xs"/><span>{person.name}</span>{selectedIds.includes(person.id) && <Icon name="check" size={14}/>}</button>)}</div></div><div className="ask-filters"><span><Icon name="filter"/> 当前版本 · 最多 20 条证据</span></div></div></section>;
}

function UncertaintyNotice({ kind = "info", title, message, citations = [], activeCitation, onSelect }) {
  const icon = kind === "citation_invalid" || kind === "insufficient" || kind === "degraded" ? "alert" : "info";
  return <div className={`uncertainty-notice uncertainty-${kind}`}><Icon name={icon}/><span><strong>{title}</strong><small>{message}</small>{citations.length ? <em>{citations.map((id) => <CitationChip key={id} id={id} active={activeCitation === id} onSelect={onSelect}/>)}</em> : null}</span></div>;
}

function AnswerProgress({ stage, selectedPersons, retrievalMode, evidenceCount }) {
  const stages = [
    { id: "retrieving", title: retrievalMode === "fulltext_only" ? "全文检索" : "混合检索", detail: "按人物边界检索当前可服务代次" },
    { id: "answering", title: "等待当前 Codex 任务", detail: `已冻结 ${evidenceCount} 条证据，不提交 Cookie 或知识库文件` },
    { id: "validating", title: "引用与归属校验", detail: "检查人物、帖子版本、摘录与原帖链接" },
  ];
  const current = Math.max(0, stages.findIndex((item) => item.id === stage));
  return <section className="panel answer-progress"><div className="progress-visual"><span className="search-pulse"><Icon name={stage === "retrieving" ? "search" : stage === "answering" ? "sparkle" : "shield"} size={28}/></span><div><p className="eyebrow">正在生成可核验回答</p><h2>{stages[current].title}</h2><p>{stages[current].detail}</p></div></div><div className="answer-stage-list">{stages.map((item, index) => <div key={item.id} className={`${index < current ? "done" : ""} ${index === current ? "active" : ""}`}><i>{index < current ? <Icon name="check" size={14}/> : index + 1}</i><span><strong>{item.title}</strong><small>{item.detail}</small></span></div>)}</div><div className="selected-context"><span>当前人物</span>{selectedPersons.map((person) => <span key={person.id}><Avatar person={person} size="xs"/>{person.name}</span>)}</div></section>;
}

function OpinionSection({ view, person, activeCitation, onSelect }) {
  const citationIds = view.citation_ids || [];
  return <article className="opinion-card"><header><Avatar person={person || { name: "?", tone: "teal" }}/><span><strong>{person?.name || view.person_id}</strong><small>{person?.account ? `雪球 @${person.account}` : "人物知识库"}</small></span><StatusPill tone={view.insufficient ? "warning" : "success"}>{view.insufficient ? "证据不足" : "直接证据"}</StatusPill></header><p>{view.view || "当前证据不足，不生成该人物观点。"} {citationIds.map((id) => <CitationChip key={id} id={id} active={activeCitation === id} onSelect={onSelect}/>)}</p></article>;
}

function EvidenceRail({ evidence, persons, activeId, onSelect, formatTimestamp = (value) => value }) {
  return <aside className="evidence-rail panel"><header className="panel-header"><div><p className="eyebrow">引用检查器</p><h2>冻结证据</h2></div><StatusPill tone={evidence.length ? "success" : "warning"}>{evidence.length} 条</StatusPill></header><p className="rail-hint">回答引用只能指向本次检索冻结的证据。</p>{evidence.length ? <div className="evidence-list">{evidence.map((item) => <EvidenceCard key={item.evidence_id} evidence={{ ...item, published_at: formatTimestamp(item.published_at) }} person={persons.find((entry) => entry.id === item.person_id)} active={activeId === item.evidence_id} onSelect={onSelect}/>)}</div> : <div className="empty-evidence"><Icon name="search" size={24}/><strong>没有可用证据</strong><p>系统不会用其他人物的材料填补空白。</p></div>}</aside>;
}

function AnswerSummary({ query, result, evidence, persons, activeCitation, onSelect, formatTimestamp }) {
  const citationIds = new Set((result.citations || []).map((item) => item.evidence_id));
  const cited = (ids) => (ids || []).filter((id) => citationIds.has(id)).map((id) => <CitationChip key={id} id={id} active={activeCitation === id} onSelect={onSelect}/>);
  return <section className="answer-workspace"><section className="answer-document panel"><header className="answer-head"><div><p className="eyebrow">综合回答</p><h2>{query}</h2><p>基于 {evidence.length} 条冻结证据 · {result.person_views?.length || 0} 个人物</p></div><StatusPill tone="success">引用校验通过</StatusPill></header><UncertaintyNotice kind="info" title="研究边界" message="以下内容是本地公开资料整理，不构成投资建议。"/><article className="answer-section"><h3>综合结论</h3><p>{result.combined_answer}</p><div className="answer-citations">{(result.citations || []).map((item) => <CitationChip key={item.evidence_id} id={item.evidence_id} active={activeCitation === item.evidence_id} onSelect={onSelect}/>)}</div></article><div className="consensus-grid"><article><span className="mini-icon success"><Icon name="check"/></span><div><h3>共同观点</h3>{result.consensus?.length ? <ul>{result.consensus.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>未识别到有充分证据支持的共同观点。</p>}</div></article><article><span className="mini-icon warning"><Icon name="alert"/></span><div><h3>分歧与边界</h3>{result.disagreements?.length ? result.disagreements.map((item, index) => <p key={index}>{item.summary} {cited(item.citation_ids)}</p>) : <p>{result.limitations?.join("；") || "当前证据中没有可确认的分歧。"}</p>}</div></article></div><div className="person-opinions"><h3>人物观点</h3>{(result.person_views || []).map((view) => <OpinionSection key={view.person_id} view={view} person={persons.find((item) => item.id === view.person_id)} activeCitation={activeCitation} onSelect={onSelect}/>)}</div><footer className="answer-footer"><UncertaintyNotice kind="insufficient" title="证据边界" message={result.limitations?.join("；") || "引用已由后端校验人物归属与原文摘录。"}/></footer></section><EvidenceRail evidence={evidence} persons={persons} activeId={activeCitation} onSelect={onSelect} formatTimestamp={formatTimestamp}/></section>;
}

function Sidebar({ page, onNavigate }) {
  return (
    <aside className="sidebar">
      <Brand />
      <div className="local-scope"><Icon name="shield" size={17}/><span><strong>仅本机</strong><small>单用户研究资料</small></span></div>
      <nav aria-label="主导航">
        <p className="nav-label">研究工作台</p>
        {NAV_ITEMS.map((item) => (
          <button key={item.id} className="nav-item" aria-current={page === item.id ? "page" : undefined} onClick={() => onNavigate(item.id)}>
            <Icon name={item.icon} /><span>{item.label}</span>{page === item.id && <i className="nav-active-dot"></i>}
          </button>
        ))}
      </nav>
      <div className="sidebar-note">
        <p><Icon name="shield" size={15}/> 当前服务</p>
        <strong>人物知识库 MVP</strong>
        <span>真实本地数据 · 不读取 Cookie</span>
      </div>
    </aside>
  );
}

function MobileNav({ page, onNavigate }) {
  return (
    <nav className="mobile-nav" aria-label="移动端主导航">
      {NAV_ITEMS.map((item) => <button key={item.id} aria-current={page === item.id ? "page" : undefined} onClick={() => onNavigate(item.id)}><Icon name={item.icon}/><span>{item.short}</span></button>)}
    </nav>
  );
}

function TopStatus({ pageTitle, connected = true, lastSync = null }) {
  return (
    <div className="top-status">
      <div className="mobile-brand"><Brand compact /><strong>{pageTitle}</strong></div>
      <div className="breadcrumb"><span>声迹</span><Icon name="chevron" size={14}/><strong>{pageTitle}</strong></div>
      <div className="top-status-right">
        <StatusPill tone={connected ? "success" : "warning"}>{connected ? "本地服务正常" : "服务未连接"}</StatusPill>
        <span className="top-divider"></span>
        <span className="last-sync"><Icon name="clock" size={15}/> {lastSync ? `${lastSync.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 更新` : "等待同步"}</span>
      </div>
    </div>
  );
}

function DemoBar({ label, children }) {
  return (
    <aside className="demo-bar" aria-label="原型演示控制">
      <span><Icon name="play" size={15}/><strong>原型演示</strong><small>{label}</small></span>
      <div>{children}</div>
    </aside>
  );
}

window.VVComponents = { Icon, Brand, Button, StatusPill, Avatar, PageIntro, PageHeader, MetricCard, Progress, Modal, Toast, Sidebar, MobileNav, TopStatus, AppShell, EmptyState, SuccessNotice, AccountList, CollectionStepper, CitationChip, EvidenceCard, QuestionComposer, UncertaintyNotice, AnswerProgress, OpinionSection, EvidenceRail, AnswerSummary, DemoBar, NAV_ITEMS };
})();
