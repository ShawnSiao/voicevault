const { useCallback, useEffect, useMemo, useState } = React;
const {
  Icon, Button, StatusPill, Avatar, PageIntro, MetricCard, Progress,
  Modal, Toast, AppShell, EmptyState, AccountList, CollectionStepper, QuestionComposer, UncertaintyNotice,
  AnswerProgress, AnswerSummary, EvidenceRail, NAV_ITEMS,
} = window.VVComponents;
const API = window.VoiceVaultApi;

const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const formatDateTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
}).format(new Date(value)) : "—";
const isoDay = (date) => {
  const offset = date.getTimezoneOffset() * 60 * 1000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};
const daysAgo = (days) => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return isoDay(date);
};
const personTone = (index) => ["teal", "blue", "amber"][index % 3];
const firstAccount = (person) => (person.accounts || [])[0] || null;
const latestFor = (items, key, value) => [...items].reverse().find((item) => item[key] === value) || null;
const statusTone = (status) => ({
  succeeded: "success", ready: "success", pending: "warning", pending_codex: "warning",
  claimed: "info", running: "info", waiting_for_human: "warning", rate_limited: "warning",
  partial: "warning", interrupted: "warning", failed: "error", cancelled: "error",
}[status] || "neutral");
const statusLabel = (status) => ({
  pending: "待执行", pending_codex: "等待 Codex", claimed: "已领取", running: "运行中",
  waiting_for_human: "等待人工验证", rate_limited: "平台限流", partial: "部分完成",
  succeeded: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断",
}[status] || status || "未创建");

function toUiPerson(person, index) {
  const account = firstAccount(person);
  const head = person.index_head || null;
  const name = person.display_name || "未命名人物";
  return {
    raw: person,
    id: person.person_id,
    name,
    alias: (person.aliases || []).join(" · ") || "本地人物知识库",
    initials: name.slice(0, 2),
    tone: personTone(index),
    accountId: account?.account_id || "",
    account: account?.external_user_id || "",
    platform: account?.platform || "xueqiu",
    accountStatus: account?.archive_basis_confirmed_at ? "已确认公开归档" : "待确认",
    accounts: person.accounts || [],
    posts: person.archive?.post_count || 0,
    revisions: person.archive?.revision_count || 0,
    indexHead: head,
    indexStatus: head?.status || "empty",
    retrievalMode: head?.retrieval_mode || "not_indexed",
    updatedAt: person.updated_at,
  };
}

function ErrorBanner({ error, onRetry }) {
  if (!error) return null;
  return <div className="degraded-banner resource-error-banner"><Icon name="alert"/><div><strong>本地资源加载失败</strong><p>{error}</p></div><Button variant="warning" icon="refresh" onClick={onRetry}>重新连接</Button></div>;
}

function WorkspacePage({ workspace, onNavigate, onAdd }) {
  const summary = workspace?.summary || {};
  const tasks = workspace?.pending_tasks || [];
  const people = workspace?.recent_people || [];
  const activate = (task) => onNavigate(task.target?.slice(1) || "people", task.person_id);
  return <div className="page page-workspace">
    <PageIntro eyebrow="本地公开证据系统" title="工作台" description="从人物、采集、索引到带引用问答，所有研究进度均来自本地数据库。" actions={<Button icon="personAdd" onClick={onAdd}>创建人物</Button>}/>
    <section className="metrics-grid" aria-label="研究概览"><MetricCard label="人物知识库" value={summary.person_count || 0} meta={`${summary.account_count || 0} 个已绑定账号`} icon="users"/><MetricCard label="本地公开帖子" value={formatNumber(summary.post_count)} meta="可审阅内容与版本" icon="file" tone="blue"/><MetricCard label="可开始问答" value={summary.askable_person_count || 0} meta="当前可服务知识库" icon="chat" tone="purple"/><MetricCard label="待处理事项" value={tasks.length} meta="按下一步动作排序" icon="activity" tone="amber"/></section>
    <section className="workspace-grid"><div className="panel workspace-tasks"><header className="panel-header"><div><h2>下一步行动</h2><p>每项行动由后端根据人物、账号、采集和索引状态计算。</p></div></header><div className="action-list">{tasks.map((task, index) => <button key={`${task.type}-${task.person_id || index}`} onClick={() => activate(task)}><span className={`action-number ${task.priority}`}>{index + 1}</span><span><strong>{task.label}</strong><small>{task.display_name || "从这里建立首个人物知识库"}</small></span><Icon name="chevron"/></button>)}{!tasks.length && <EmptyState icon="check" title="当前没有待处理事项" description="可从人物知识库开始新的研究问题。" action={<Button onClick={() => onNavigate("ask")}>开始问答</Button>}/>}</div></div><div className="panel workspace-people"><header className="panel-header"><div><h2>最近人物</h2><p>以真实人物为知识库边界。</p></div><Button variant="ghost" onClick={() => onNavigate("people")}>管理人物</Button></header><div className="workspace-person-list">{people.map((person, index) => <button key={person.person_id} onClick={() => onNavigate("person-detail", person.person_id)}><Avatar person={{ name: person.display_name, initials: person.display_name.slice(0, 2), tone: personTone(index) }}/><span><strong>{person.display_name}</strong><small>{person.archive.post_count} 帖子 · {person.readiness}</small></span><StatusPill tone={person.can_ask ? "success" : "warning"}>{person.next_action.label}</StatusPill></button>)}{!people.length && <EmptyState icon="users" title="还没有人物" description="创建人物并绑定一个公开平台账号后，系统会在这里显示研究进度。"/>}</div></div></section>
  </div>;
}

function PersonDetailPage({ personId, onNavigate, onRefresh, showToast }) {
  const [person, setPerson] = useState(null); const [error, setError] = useState(""); const [bindOpen, setBindOpen] = useState(false);
  const load = useCallback(async () => { if (!personId) return; try { setError(""); const payload = await API.get(`/api/persons/${encodeURIComponent(personId)}`); setPerson(payload.person); } catch (failure) { setError(failure.message); } }, [personId]);
  useEffect(() => { load(); }, [load]);
  if (!personId) return <div className="page"><EmptyState icon="users" title="未选择人物" description="从人物列表选择一个知识库后查看其建设进度。" action={<Button onClick={() => onNavigate("people")}>返回人物列表</Button>}/></div>;
  if (error) return <div className="page"><EmptyState icon="alert" title="人物详情加载失败" description={error} action={<Button onClick={load}>重试</Button>}/></div>;
  if (!person) return <div className="page"><EmptyState icon="refresh" title="正在读取人物知识库" description="正在加载人物、账号、覆盖与索引状态。"/></div>;
  const action = person.next_action || {};
  const act = () => { onNavigate(action.target?.slice(1) || "people", person.person_id); };
  return <div className="page page-person-detail"><PageIntro eyebrow="人物知识库建设中心" title={person.display_name} description="人物拥有多个平台账号；采集、帖子版本和索引代次都归属于该人物知识库。" actions={<><Button variant="ghost" onClick={() => onNavigate("people")}>返回人物</Button><Button variant="secondary" icon="link" onClick={() => setBindOpen(true)}>绑定账号</Button><Button icon="arrow" onClick={act}>{action.label}</Button></>}/><section className="person-detail-grid"><article className="panel identity-panel"><header><Avatar person={{ name: person.display_name, initials: person.display_name.slice(0, 2), tone: "teal" }} size="lg"/><div><p className="eyebrow">人物信息</p><h2>{person.display_name}</h2><p>{(person.aliases || []).join(" · ") || "未添加别名"}</p></div></header><AccountList accounts={person.accounts} empty={<EmptyState icon="link" title="尚未绑定账号" description="绑定公开平台账号后才能开始采集。"/>}/></article><article className="panel build-panel"><header className="panel-header"><div><h2>知识库建设进度</h2><p>当前状态：{person.readiness}</p></div><StatusPill tone={person.can_ask ? "success" : "warning"}>{person.can_ask ? "可开始问答" : action.label}</StatusPill></header><div className="build-steps"><div className={person.accounts.length ? "done" : "active"}><span>1</span><strong>绑定账号</strong></div><div className={person.archive.post_count ? "done" : person.accounts.length ? "active" : ""}><span>2</span><strong>采集内容</strong></div><div className={person.index_head ? "done" : person.archive.post_count ? "active" : ""}><span>3</span><strong>构建索引</strong></div><div className={person.can_ask ? "done" : ""}><span>4</span><strong>开始问答</strong></div></div><dl className="detail-list"><div><dt>帖子 / 版本</dt><dd>{formatNumber(person.archive.post_count)} / {formatNumber(person.archive.revision_count)}</dd></div><div><dt>当前索引</dt><dd>{person.index_head?.retrieval_mode || "尚未建立"}</dd></div></dl></article><aside className="panel next-action-card"><p className="eyebrow">下一步行动</p><h2>{action.label}</h2><p>此操作根据后端的 read model 生成，避免页面自行推断状态。</p><Button icon="arrow" onClick={act}>{action.label}</Button><Button variant="ghost" icon="refresh" onClick={() => { load(); onRefresh(); }}>刷新状态</Button></aside></section><BindAccountModal open={bindOpen} person={person} onClose={() => setBindOpen(false)} onBound={async () => { await Promise.all([load(), onRefresh()]); setBindOpen(false); showToast("平台账号已绑定", "可为该账号创建独立采集任务。", "success"); }}/></div>;
}

function BindAccountModal({ open, person, onClose, onBound }) {
  const [platform, setPlatform] = useState("xueqiu"); const [externalId, setExternalId] = useState(""); const [confirmed, setConfirmed] = useState(false); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setPlatform("xueqiu"); setExternalId(""); setConfirmed(false); setError(""); } }, [open]);
  const valid = externalId.trim() && confirmed && (platform !== "xueqiu" || /^\d+$/.test(externalId.trim()));
  const submit = async () => { if (!valid || busy) return; setBusy(true); setError(""); try { await API.post(`/api/persons/${encodeURIComponent(person.person_id)}/accounts`, { platform, external_user_id: externalId.trim(), archive_basis_confirmed: true }); await onBound(); } catch (failure) { setError(failure.message); } finally { setBusy(false); } };
  return <Modal open={open} onClose={onClose} title="绑定平台账号" description="账号仍归属于当前人物知识库；每个账号保留独立的采集范围与覆盖记录。" footer={<><Button variant="ghost" onClick={onClose}>取消</Button><Button icon="link" disabled={!valid || busy} onClick={submit}>{busy ? "正在绑定" : "绑定账号"}</Button></>}><div className="two-fields"><label className="field"><span>平台</span><div className="select-wrap"><select value={platform} onChange={(event) => setPlatform(event.target.value)}><option value="xueqiu">雪球</option><option value="weibo">微博</option></select><Icon name="chevron"/></div></label><label className="field"><span>公开账号 ID</span><input value={externalId} onChange={(event) => setExternalId(event.target.value)} placeholder={platform === "xueqiu" ? "雪球数字用户 ID" : "平台唯一账号 ID"}/>{platform === "xueqiu" && externalId && !/^\d+$/.test(externalId) && <em className="field-error">雪球账号必须是数字 ID</em>}</label></div><label className={`confirm-box ${confirmed ? "is-checked" : ""}`}><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}/><i>{confirmed && <Icon name="check" size={15}/>}</i><span><strong>确认归档公开内容</strong><small>只保存公开帖子及其可审阅版本，不保存登录态或 Cookie。</small></span></label>{error && <p className="form-global-error"><Icon name="alert"/>{error}</p>}</Modal>;
}

function ExtensionsPage() { return <div className="page"><PageIntro eyebrow="研究能力扩展" title="研究扩展" description="投资看板、人物比较和研究报告将在复用人物知识库与证据边界的前提下逐步增加。"/><EmptyState icon="layers" title="研究扩展尚未启用" description="当前优先保证人物归档、知识库索引和带证据问答的可靠性。"/></div>; }

function PeoplePage({ persons, onAdd, onNavigate }) {
  const [activeId, setActiveId] = useState(persons[0]?.id || "");
  const [filter, setFilter] = useState("");
  useEffect(() => {
    if (!persons.some((person) => person.id === activeId)) setActiveId(persons[0]?.id || "");
  }, [persons, activeId]);
  const filtered = persons.filter((person) => `${person.name} ${person.alias} ${person.account}`.toLowerCase().includes(filter.trim().toLowerCase()));
  const active = persons.find((person) => person.id === activeId) || persons[0];
  const postTotal = persons.reduce((total, person) => total + person.posts, 0);
  const revisionTotal = persons.reduce((total, person) => total + person.revisions, 0);
  const indexedCount = persons.filter((person) => person.indexHead).length;

  return <div className="page page-people">
    <PageIntro eyebrow="人物是知识库边界" title="人物与平台账号" description="先建立人物，再绑定公开平台账号。同一人物后续可合并雪球、微博等多个来源。" actions={<><Button variant="secondary" icon="collect" onClick={() => onNavigate("collect")}>创建采集任务</Button><Button icon="personAdd" onClick={onAdd}>添加人物</Button></>}/>
    <section className="metrics-grid" aria-label="人物资料概览">
      <MetricCard label="人物知识库" value={persons.length} meta={`${persons.filter((person) => person.account).length} 个已绑定账号`} icon="users"/>
      <MetricCard label="本地公开帖子" value={formatNumber(postTotal)} meta="SQLite 业务数据源" icon="file" tone="blue"/>
      <MetricCard label="保留历史版本" value={formatNumber(revisionTotal)} meta="原帖更新时追加版本" icon="calendar" tone="amber"/>
      <MetricCard label="已建立索引" value={`${indexedCount} / ${persons.length}`} meta="每个人物独立代次" icon="layers" tone="purple"/>
    </section>
    <section className="work-grid people-work-grid">
      <div className="panel person-list-panel">
        <header className="panel-header"><div><h2>人物列表</h2><p>当前只显示本地数据库中的真实人物和账号。</p></div><div className="compact-filter"><Icon name="search"/><input value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="筛选人物" placeholder="搜索人物或账号 ID"/></div></header>
        <div className="person-table-head" aria-hidden="true"><span>人物</span><span>本地内容</span><span>索引</span><span></span></div>
        <div className="person-rows">
          {filtered.map((person) => <button key={person.id} className={`person-row ${active?.id === person.id ? "is-active" : ""}`} onClick={() => setActiveId(person.id)}>
            <span className="person-cell person-main"><Avatar person={person}/><span><strong>{person.name}</strong><small>{person.alias}</small><em>{person.account ? <><b>{person.platform === "xueqiu" ? "雪球" : person.platform}</b> @{person.account}</> : "尚未绑定账号"}</em></span></span>
            <span className="person-cell numeric"><strong>{formatNumber(person.posts)}</strong><small>{formatNumber(person.revisions)} 个版本</small></span>
            <span className="person-cell"><StatusPill tone={person.indexHead ? statusTone(person.indexHead.status === "degraded" ? "partial" : "succeeded") : "neutral"}>{person.indexHead ? (person.retrievalMode === "hybrid" ? "混合索引" : "仅全文") : "等待索引"}</StatusPill></span>
            <Icon name="chevron" className="row-chevron"/>
          </button>)}
          {!filtered.length && <div className="empty-compact people-empty"><Icon name="users"/><strong>{persons.length ? "没有匹配的人物" : "还没有人物知识库"}</strong><p>{persons.length ? "清除筛选条件后重试。" : "添加人物并绑定雪球数字用户 ID。"}</p></div>}
        </div>
      </div>
      {active && <aside className="panel person-detail-panel">
        <header className="person-detail-head"><Avatar person={active} size="lg"/><div><p className="eyebrow">当前人物</p><h2>{active.name}</h2><span>{active.alias}</span></div></header>
        <div className="identity-check"><Icon name="shield"/><span><strong>{active.accountStatus}</strong><small>仅归档公开内容，不保存浏览器 Cookie</small></span></div>
        <dl className="detail-list">
          <div><dt>平台账号</dt><dd>{active.account ? <><span className="platform-badge">{active.platform === "xueqiu" ? "雪球" : active.platform}</span><span className="mono">{active.account}</span></> : "未绑定"}</dd></div>
          <div><dt>帖子 / 版本</dt><dd>{formatNumber(active.posts)} / {formatNumber(active.revisions)}</dd></div>
          <div><dt>当前索引</dt><dd>{active.indexHead ? active.retrievalMode : "尚未建立"}</dd></div>
          <div><dt>人物更新</dt><dd>{formatDateTime(active.updatedAt)}</dd></div>
        </dl>
        <div className="coverage-mini"><div><strong>知识库状态</strong><span>{active.indexHead ? "可检索" : active.posts ? "等待构建" : "等待内容"}</span></div><div className="coverage-line"><i style={{ width: active.indexHead ? "100%" : active.posts ? "58%" : "4%" }}></i></div><small>{active.indexHead ? `当前代次 ${active.indexHead.generation_id}` : active.posts ? "已有帖子，尚未建立检索代次。" : "完成首次采集后才能构建知识库索引。"}</small></div>
        <div className="detail-actions"><Button variant="secondary" icon="eye" onClick={() => onNavigate("person-detail", active.id)}>查看详情</Button><Button icon="collect" onClick={() => onNavigate("collect", active.id)}>采集内容</Button></div>
      </aside>}
    </section>
  </div>;
}

function AddPersonModal({ open, onClose, persons, onCreate, showToast }) {
  const [name, setName] = useState("");
  const [alias, setAlias] = useState("");
  const [account, setAccount] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  useEffect(() => { if (open) { setName(""); setAlias(""); setAccount(""); setConfirmed(false); setSubmitted(false); setFailure(""); } }, [open]);
  const duplicate = persons.some((person) => person.account === account.trim());
  const numeric = /^\d+$/.test(account.trim());
  const valid = name.trim() && numeric && confirmed && !duplicate;
  const submit = async () => {
    setSubmitted(true);
    if (!valid || busy) return;
    setBusy(true); setFailure("");
    try {
      await onCreate({ name: name.trim(), aliases: alias.trim() ? [alias.trim()] : [], account: account.trim() });
      onClose();
      showToast("人物与雪球账号已建立", "下一步可创建首次采集任务。", "success");
    } catch (error) {
      setFailure(`${error.code ? `${error.code}：` : ""}${error.message}`);
    } finally { setBusy(false); }
  };
  return <Modal open={open} onClose={onClose} title="添加人物与平台账号" description="同一人物后续可继续绑定其他平台账号，内容统一归入该人物知识库。" footer={<><Button variant="ghost" onClick={onClose}>暂不添加</Button><Button icon="personAdd" disabled={busy} onClick={submit}>{busy ? "正在建立" : "建立人物知识库"}</Button></>}>
    <div className="form-section"><div className="form-section-number">1</div><div><h3>人物信息</h3><p>使用便于本机识别的名称，不要求与平台昵称相同。</p><div className="two-fields"><label className="field"><span>人物名称 <b>必填</b></span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：冰冰小美"/>{submitted && !name.trim() && <em className="field-error">请输入人物名称</em>}</label><label className="field"><span>研究标签</span><input value={alias} onChange={(event) => setAlias(event.target.value)} placeholder="例如：情绪体系研究"/></label></div></div></div>
    <div className="form-section"><div className="form-section-number">2</div><div><h3>首个平台账号</h3><p>MVP 先支持雪球，账号必须使用数字用户 ID。</p><div className="two-fields"><label className="field"><span>平台</span><div className="select-wrap"><select disabled><option>雪球</option></select><Icon name="chevron"/></div><small>微博等平台将在后续适配。</small></label><label className="field"><span>雪球数字用户 ID <b>必填</b></span><input className="mono" value={account} onChange={(event) => setAccount(event.target.value)} inputMode="numeric" placeholder="例如：1000000000"/>{submitted && !numeric && <em className="field-error">请输入纯数字雪球用户 ID</em>}{duplicate && <em className="field-error">此账号已绑定到其他人物</em>}</label></div></div></div>
    <label className={`confirm-box ${confirmed ? "is-checked" : ""}`}><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}/><i>{confirmed && <Icon name="check" size={15}/>}</i><span><strong>确认仅用于本机公开资料研究</strong><small>只采集公开帖子；登录态、Cookie、密钥和私人样本不写入项目。</small></span></label>
    {submitted && !confirmed && <p className="form-global-error"><Icon name="alert"/>需要确认本机研究用途后才能建立账号。</p>}
    {failure && <p className="form-global-error"><Icon name="alert"/>{failure}</p>}
  </Modal>;
}

const COLLECTION_STEPS = [
  { id: "draft", label: "设置范围" }, { id: "checked", label: "检查本地" },
  { id: "pending", label: "等待领取" }, { id: "running", label: "采集中" },
  { id: "completed", label: "完成" },
];

function collectionVisualState(job, checked) {
  if (!job) return checked ? "checked" : "draft";
  if (job.status === "pending_codex") return "pending";
  if (["claimed", "running"].includes(job.status)) return "running";
  if (job.status === "succeeded") return "completed";
  if (job.status === "cancelled") return "cancelled";
  if (job.status === "failed") return "failed";
  return "waiting";
}

function intervalDays(interval) {
  if (!interval?.start_at || !interval?.end_at) return 0;
  return Math.max(0, Math.round((Date.parse(interval.end_at) - Date.parse(interval.start_at)) / 86400000));
}

function CollectionPage({ persons, onRefresh, showToast, onNavigate }) {
  const eligible = persons.filter((person) => person.accounts.length);
  const [personId, setPersonId] = useState(eligible[0]?.id || "");
  const [accountId, setAccountId] = useState(eligible[0]?.accounts[0]?.account_id || "");
  const [mode, setMode] = useState("normal");
  const [startDate, setStartDate] = useState(daysAgo(30));
  const [endDate, setEndDate] = useState(isoDay(new Date()));
  const [coverage, setCoverage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [currentJob, setCurrentJob] = useState(null);
  const [collection, setCollection] = useState(null);
  const person = persons.find((item) => item.id === personId) || eligible[0] || null;
  const account = person?.accounts.find((item) => item.account_id === accountId) || person?.accounts[0] || null;
  const loadCollection = useCallback(async () => {
    if (!person?.id) return;
    const payload = await API.get(`/api/persons/${encodeURIComponent(person.id)}/collection-summary`);
    setCollection(payload.collection);
  }, [person?.id]);
  useEffect(() => {
    if (!person && eligible[0]) setPersonId(eligible[0].id);
  }, [person, eligible]);
  useEffect(() => {
    if (person?.accounts.length && !person.accounts.some((item) => item.account_id === accountId)) setAccountId(person.accounts[0].account_id);
  }, [person?.id, accountId]);
  useEffect(() => { loadCollection().catch((error) => showToast("采集摘要加载失败", error.message, "warning")); }, [loadCollection]);
  useEffect(() => {
    const latest = collection?.accounts?.find((item) => item.account_id === account?.account_id)?.jobs?.[0] || null;
    setCurrentJob(latest);
  }, [collection, account?.account_id]);
  const state = collectionVisualState(currentJob, Boolean(coverage));
  const normalizedStep = ["waiting", "failed"].includes(state) ? "running" : state === "cancelled" ? "pending" : state;
  const stepIndex = COLLECTION_STEPS.findIndex((step) => step.id === normalizedStep);
  const totalDays = coverage ? intervalDays(coverage.request) : Math.max(1, Math.round((Date.parse(`${endDate}T00:00:00`) - Date.parse(`${startDate}T00:00:00`)) / 86400000) + 1);
  const coveredDays = coverage ? coverage.covered.reduce((sum, item) => sum + intervalDays(item), 0) : 0;
  const missingDays = coverage ? coverage.missing.reduce((sum, item) => sum + intervalDays(item), 0) : totalDays;
  const coveredWidth = coverage && totalDays ? Math.min(100, coveredDays / totalDays * 100) : 0;
  const remoteActions = currentJob?.remote_action_count || (coverage && !coverage.proof_complete ? coverage.missing.length : 0);
  const locked = ["pending", "running"].includes(state);

  const withBusy = async (action) => {
    if (busy) return;
    setBusy(true);
    try { await action(); }
    catch (error) { showToast(error.code || "操作失败", error.message, "warning"); }
    finally { setBusy(false); }
  };
  const checkCoverage = () => withBusy(async () => {
    if (!person || !startDate || !endDate || endDate < startDate) throw new Error("请选择有效的人物与日期范围");
    if (!account) throw new Error("请选择平台账号");
    const query = new URLSearchParams({ account_id: account.account_id, start_date: startDate, end_date: endDate });
    const payload = await API.get(`/api/persons/${encodeURIComponent(person.id)}/coverage?${query}`);
    setCoverage(payload.coverage); setCurrentJob(null);
    showToast("本地覆盖检查完成", payload.coverage.proof_complete ? "请求区间已有完整覆盖，不会创建远端动作。" : `发现 ${payload.coverage.missing.length} 个待采集区间。`);
  });
  const createTask = () => withBusy(async () => {
    if (!person) throw new Error("没有可用的平台账号");
    if (!account) throw new Error("请选择平台账号");
    const payload = await API.post("/api/collection-jobs", { account_id: account.account_id, mode, start_date: startDate, end_date: endDate });
    setCurrentJob(payload.job); await Promise.all([onRefresh(), loadCollection()]);
    showToast("采集任务已创建", payload.job.status === "succeeded" ? "本地覆盖完整，未产生远端动作。" : "任务正在等待当前 Codex 桌面任务领取。");
  });
  const reloadJob = () => withBusy(async () => {
    await Promise.all([onRefresh(), loadCollection()]);
  });
  const cancelJob = () => withBusy(async () => {
    const payload = await API.post(`/api/collection-jobs/${encodeURIComponent(currentJob.job_id)}/cancel`, {});
    setCurrentJob(payload.job); await Promise.all([onRefresh(), loadCollection()]); showToast("采集任务已取消", "当前交接凭据已经撤销。", "warning");
  });
  const resumeJob = () => withBusy(async () => {
    const payload = await API.post(`/api/collection-jobs/${encodeURIComponent(currentJob.job_id)}/resume`, {});
    setCurrentJob(payload.job); await Promise.all([onRefresh(), loadCollection()]); showToast("任务已重新开放领取", "已签发新的交接凭据。");
  });
  const selectAccount = (value) => { const next = eligible.find((item) => item.accounts.some((account) => account.account_id === value)); if (next) setPersonId(next.id); setAccountId(value); setCoverage(null); setCurrentJob(null); };

  if (!eligible.length) return <div className="page page-collect"><PageIntro eyebrow="手动批量采集 MVP" title="采集交接" description="先添加人物并绑定雪球账号，再创建采集任务。"/><section className="ask-empty panel"><span className="ask-orbit"><Icon name="personAdd" size={26}/></span><h2>没有可采集的平台账号</h2><p>人物页完成账号绑定后，这里会显示真实覆盖预检与任务交接。</p><Button icon="users" onClick={() => onNavigate("people")}>前往人物与账号</Button></section></div>;

  return <div className="page page-collect">
    <PageIntro eyebrow="手动批量采集 MVP" title="采集交接" description="网页检查本地缺口并生成交接任务；当前 Codex 桌面任务在已登录浏览器中执行采集。" actions={<Button variant="secondary" icon="refresh" disabled={busy} onClick={reloadJob}>刷新任务状态</Button>}/>
    <div className="boundary-banner"><Icon name="info"/><div><strong>网页不会直接启动 Codex，也不保存 Cookie</strong><p>交接任务只包含人物、平台账号、时间范围和短期凭据。登录与验证保留在 Codex 内置浏览器中。</p></div></div>
    <CollectionStepper steps={COLLECTION_STEPS} currentId={normalizedStep}/>
    <section className="collection-layout">
      <div className="panel collection-config">
        <header className="panel-header"><div><p className="eyebrow">任务请求</p><h2>采集范围</h2></div><StatusPill tone={mode === "recheck" ? "info" : "neutral"}>{mode === "recheck" ? "追加复查" : "普通采集"}</StatusPill></header>
        <label className="field"><span>人物与账号</span><div className="select-wrap"><select value={account?.account_id || ""} disabled={locked} onChange={(event) => selectAccount(event.target.value)}>{eligible.flatMap((item) => item.accounts.map((itemAccount) => <option key={itemAccount.account_id} value={itemAccount.account_id}>{item.name} · {itemAccount.platform} @{itemAccount.external_user_id}</option>))}</select><Icon name="chevron"/></div></label>
        <div className="two-fields"><label className="field"><span>开始日期</span><input type="date" value={startDate} disabled={locked} onChange={(event) => { setStartDate(event.target.value); setCoverage(null); }}/></label><label className="field"><span>结束日期</span><input type="date" value={endDate} disabled={locked} onChange={(event) => { setEndDate(event.target.value); setCoverage(null); }}/></label></div>
        <fieldset className="segmented-field"><legend>采集方式</legend><label className={mode === "normal" ? "is-selected" : ""}><input type="radio" checked={mode === "normal"} disabled={locked} onChange={() => { setMode("normal"); setCoverage(null); }}/><span><strong>普通采集</strong><small>完整覆盖段不访问远端</small></span></label><label className={mode === "recheck" ? "is-selected" : ""}><input type="radio" checked={mode === "recheck"} disabled={locked} onChange={() => { setMode("recheck"); setCoverage(null); }}/><span><strong>追加复查</strong><small>写入新版本和观察记录</small></span></label></fieldset>
        <div className="range-code"><Icon name="calendar"/><span><small>请求日期范围</small><strong className="mono">{startDate && endDate ? `${startDate} — ${endDate}` : "请选择完整日期"}</strong></span></div>
        <Button icon="search" className="full-button" disabled={busy || locked || !startDate || !endDate || endDate < startDate} onClick={checkCoverage}>{busy ? "正在处理" : "检查本地覆盖与缺口"}</Button>
      </div>
      <div className="panel coverage-panel">
        <header className="panel-header"><div><p className="eyebrow">本地覆盖预检</p><h2>{person.name} · {account?.platform || "账号"}</h2></div>{coverage ? <StatusPill tone={coverage.proof_complete ? "success" : "warning"}>{coverage.proof_complete ? "完整覆盖" : "存在缺口"}</StatusPill> : <StatusPill>尚未检查</StatusPill>}</header>
        <div className="coverage-legend"><span><i className="legend-local"></i>已证明覆盖</span><span><i className="legend-gap"></i>实际缺口</span><span><i className="legend-request"></i>本次请求</span></div>
        <div className="coverage-map" aria-label="覆盖区间示意"><div className="coverage-axis"><span>{startDate.slice(5) || "—"}</span><span></span><span></span><span>{endDate.slice(5) || "—"}</span></div><div className="coverage-request"><i></i></div><div className="coverage-segments"><i className="segment-gap" style={{ left: 0, width: coverage ? "100%" : "0%" }}></i><i className="segment-local" style={{ left: 0, width: `${coveredWidth}%` }}></i></div></div>
        {!coverage ? <div className="empty-compact"><Icon name="search"/><strong>先检查本地资料</strong><p>系统将读取真实覆盖证明并计算待采集区间。</p></div> : <><div className="coverage-summary"><div><small>已证明覆盖</small><strong>{coveredDays} 天</strong><span>{coverage.covered.length} 个区间</span></div><div><small>实际待采集</small><strong>{missingDays} 天</strong><span>{coverage.missing.length} 个缺口</span></div><div><small>预计远端动作</small><strong>{mode === "recheck" ? Math.max(1, coverage.missing.length) : coverage.missing.length} 段</strong><span>{coverage.proof_complete && mode === "normal" ? "remote_action_count=0" : "只访问必要区间"}</span></div></div><div className={`proof-box ${coverage.proof_complete ? "" : "proof-warning"}`}><Icon name={coverage.proof_complete ? "shield" : "alert"}/><div><strong>{coverage.proof_complete ? "请求区间已有完整覆盖证明" : "存在需要采集的真实缺口"}</strong><p>{coverage.proof_complete && mode === "normal" ? "普通采集不会创建远端动作；需要复查时切换为追加复查。" : "创建任务后由当前 Codex 桌面任务领取并执行。"}</p></div></div></>}
      </div>
      <TaskHandoffPanel state={state} job={currentJob} coverage={coverage} busy={busy} onCreate={createTask} onCopy={() => navigator.clipboard?.writeText(currentJob?.handoff_instruction || "")} onRefresh={reloadJob} onCancel={cancelJob} onResume={resumeJob} onReset={() => { setCoverage(null); setCurrentJob(null); }} onNavigate={onNavigate} remoteActions={remoteActions}/>
    </section>
  </div>;
}

function TaskHandoffPanel({ state, job, coverage, busy, onCreate, onCopy, onRefresh, onCancel, onResume, onReset, onNavigate, remoteActions }) {
  const titles = { draft: "等待范围设置", checked: "可以创建任务", pending: "等待 Codex 领取", running: "Codex 正在采集", waiting: "任务需要处理", completed: "采集任务完成", cancelled: "采集任务已取消", failed: "采集任务失败" };
  const tone = state === "completed" ? "success" : ["failed", "cancelled"].includes(state) ? "error" : ["pending", "waiting"].includes(state) ? "warning" : state === "running" ? "info" : "neutral";
  const instruction = job?.handoff_instruction || "";
  const itemsSeen = Number(job?.items_seen || 0);
  const errorCode = job?.error?.code || job?.error || "—";
  return <div className={`panel handoff-panel handoff-${state}`}>
    <header className="panel-header"><div><p className="eyebrow">Codex 任务交接</p><h2>{titles[state]}</h2></div><StatusPill tone={tone}>{job ? statusLabel(job.status) : "本机任务"}</StatusPill></header>
    {state === "draft" && <div className="handoff-empty"><span><Icon name="terminal" size={26}/></span><strong>还没有交接任务</strong><p>先完成本地覆盖检查。网页只在确认需要访问远端时创建任务。</p></div>}
    {state === "checked" && <div className="handoff-ready"><div className="callout-row"><Icon name="check"/><span><strong>{coverage?.proof_complete ? "本地覆盖已确认" : "本地预检完成"}</strong><small>{coverage?.proof_complete ? "普通采集不会产生远端动作。" : `发现 ${coverage?.missing.length || 0} 个待采集区间。`}</small></span></div>{coverage?.proof_complete ? <div className="no-remote-action"><Icon name="shield"/><span><strong>本地已有，不执行采集</strong><small>如需定期复查，请切换「追加复查」。</small></span></div> : <Button icon="terminal" className="full-button" disabled={busy} onClick={onCreate}>创建 Codex 采集任务</Button>}</div>}
    {state === "pending" && <div className="handoff-pending"><div className="handoff-code"><span><small>一次性执行指令</small><code>{instruction || "等待交接凭据"}</code></span><button disabled={!instruction} onClick={onCopy} aria-label="复制执行指令"><Icon name="copy"/></button></div><p className="muted-copy">回到当前 Codex 桌面任务粘贴此指令。页面不会自行启动 Codex。</p><div className="handoff-meta"><span><small>状态</small><strong>{job?.display_status || statusLabel(job?.status)}</strong></span><span><small>远端动作</small><strong>{remoteActions}</strong></span></div><div className="button-row"><Button icon="copy" disabled={!instruction} onClick={onCopy}>复制执行指令</Button><Button variant="ghost" disabled={busy} onClick={onCancel}>取消任务</Button></div></div>}
    {state === "running" && <div className="handoff-running"><div className="live-line"><i></i><span><strong>Codex 已领取任务</strong><small>任务进度会在刷新后更新。</small></span></div><Progress value={Math.min(95, itemsSeen ? 45 + itemsSeen : 30)} label={`已观察 ${itemsSeen} 条 · ${remoteActions} 个远端动作`}/><div className="run-stats"><span><small>分段</small><strong>{job?.segment_count || 0}</strong></span><span><small>已观察</small><strong>{itemsSeen}</strong></span><span><small>状态</small><strong>{job?.display_status || statusLabel(job?.status)}</strong></span></div><Button variant="secondary" icon="refresh" className="full-button" disabled={busy} onClick={onRefresh}>刷新真实进度</Button></div>}
    {state === "waiting" && <div className="handoff-waiting"><div className="human-warning"><Icon name="alert"/><span><strong>{job?.display_status || statusLabel(job?.status)}</strong><p>需要人工验证、等待限流恢复或重新开放领取。</p></span></div><dl className="checkpoint-data"><div><dt>稳定错误</dt><dd className="mono">{String(errorCode)}</dd></div><div><dt>已观察</dt><dd>{itemsSeen} 条</dd></div></dl><div className="button-row"><Button icon="refresh" disabled={busy} onClick={onResume}>重新开放领取</Button><Button variant="secondary" disabled={busy} onClick={onRefresh}>刷新</Button></div></div>}
    {state === "completed" && <div className="handoff-completed"><div className="success-seal"><Icon name="check" size={30}/></div><h3>采集任务已完成</h3><p>任务结果已经提交本地数据库，可继续构建人物知识库索引。</p><div className="completion-grid"><span><small>状态</small><strong>{job?.display_status || statusLabel(job?.status)}</strong></span><span><small>远端动作</small><strong>{remoteActions}</strong></span><span><small>分段</small><strong>{job?.segment_count || 0}</strong></span><span><small>完成时间</small><strong>{formatDateTime(job?.updated_at)}</strong></span></div><Button icon="database" className="full-button" onClick={() => onNavigate("knowledge")}>查看知识库索引</Button></div>}
    {["cancelled", "failed"].includes(state) && <div className="handoff-empty handoff-cancelled"><span><Icon name={state === "failed" ? "alert" : "close"} size={26}/></span><strong>{titles[state]}</strong><p>{state === "failed" ? `稳定错误：${String(errorCode)}` : "当前交接凭据已经撤销。"}</p><Button variant="secondary" icon="refresh" onClick={onReset}>重新设置范围</Button></div>}
  </div>;
}

function KnowledgePage({ persons, onRefresh, showToast, onNavigate }) {
  const [selectedId, setSelectedId] = useState(persons[0]?.id || "");
  const [knowledge, setKnowledge] = useState(null);
  const [busyId, setBusyId] = useState("");
  const [materialQueryInput, setMaterialQueryInput] = useState("");
  const [materialQuery, setMaterialQuery] = useState("");
  const [materialPage, setMaterialPage] = useState(1);
  const [postDetails, setPostDetails] = useState({});
  const [detailLoading, setDetailLoading] = useState({});
  useEffect(() => { if (!persons.some((person) => person.id === selectedId)) setSelectedId(persons[0]?.id || ""); }, [persons, selectedId]);
  const selected = persons.find((person) => person.id === selectedId) || persons[0];
  useEffect(() => {
    setMaterialQueryInput("");
    setMaterialQuery("");
    setMaterialPage(1);
    setPostDetails({});
    setDetailLoading({});
  }, [selected?.id]);
  const loadKnowledge = useCallback(async () => {
    if (!selected?.id) return;
    const params = new URLSearchParams({ page: String(materialPage), page_size: "20" });
    if (materialQuery) params.set("q", materialQuery);
    const payload = await API.get(`/api/persons/${encodeURIComponent(selected.id)}/knowledge-base?${params.toString()}`);
    setKnowledge(payload.knowledge_base);
  }, [selected?.id, materialPage, materialQuery]);
  useEffect(() => { loadKnowledge().catch((error) => showToast("知识库加载失败", error.message, "warning")); }, [loadKnowledge]);
  const vectorConfigured = selected?.retrievalMode === "hybrid";
  const postTotal = persons.reduce((sum, person) => sum + person.posts, 0);
  const revisionTotal = persons.reduce((sum, person) => sum + person.revisions, 0);
  const indexedCount = persons.filter((person) => person.indexHead).length;
  const hybridCount = persons.filter((person) => person.retrievalMode === "hybrid").length;
  const latestJob = knowledge?.generations?.[0] || null;
  const activeJob = latestJob && ["pending", "running"].includes(latestJob.status);
  const materials = knowledge?.posts || [];
  const postPage = knowledge?.post_page;

  const loadPostDetails = async (post) => {
    if (!selected?.id || postDetails[post.post_key] || detailLoading[post.post_key]) return;
    setDetailLoading((current) => ({ ...current, [post.post_key]: true }));
    try {
      const payload = await API.get(`/api/persons/${encodeURIComponent(selected.id)}/posts/${encodeURIComponent(post.post_key)}`);
      setPostDetails((current) => ({ ...current, [post.post_key]: payload.post }));
    } catch (error) {
      showToast("帖子版本加载失败", error.message, "warning");
    } finally {
      setDetailLoading((current) => ({ ...current, [post.post_key]: false }));
    }
  };

  const searchMaterials = (event) => {
    event.preventDefault();
    setMaterialPage(1);
    setMaterialQuery(materialQueryInput.trim());
  };

  const rebuild = async () => {
    if (!selected || busyId) return;
    setBusyId(selected.id);
    try {
      const accepted = await API.post("/api/index-jobs", { person_id: selected.id });
      const terminal = await API.poll(accepted.status_url, (payload) => ["succeeded", "failed", "interrupted"].includes(payload.job.status), (payload) => {
      });
      showToast(terminal.job.status === "succeeded" ? "知识库索引已更新" : "索引任务未成功", terminal.job.status === "succeeded" ? `当前模式：${terminal.job.retrieval_mode}` : (terminal.job.error?.code || terminal.job.status), terminal.job.status === "succeeded" ? "success" : "warning");
      await Promise.all([onRefresh(), loadKnowledge()]);
    } catch (error) { showToast(error.code || "索引失败", error.message, "warning"); }
    finally { setBusyId(""); }
  };

  return <div className="page page-knowledge">
    <PageIntro eyebrow="全文 + embedding 混合检索" title="知识库索引" description="每个人物拥有独立知识库边界；帖子、历史版本和检索代次均来自本地真实数据。" actions={<><Button variant="secondary" icon="refresh" disabled={!selected || busyId || activeJob} onClick={rebuild}>{busyId ? "正在构建" : "重建当前人物索引"}</Button><Button icon="chat" onClick={() => onNavigate("ask")}>使用知识库问答</Button></>}/>
    {!vectorConfigured && <div className="degraded-banner"><Icon name="alert"/><div><strong>当前知识库使用全文检索</strong><p>当前代次未提供向量检索；回答会明确显示检索模式。</p></div><StatusPill tone="warning">fulltext_only</StatusPill></div>}
    <section className="metrics-grid knowledge-metrics">
      <MetricCard label="已入库帖子" value={formatNumber(postTotal)} meta={`${formatNumber(revisionTotal)} 个不可变版本`} icon="file"/>
      <MetricCard label="人物知识库" value={persons.length} meta="账号内容按人物合并" icon="users" tone="purple"/>
      <MetricCard label="已建立索引" value={`${indexedCount} / ${persons.length}`} meta="每个人物独立代次" icon="search" tone="blue"/>
      <MetricCard label="混合检索" value={`${hybridCount} / ${persons.length}`} meta={vectorConfigured ? "embedding 已配置" : "当前降级为仅全文"} icon="sparkle" tone={vectorConfigured ? "purple" : "amber"}/>
    </section>
    <section className="knowledge-layout">
      <div className="panel kb-people-panel">
        <header className="panel-header"><div><h2>人物知识库</h2><p>选择人物查看当前可服务代次和最近索引任务。</p></div><StatusPill tone="neutral">{persons.length} 人</StatusPill></header>
        <div className="kb-table-head"><span>人物与来源</span><span>内容</span><span>全文索引</span><span>向量索引</span><span>最近更新</span></div>
        <div className="kb-rows">{persons.map((person) => {
          const isBusy = selected?.id === person.id && activeJob;
          return <button type="button" className={`kb-row kb-row-button ${selected?.id === person.id ? "is-active" : ""}`} key={person.id} onClick={() => setSelectedId(person.id)}>
            <div className="kb-person"><Avatar person={person}/><span><strong>{person.name}</strong><small>{person.account ? <><b>雪球</b> @{person.account}</> : "未绑定账号"}</small></span></div>
            <div><strong>{formatNumber(person.posts)}</strong><small>{formatNumber(person.revisions)} 个版本</small></div>
            <div><StatusPill tone={person.indexHead ? "success" : "neutral"}>{person.indexHead ? "就绪" : "等待构建"}</StatusPill><small>{person.indexHead?.generation_id ? person.indexHead.generation_id.slice(0, 8) : "—"}</small></div>
            <div><StatusPill tone={!person.indexHead ? "neutral" : person.retrievalMode === "hybrid" ? "success" : "warning"}>{!person.indexHead ? "无代次" : person.retrievalMode === "hybrid" ? "混合检索" : "仅全文"}</StatusPill><small>{isBusy ? statusLabel(latestJob.status) : person.indexHead?.status || "—"}</small></div>
            <div><strong>{formatDateTime(person.indexHead?.completed_at)}</strong><small>{isBusy ? statusLabel(latestJob.status) : "查看代次详情"}</small></div>
          </button>;
        })}{!persons.length && <div className="empty-compact"><Icon name="database"/><strong>没有人物知识库</strong><p>先创建人物并归档公开帖子。</p></div>}</div>
      </div>
      <aside className="panel generation-panel">
        <header className="panel-header"><div><p className="eyebrow">当前可服务代次</p><h2 className="mono">{knowledge?.active_generation?.generation_id || "尚未建立"}</h2></div><StatusPill tone={knowledge?.active_generation ? (knowledge.active_generation.retrieval_mode === "hybrid" ? "success" : "warning") : "neutral"}>{knowledge?.active_generation ? (knowledge.active_generation.retrieval_mode === "hybrid" ? "混合检索就绪" : "全文检索就绪") : "等待构建"}</StatusPill></header>
        {(busyId || activeJob) && <div className="generation-progress"><Progress value={latestJob?.status === "running" ? 64 : 24} label={`索引任务 ${statusLabel(latestJob?.status || "pending")}`}/><p>任务完成前，已有代次继续提供检索。</p></div>}
        <dl className="provider-list"><div><dt>当前人物</dt><dd><strong>{selected?.name || "—"}</strong></dd></div><div><dt>检索模式</dt><dd><strong>{knowledge?.active_generation?.retrieval_mode || "not_indexed"}</strong><StatusPill tone={knowledge?.can_ask ? "success" : "warning"}>{knowledge?.can_ask ? "可服务" : "未建立"}</StatusPill></dd></div><div><dt>最近任务</dt><dd><strong>{latestJob?.display_status || statusLabel(latestJob?.status)}</strong>{latestJob?.error?.code && <small>{latestJob.error.code}</small>}</dd></div><div><dt>可问答</dt><dd><strong>{knowledge?.can_ask ? "是" : "否"}</strong><small>{knowledge?.next_action?.label || "等待状态"}</small></dd></div></dl>
        <div className="provider-note"><Icon name="shield"/><span>索引代次只保存模型指纹和检索模式；密钥不会写入数据库或项目。</span></div>
        <Button icon="refresh" className="full-button generation-action" disabled={!selected || busyId || activeJob} onClick={rebuild}>{busyId ? "正在构建索引" : "为当前人物重建索引"}</Button>
      </aside>
    </section>
    <section className="panel post-version-panel"><header className="panel-header material-header"><div><p className="eyebrow">帖子与版本</p><h2>当前人物的可审阅材料</h2><p>仅加载当前页；展开单条帖子后才读取其不可变版本全文。</p></div><StatusPill tone="neutral">{formatNumber(postPage?.total || 0)} 篇</StatusPill></header><form className="material-toolbar" onSubmit={searchMaterials}><label className="material-search"><Icon name="search" size={16}/><input value={materialQueryInput} onChange={(event) => setMaterialQueryInput(event.target.value)} placeholder="搜索主题、摘要或帖子 ID" aria-label="搜索当前人物的帖子"/></label><Button variant="secondary" icon="search" type="submit">搜索</Button>{materialQuery && <button type="button" className="text-button" onClick={() => { setMaterialQueryInput(""); setMaterialQuery(""); setMaterialPage(1); }}>清除筛选</button>}</form>{materials.length ? <><div className="post-version-list">{materials.map((post) => { const detail = postDetails[post.post_key]; const loading = detailLoading[post.post_key]; return <details key={post.post_key} onToggle={(event) => event.currentTarget.open && loadPostDetails(post)}><summary><span className="material-summary"><small><b>{post.platform}</b> · 帖子 {post.external_post_id} · {formatDateTime(post.published_at)}</small><strong>{post.title}</strong><em>{post.summary}</em><small>{post.version_count} 个版本 · {post.observation?.status || "未观察"}</small></span><Icon name="chevron"/></summary><div className="material-detail">{loading && <p className="material-loading">正在读取完整版本正文…</p>}{detail && <div className="version-list">{detail.versions.map((version, index) => <article key={`${post.post_key}-${index}`}><small>版本 {detail.versions.length - index} · {formatDateTime(version.captured_at)}</small><p>{version.content || "原帖文本为空"}</p></article>)}</div>}{post.source_url && <a href={post.source_url} target="_blank" rel="noreferrer">查看来源原帖 <Icon name="external" size={14}/></a>}</div></details>; })}</div><footer className="material-pagination"><span>第 {postPage?.page || 1} / {postPage?.total_pages || 1} 页 · 共 {formatNumber(postPage?.total || 0)} 篇</span><div className="button-row"><Button variant="ghost" disabled={!postPage?.has_previous} onClick={() => setMaterialPage((current) => Math.max(1, current - 1))}>上一页</Button><Button variant="secondary" disabled={!postPage?.has_next} onClick={() => setMaterialPage((current) => current + 1)}>下一页</Button></div></footer></> : <EmptyState icon={materialQuery ? "search" : "file"} title={materialQuery ? "没有匹配的帖子" : "尚未归档帖子"} description={materialQuery ? "尝试调整关键词，或按帖子 ID 搜索。" : "完成采集并提交结果后，帖子与版本会显示在这里。"}/>}</section>
  </div>;
}

function AskPage({ persons, capabilities, showToast }) {
  const [selectedIds, setSelectedIds] = useState([]);
  const [query, setQuery] = useState("");
  const [stage, setStage] = useState("idle");
  const [retrieval, setRetrieval] = useState(null);
  const [question, setQuestion] = useState(null);
  const [evidence, setEvidence] = useState([]);
  const [activeCitation, setActiveCitation] = useState("");
  const [failure, setFailure] = useState("");
  useEffect(() => { if (!selectedIds.length && persons.length) setSelectedIds(persons.slice(0, 2).map((person) => person.id)); }, [persons]);
  const selectedPersons = persons.filter((person) => selectedIds.includes(person.id));
  const retrievalMode = retrieval?.retrieval_mode || capabilities?.embedding?.mode || (capabilities?.embedding?.configured ? "hybrid" : "fulltext_only");
  const togglePerson = (id) => { setSelectedIds((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < 10 ? [...current, id] : current); setStage("idle"); setQuestion(null); setEvidence([]); };
  const selectCitation = (id) => { setActiveCitation(id); document.querySelector(`[data-evidence='${id}']`)?.scrollIntoView({ behavior: "smooth", block: "nearest" }); };

  const start = async () => {
    if (!query.trim()) return showToast("还没有研究问题", "输入问题后再开始检索。", "warning");
    if (!selectedIds.length) return showToast("至少选择一个人物", "单次最多选择 10 个人物。", "warning");
    setFailure(""); setQuestion(null); setEvidence([]); setStage("retrieving");
    try {
      const created = await API.post("/api/questions", { query: query.trim(), person_ids: selectedIds, limit: 20, min_hits_per_person: 1 });
      const completed = await API.poll(created.question.status_url, (payload) => !["retrieving", "pending", "running"].includes(payload.question.status), (payload) => {
        setQuestion({ ...payload.question, status_url: created.question.status_url });
        setRetrieval({ retrieval_mode: payload.question.retrieval_mode });
      });
      const next = { ...completed.question, status_url: created.question.status_url };
      setQuestion(next); setRetrieval({ retrieval_mode: next.retrieval_mode });
      if (["failed", "interrupted"].includes(next.status)) throw new Error(next.error?.code || "检索运行未成功");
      const frozen = await API.get(next.evidence_url || created.question.evidence_url);
      setEvidence(frozen.bundle?.evidence || []); setActiveCitation(frozen.bundle?.evidence?.[0]?.evidence_id || "");
      setStage(next.status === "succeeded" ? "ready" : next.status === "citation_invalid" ? "invalid" : "answering");
    } catch (error) { setFailure(`${error.code ? `${error.code}：` : ""}${error.message}`); setStage("idle"); showToast("问答运行创建失败", error.message, "warning"); }
  };
  const refreshQuestion = async () => {
    if (!question?.status_url) return;
    try {
      setStage("validating");
      const payload = await API.get(question.status_url);
      const next = { ...question, ...payload.question };
      setQuestion(next);
      if (next.status === "succeeded") setStage("ready");
      else if (next.status === "citation_invalid") setStage("invalid");
      else if (next.status === "failed") { setFailure(next.error?.code || "回答运行失败"); setStage("idle"); }
      else setStage("answering");
    } catch (error) { setFailure(error.message); setStage("answering"); }
  };

  return <div className="page page-ask">
    <PageIntro eyebrow="一份回答，分别标注人物观点" title="多人物知识问答" description={retrievalMode === "fulltext_only" ? "当前以全文索引召回真实证据，再交给当前 Codex 桌面任务组织回答。" : "使用全文与向量混合召回真实证据，再交给当前 Codex 桌面任务组织回答。"} actions={<><StatusPill tone={retrievalMode === "hybrid" ? "success" : "warning"} icon={retrievalMode === "hybrid" ? "layers" : "alert"}>检索：{retrievalMode === "hybrid" ? "混合" : "仅全文"}</StatusPill><StatusPill tone="info" icon="sparkle">回答：当前 Codex 任务</StatusPill></>}/>
    {retrievalMode === "fulltext_only" && <UncertaintyNotice kind="degraded" title="embedding 尚未配置" message="当前只使用全文命中的证据，不会宣称语义检索已经生效。"/>}
    <QuestionComposer query={query} onQueryChange={(value) => { setQuery(value); setStage("idle"); }} persons={persons} selectedIds={selectedIds} onTogglePerson={togglePerson} retrievalMode={retrievalMode} onSubmit={start} busy={["retrieving", "validating"].includes(stage)}/>
    {failure && <div className="proof-box proof-warning ask-failure"><Icon name="alert"/><div><strong>运行失败</strong><p>{failure}</p></div></div>}
    {stage === "idle" && <section className="ask-empty panel"><span className="ask-orbit"><Icon name="search" size={26}/></span><h2>从人物材料中寻找可追溯答案</h2><p>系统只使用当前所选人物的本地公开帖子；证据不足时保留空白。</p><div className="suggestion-list"><button onClick={() => setQuery("比较他们对流动性确认的判断顺序。")}>比较他们对流动性确认的判断顺序<Icon name="arrow"/></button><button onClick={() => setQuery("他们如何定义观察仓与进攻仓的边界？")}>他们如何定义观察仓与进攻仓的边界<Icon name="arrow"/></button><button onClick={() => setQuery("哪些观点有直接证据，哪些仍缺少材料？")}>区分有证据观点与材料空白<Icon name="arrow"/></button></div></section>}
    {stage === "retrieving" && <AnswerProgress stage="retrieving" selectedPersons={selectedPersons} retrievalMode={retrievalMode} evidenceCount={0}/>}
    {["answering", "validating"].includes(stage) && <><AnswerProgress stage={stage} selectedPersons={selectedPersons} retrievalMode={retrievalMode} evidenceCount={evidence.length}/><section className="panel codex-handoff"><div><p className="eyebrow">当前 Codex 桌面任务</p><h2>证据包已经冻结，等待结构化回答</h2><p>{question?.codex_instruction}</p><div className="handoff-code"><span><small>固定交接指令</small><code>{question?.codex_instruction || "正在创建问答运行"}</code></span><button onClick={() => navigator.clipboard?.writeText(question?.codex_instruction || "")}><Icon name="copy"/></button></div></div><div className="button-row"><Button variant="secondary" icon="refresh" onClick={refreshQuestion}>刷新回答状态</Button><StatusPill tone="warning">{statusLabel(question?.status || "pending_codex")}</StatusPill></div></section><EvidenceRail evidence={evidence} persons={persons} activeId={activeCitation} onSelect={selectCitation} formatTimestamp={formatDateTime}/></>}
    {stage === "ready" && question?.result && <AnswerSummary query={query} result={question.result} evidence={evidence} persons={persons} activeCitation={activeCitation} onSelect={selectCitation} formatTimestamp={formatDateTime}/>}
    {stage === "invalid" && <section className="panel answer-invalid"><span><Icon name="alert" size={28}/></span><p className="eyebrow">citation_invalid</p><h2>引用归属校验未通过</h2><p>后端拒绝了人物、帖子版本或摘录不一致的回答。</p><Button variant="secondary" icon="refresh" onClick={() => setStage("idle")}>返回重新检索</Button></section>}
  </div>;
}

function Capability({ icon, title, detail, status, tone }) {
  return <article><span className={`capability-icon tone-${tone}`}><Icon name={icon}/></span><div><strong>{title}</strong><small>{detail}</small></div><StatusPill tone={tone}>{status}</StatusPill></article>;
}

function RuntimePage({ onRefresh, showToast }) {
  const [system, setSystem] = useState(null);
  const loadSystem = useCallback(async () => { const payload = await API.get("/api/system"); setSystem(payload.system); }, []);
  useEffect(() => { loadSystem().catch((error) => showToast("系统状态加载失败", error.message, "warning")); }, [loadSystem]);
  const capabilities = system?.health || {};
  const embedding = capabilities.embedding || {};
  const answer = capabilities.openai_compatible_answer || {};
  const degraded = !embedding.configured;
  const activities = (system?.activity || []).map((item) => ({ type: item.type, created_at: item.updated_at, title: `${item.type === "collection" ? "采集任务" : "索引任务"} · ${item.display_status}`, meta: item.type === "collection" ? `${item.remote_action_count || 0} 个远端动作` : `${item.person_name || "人物知识库"} · ${item.retrieval_mode || "等待构建"}`, tone: statusTone(item.status) }));
  const refresh = async () => { try { await Promise.all([onRefresh(), loadSystem()]); showToast("运行状态已刷新", "所有状态均来自本地系统摘要。"); } catch (error) { showToast("刷新失败", error.message, "warning"); } };
  return <div className="page page-runtime">
    <PageIntro eyebrow="本机职责与依赖" title="运行状态" description="将网页、采集执行、检索和回答拆开显示；每项状态均来自本地真实能力 API。" actions={<Button variant="secondary" icon="refresh" onClick={refresh}>重新检查连接</Button>}/>
    <section className="runtime-flow panel"><header className="panel-header"><div><h2>当前运行链路</h2><p>网页管理本地资料；Codex 桌面任务负责浏览器采集和当前模式回答。</p></div><StatusPill tone={degraded ? "warning" : "success"}>{degraded ? "可用 · 全文降级" : "混合检索可用"}</StatusPill></header><div className="flow-nodes"><article><span><Icon name="server"/></span><strong>本地网页</strong><small>建人物、建任务、看结果</small><StatusPill tone={capabilities?.resource_api === "ready" ? "success" : "error"}>{capabilities?.resource_api || "未知"}</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="terminal"/></span><strong>Codex 任务桥接</strong><small>领取采集与回答任务</small><StatusPill tone={capabilities?.codex_task === "available" ? "success" : "warning"}>{capabilities?.codex_task || "未知"}</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="collect"/></span><strong>内置浏览器</strong><small>沿用登录态、人工验证</small><StatusPill tone="info">由 Codex 管理</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="database"/></span><strong>本地知识库</strong><small>{degraded ? "SQLite + 全文索引" : "SQLite + 混合索引"}</small><StatusPill tone={degraded ? "warning" : "success"}>{degraded ? "仅全文" : "混合检索"}</StatusPill></article></div></section>
    <section className="runtime-grid">
      <div className="panel capability-panel"><header className="panel-header"><div><h2>能力检查</h2><p>各组件独立报告，不用单一「系统正常」掩盖降级。</p></div></header><div className="capability-list"><Capability icon="server" title="本地资源 API" detail="127.0.0.1 · 单用户" status={capabilities.resource_api || "未知"} tone={capabilities.resource_api === "ready" ? "success" : "error"}/><Capability icon="terminal" title="Codex 任务桥接" detail="采集与当前任务回答入口" status={capabilities.codex_task || "未知"} tone={capabilities.codex_task === "available" ? "success" : "warning"}/><Capability icon="search" title="索引任务" detail="本地任务由系统摘要聚合" status={capabilities.index_jobs || "未知"} tone={capabilities.index_jobs === "ready" ? "success" : "warning"}/><Capability icon="sparkle" title="Embedding provider" detail={`OpenAI-compatible · ${embedding.mode || "未配置"}`} status={embedding.configured ? "已配置" : "未配置"} tone={embedding.configured ? "success" : "warning"}/><Capability icon="chat" title="回答 provider" detail="当前 Codex 任务优先，保留兼容 API 扩展" status={answer.configured ? "API 已配置" : "Codex 模式"} tone={answer.configured ? "success" : "info"}/><Capability icon="collect" title="定时增量采集" detail="MVP 稳定后再增加随机执行窗口" status={capabilities.scheduled_collection || "未知"} tone="neutral"/></div></div>
      <div className="panel activity-panel"><header className="panel-header"><div><h2>最近运行记录</h2><p>采集和索引任务均读取本地数据库。</p></div><StatusPill tone="neutral">最近 {activities.length} 条</StatusPill></header><div className="activity-list">{activities.map((item, index) => <article key={`${item.type}-${index}`}><i className={`activity-dot ${item.tone}`}></i><time>{formatDateTime(item.created_at)}</time><span><strong>{item.title}</strong><small>{item.meta}</small></span><Icon name="chevron" size={15}/></article>)}{!activities.length && <div className="empty-compact"><Icon name="activity"/><strong>暂无运行记录</strong><p>创建采集或索引任务后会显示在这里。</p></div>}</div><div className="runtime-boundary"><Icon name="shield"/><div><strong>本地数据边界</strong><p>Cookie 留在浏览器；API 密钥来自环境变量；项目只保存公开帖子、索引和任务状态。</p></div></div></div>
    </section>
    <section className="panel extension-panel"><div><span className="extension-icon"><Icon name="layers" size={22}/></span><div><p className="eyebrow">后续扩展口</p><h2>可替换的 RAG 与回答 provider</h2><p>配置本地或云端 OpenAI-compatible 服务后，现有人物、证据和引用契约保持不变。</p></div></div><dl><div><dt>Embedding</dt><dd><code>VOICEVAULT_EMBEDDING_BASE_URL</code></dd></div><div><dt>回答模型</dt><dd><code>VOICEVAULT_ANSWER_BASE_URL</code></dd></div><div><dt>凭据</dt><dd><code>环境变量 · 不入库</code></dd></div></dl></section>
  </div>;
}

function App() {
  const [page, setPage] = useState("workspace");
  const [selectedPersonId, setSelectedPersonId] = useState("");
  const [resources, setResources] = useState({ workspace: null, persons: [], collectionJobs: [], indexJobs: [], capabilities: null });
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastSync, setLastSync] = useState(null);
  const [addOpen, setAddOpen] = useState(false);
  const [toast, setToast] = useState(null);
  const showToast = (title, detail, tone = "success") => setToast({ title, detail, tone, key: Date.now() });
  const refreshAll = useCallback(async () => {
    setError("");
    try {
      const [workspace, people, collections, indexes, capability] = await Promise.all([
        API.get("/api/workspace"), API.get("/api/people"), API.get("/api/collection-jobs"), API.get("/api/index-jobs"), API.get("/api/capabilities"),
      ]);
      setResources({ workspace: workspace.workspace || null, persons: people.people || [], collectionJobs: collections.jobs || [], indexJobs: indexes.jobs || [], capabilities: capability.capabilities || {} });
      setConnected(true); setLastSync(new Date());
    } catch (failure) { setConnected(false); setError(`${failure.code ? `${failure.code}：` : ""}${failure.message}`); throw failure; }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { refreshAll().catch(() => {}); }, [refreshAll]);
  const persons = useMemo(() => resources.persons.map(toUiPerson), [resources.persons]);
  const createPerson = async ({ name, aliases, account }) => {
    const created = await API.post("/api/persons", { display_name: name, aliases });
    await API.post(`/api/persons/${encodeURIComponent(created.person.person_id)}/accounts`, { platform: "xueqiu", external_user_id: account, archive_basis_confirmed: true });
    await refreshAll();
  };
  const navigate = (next, personId = "") => { if (personId) setSelectedPersonId(personId); setPage(next); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const pageTitle = NAV_ITEMS.find((item) => item.id === page)?.label || "声迹";
  let pageContent;
  if (page === "workspace") pageContent = <WorkspacePage workspace={resources.workspace} onAdd={() => setAddOpen(true)} onNavigate={navigate}/>;
  else if (page === "people") pageContent = <PeoplePage persons={persons} onAdd={() => setAddOpen(true)} onNavigate={navigate}/>;
  else if (page === "person-detail") pageContent = <PersonDetailPage personId={selectedPersonId} onNavigate={navigate} onRefresh={() => refreshAll().catch(() => {})} showToast={showToast}/>;
  else if (page === "collect") pageContent = <CollectionPage persons={persons} onRefresh={refreshAll} showToast={showToast} onNavigate={navigate}/>;
  else if (page === "knowledge") pageContent = <KnowledgePage persons={persons} onRefresh={refreshAll} showToast={showToast} onNavigate={navigate}/>;
  else if (page === "ask") pageContent = <AskPage persons={persons} capabilities={resources.capabilities} showToast={showToast}/>;
  else if (page === "extensions") pageContent = <ExtensionsPage/>;
  else pageContent = <RuntimePage onRefresh={refreshAll} showToast={showToast}/>;

  return <AppShell page={page === "person-detail" ? "people" : page} pageTitle={page === "person-detail" ? "人物详情" : pageTitle} connected={connected} lastSync={lastSync} onNavigate={navigate}><ErrorBanner error={error} onRetry={() => refreshAll().catch(() => {})}/>{loading ? <div className="page"><EmptyState icon="refresh" title="正在读取本地资源" description="人物、任务和模型状态均来自当前 VoiceVault 服务。"/></div> : pageContent}<AddPersonModal open={addOpen} onClose={() => setAddOpen(false)} persons={persons} onCreate={createPerson} showToast={showToast}/><Toast toast={toast} onClose={() => setToast(null)}/></AppShell>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
