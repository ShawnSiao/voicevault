const { useEffect, useMemo, useState } = React;
const {
  Icon, Button, StatusPill, Avatar, PageIntro, MetricCard, Progress,
  Modal, Toast, Sidebar, MobileNav, TopStatus, DemoBar, NAV_ITEMS,
} = window.VVComponents;
const FIXTURES = window.VoiceVaultData;

const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(value);
const DAY_MS = 24 * 60 * 60 * 1000;
const parseIsoDay = (value) => Date.parse(`${value}T00:00:00Z`);
const addIsoDays = (value, days) => {
  const time = parseIsoDay(value);
  if (!Number.isFinite(time)) return "—";
  return new Date(time + days * DAY_MS).toISOString().slice(0, 10);
};

function PeoplePage({ persons, onAdd, onNavigate }) {
  const [activeId, setActiveId] = useState(persons[0]?.id);
  const active = persons.find((person) => person.id === activeId) || persons[0];
  const postTotal = persons.reduce((total, person) => total + person.posts, 0);
  const readyCount = persons.filter((person) => person.indexStatus === "ready").length;
  const pendingCount = persons.length - readyCount;
  const gapSegments = 1 + persons.filter((person) => person.indexStatus !== "ready").length;

  return (
    <div className="page page-people">
      <PageIntro
        eyebrow="人物是知识库边界"
        title="人物与平台账号"
        description="先建立人物，再绑定公开平台账号。同一人物后续可合并雪球、微博等多个来源。"
        actions={<><Button variant="secondary" icon="collect" onClick={() => onNavigate("collect")}>创建采集任务</Button><Button icon="personAdd" onClick={onAdd}>添加人物</Button></>}
      />

      <section className="metrics-grid" aria-label="人物资料概览">
        <MetricCard label="人物知识库" value={persons.length} meta={`${persons.filter((person) => person.account).length} 个已绑定雪球账号`} icon="users" />
        <MetricCard label="本地公开帖子" value={formatNumber(postTotal)} meta="历史版本另存 30 份" icon="file" tone="blue" />
        <MetricCard label="完整覆盖缺口" value={`${gapSegments} 段`} meta="等待手动采集" icon="calendar" tone="amber" />
        <MetricCard label="语义知识库" value={`${readyCount} / ${persons.length}`} meta={`${pendingCount} 个等待内容或向量`} icon="layers" tone="purple" />
      </section>

      <section className="work-grid people-work-grid">
        <div className="panel person-list-panel">
          <header className="panel-header">
            <div><h2>人物列表</h2><p>账号归属需要在首次采集后核验。</p></div>
            <div className="compact-filter"><Icon name="search"/><input aria-label="筛选人物" placeholder="搜索人物或账号 ID" /></div>
          </header>
          <div className="person-table-head" aria-hidden="true"><span>人物</span><span>本地内容</span><span>索引</span><span></span></div>
          <div className="person-rows">
            {persons.map((person) => (
              <button key={person.id} className={`person-row ${active?.id === person.id ? "is-active" : ""}`} onClick={() => setActiveId(person.id)}>
                <span className="person-cell person-main"><Avatar person={person}/><span><strong>{person.name}</strong><small>{person.alias}</small><em><b>雪球</b> @{person.account}</em></span></span>
                <span className="person-cell numeric"><strong>{formatNumber(person.posts)}</strong><small>{person.coverage}</small></span>
                <span className="person-cell"><StatusPill tone={person.indexStatus === "ready" ? "success" : person.posts === 0 ? "neutral" : "warning"}>{person.indexStatus === "ready" ? "混合索引就绪" : person.posts === 0 ? "等待采集" : "向量构建中"}</StatusPill></span>
                <Icon name="chevron" className="row-chevron"/>
              </button>
            ))}
          </div>
        </div>

        {active && <aside className="panel person-detail-panel">
          <header className="person-detail-head"><Avatar person={active} size="lg"/><div><p className="eyebrow">当前人物</p><h2>{active.name}</h2><span>{active.alias}</span></div><button className="icon-button" aria-label="更多操作"><Icon name="more"/></button></header>
          <div className="identity-check"><Icon name="shield"/><span><strong>{active.accountStatus === "已核验" ? "账号归属已核验" : active.posts === 0 ? "等待首次采集核验" : "存在待补采区间"}</strong><small>仅用于本机公开资料研究</small></span></div>
          <dl className="detail-list">
            <div><dt>平台账号</dt><dd><span className="platform-badge">雪球</span><span className="mono">{active.account}</span></dd></div>
            <div><dt>内容覆盖</dt><dd>{active.posts ? active.coverage : "尚未形成覆盖证明"}</dd></div>
            <div><dt>帖子 / 版本</dt><dd>{formatNumber(active.posts)} / {active.revisions}</dd></div>
            <div><dt>最近发帖</dt><dd>{active.posts ? active.lastPost : "尚无本地内容"}</dd></div>
          </dl>
          <div className="coverage-mini">
            <div><strong>覆盖证明</strong><span>{active.posts ? "已验证区间" : "未知"}</span></div>
            <div className="coverage-line"><i style={{ width: active.posts ? (active.indexStatus === "building" ? "68%" : "92%") : "2%" }}></i></div>
            <small>{active.posts ? "已完成区间不会在普通采集中重复请求远端。" : "本地无帖子，首次采集将建立覆盖基线。"}</small>
          </div>
          <div className="detail-actions"><Button variant="secondary" icon="eye">查看资料</Button><Button icon="collect" onClick={() => onNavigate("collect")}>采集缺失区间</Button></div>
        </aside>}
      </section>
      <div className="fixture-note"><Icon name="info" size={16}/><span>页面使用虚构人物和演示帖子，仅用于验证信息架构与交互。</span></div>
    </div>
  );
}

const COLLECTION_STEPS = [
  { id: "draft", label: "设置范围" },
  { id: "checked", label: "检查本地" },
  { id: "pending", label: "等待领取" },
  { id: "running", label: "采集中" },
  { id: "completed", label: "完成" },
];

function CollectionPage({ persons, taskState, setTaskState, collectionConfig, setCollectionConfig, showToast, onNavigate }) {
  const { personId, mode, startDate, endDate, handoffVersion } = collectionConfig;
  const person = persons.find((item) => item.id === personId) || persons[0];
  const normalizedStep = taskState === "waiting" ? "running" : taskState === "cancelled" ? "pending" : taskState;
  const stepIndex = COLLECTION_STEPS.findIndex((step) => step.id === normalizedStep);
  const startTime = parseIsoDay(startDate);
  const endTime = parseIsoDay(endDate);
  const rangeValid = Number.isFinite(startTime) && Number.isFinite(endTime) && endTime >= startTime;
  const totalDays = rangeValid ? Math.floor((endTime - startTime) / DAY_MS) + 1 : 0;
  const coverageDates = person.coverage.match(/\d{4}-\d{2}-\d{2}/g) || [];
  const coverageStart = coverageDates[0] ? parseIsoDay(coverageDates[0]) : NaN;
  const coverageEnd = coverageDates[1] ? parseIsoDay(coverageDates[1]) : NaN;
  const overlapStart = rangeValid && Number.isFinite(coverageStart) ? Math.max(startTime, coverageStart) : NaN;
  const overlapEnd = rangeValid && Number.isFinite(coverageEnd) ? Math.min(endTime, coverageEnd) : NaN;
  const coveredDays = Number.isFinite(overlapStart) && Number.isFinite(overlapEnd) && overlapEnd >= overlapStart ? Math.floor((overlapEnd - overlapStart) / DAY_MS) + 1 : 0;
  const gapDays = Math.max(totalDays - coveredDays, 0);
  const coverageLeft = totalDays && coveredDays ? Math.max(0, ((overlapStart - startTime) / DAY_MS) / totalDays * 100) : 0;
  const coverageWidth = totalDays ? coveredDays / totalDays * 100 : 0;
  const identityVerified = person.posts > 0;
  const coverageKnown = coveredDays > 0 || (person.posts > 0 && Number.isFinite(coverageStart));
  const needsRemoteAction = rangeValid && (mode === "recheck" || gapDays > 0);
  const gapSegments = !rangeValid || !gapDays ? 0 : !coveredDays ? 1 : Number(overlapStart > startTime) + Number(overlapEnd < endTime);
  const expectedActions = needsRemoteAction ? (mode === "recheck" ? 1 : gapSegments) : 0;
  const endExclusive = addIsoDays(endDate, 1);
  const axisDates = rangeValid ? [0, .33, .67, 1].map((ratio) => addIsoDays(startDate, Math.round((totalDays - 1) * ratio)).slice(5)) : ["—", "—", "—", "—"];
  const issuedVersion = Math.max(handoffVersion, 1);
  const handoffFingerprint = ((issuedVersion * 2654435761) >>> 0).toString(36).toUpperCase().padStart(7, "0");
  const handoffCode = `vvh_${String(issuedVersion).padStart(4, "0")}-${handoffFingerprint}`;
  const taskLocked = ["pending", "running", "waiting"].includes(taskState);
  const updateConfig = (patch) => {
    if (taskLocked) return;
    setCollectionConfig((current) => ({ ...current, ...patch }));
    setTaskState("draft");
  };

  const copyInstruction = async () => {
    const value = `执行声迹采集交接 ${handoffCode}`;
    try { await navigator.clipboard.writeText(value); } catch (_) {}
    showToast("执行指令已复制", "回到当前 Codex 桌面任务粘贴即可。", "success");
  };

  const createTask = () => {
    if (!needsRemoteAction) return;
    setCollectionConfig((current) => ({ ...current, handoffVersion: current.handoffVersion + 1 }));
    setTaskState("pending");
    showToast("采集任务已创建", "任务正在等待当前 Codex 桌面任务领取。", "success");
  };

  const reopenTask = () => {
    setCollectionConfig((current) => ({ ...current, handoffVersion: current.handoffVersion + 1 }));
    setTaskState("pending");
    showToast("已重新开放领取", "旧交接凭据作废，已生成新的继续指令。", "success");
  };
  const cancelTask = () => {
    setCollectionConfig((current) => ({ ...current, handoffVersion: current.handoffVersion + 1 }));
    setTaskState("cancelled");
    showToast("采集任务已取消", "当前交接凭据已撤销，不可继续复制或领取。", "warning");
  };

  return (
    <div className="page page-collect">
      <PageIntro
        eyebrow="手动批量采集 MVP"
        title="采集交接"
        description="网页检查本地缺口并生成交接任务；当前 Codex 桌面任务在已登录浏览器中执行采集。"
        actions={<Button variant="secondary" icon="activity" onClick={() => onNavigate("runtime")}>查看职责边界</Button>}
      />

      <div className="boundary-banner"><Icon name="info"/><div><strong>网页不会直接启动 Codex，也不保存 Cookie</strong><p>交接任务只包含人物、平台账号、时间范围和短期凭据。登录与验证保留在 Codex 内置浏览器中。</p></div></div>

      <section className="stepper" aria-label="采集任务进度">
        {COLLECTION_STEPS.map((step, index) => <div key={step.id} className={`${index <= stepIndex ? "is-done" : ""} ${index === stepIndex ? "is-current" : ""}`}><i>{index < stepIndex ? <Icon name="check" size={14}/> : index + 1}</i><span>{step.label}</span></div>)}
      </section>

      <section className="collection-layout">
        <div className="panel collection-config">
          <header className="panel-header"><div><p className="eyebrow">任务请求</p><h2>采集范围</h2></div><StatusPill tone={mode === "recheck" ? "info" : "neutral"}>{mode === "recheck" ? "追加复查" : "普通采集"}</StatusPill></header>
          <label className="field"><span>人物与账号</span><div className="select-wrap"><select value={personId} disabled={taskLocked} onChange={(event) => updateConfig({ personId: event.target.value })}>{persons.filter((item) => item.account).map((item) => <option key={item.id} value={item.id}>{item.name} · 雪球 @{item.account}</option>)}</select><Icon name="chevron"/></div></label>
          <div className="two-fields">
            <label className="field"><span>开始日期</span><input type="date" value={startDate} disabled={taskLocked} onChange={(event) => updateConfig({ startDate: event.target.value })}/></label>
            <label className="field"><span>结束日期</span><input type="date" value={endDate} disabled={taskLocked} onChange={(event) => updateConfig({ endDate: event.target.value })}/></label>
          </div>
          <fieldset className="segmented-field"><legend>采集方式</legend><label className={mode === "normal" ? "is-selected" : ""}><input type="radio" name="mode" checked={mode === "normal"} disabled={taskLocked} onChange={() => updateConfig({ mode: "normal" })}/><span><strong>普通采集</strong><small>完整覆盖段不访问远端</small></span></label><label className={mode === "recheck" ? "is-selected" : ""}><input type="radio" name="mode" checked={mode === "recheck"} disabled={taskLocked} onChange={() => updateConfig({ mode: "recheck" })}/><span><strong>追加复查</strong><small>写入新版本和观察记录</small></span></label></fieldset>
          <div className="range-code"><Icon name="calendar"/><span><small>内部半开区间</small><strong className="mono">{rangeValid ? `[${startDate} 00:00, ${endExclusive} 00:00)` : "结束日期不能早于开始日期"}</strong></span></div>
          {taskState === "draft" && <Button icon="search" className="full-button" disabled={!rangeValid} onClick={() => setTaskState("checked")}>检查本地覆盖与缺口</Button>}
          {taskState !== "draft" && <Button variant="ghost" icon={taskLocked ? "shield" : "refresh"} className="full-button" disabled={taskLocked} onClick={() => setTaskState("draft")}>{taskLocked ? "活动任务已锁定范围" : "重新设置采集范围"}</Button>}
        </div>

        <div className="panel coverage-panel">
          <header className="panel-header"><div><p className="eyebrow">本地覆盖预检</p><h2>{person.name}的内容区间</h2></div>{taskState === "draft" ? <StatusPill>尚未检查</StatusPill> : <StatusPill tone={coverageKnown ? "success" : "warning"}>{coverageKnown ? "已检查" : "覆盖未知"}</StatusPill>}</header>
          <div className="coverage-legend"><span><i className="legend-local"></i>已证明覆盖</span><span><i className="legend-gap"></i>实际缺口</span><span><i className="legend-request"></i>本次请求</span></div>
          <div className="coverage-map" aria-label="覆盖区间示意">
            <div className="coverage-axis">{axisDates.map((date, index) => <span key={`${date}-${index}`}>{date}</span>)}</div>
            <div className="coverage-request"><i></i></div>
            <div className="coverage-segments"><i className="segment-gap" style={{ left: 0, width: taskState === "draft" ? "0%" : "100%" }}></i><i className="segment-local" style={{ left: `${coverageLeft}%`, width: taskState === "draft" ? "0%" : `${coverageWidth}%` }}></i></div>
          </div>
          {taskState === "draft" ? <div className="empty-compact"><Icon name="search"/><strong>先检查本地资料</strong><p>系统将计算可跳过的完整覆盖段和真实待采集区间。</p></div> : <>
            <div className="coverage-summary"><div><small>可直接跳过</small><strong>{coveredDays} 天</strong><span>{coveredDays ? "已有完整覆盖证明" : "没有可跳过区间"}</span></div><div><small>实际待采集</small><strong>{mode === "recheck" ? totalDays : gapDays} 天</strong><span>{gapDays ? `${startDate.slice(5)} 至 ${endDate.slice(5)} 中的缺口` : "请求区间已完整覆盖"}</span></div><div><small>预计远端动作</small><strong>{expectedActions} 段</strong><span>{mode === "recheck" ? "复查并追加版本" : expectedActions ? "只访问缺口" : "remote_action_count=0"}</span></div></div>
            <div className={`proof-box ${!coverageKnown || !coveredDays ? "proof-warning" : ""}`}><Icon name={coverageKnown && coveredDays ? "shield" : "alert"}/><div><strong>{!coverageKnown ? "尚无新格式覆盖证明" : !coveredDays ? "请求区间没有本地覆盖" : gapDays === 0 ? "请求区间已完整覆盖" : "已有区间的跳过依据完整"}</strong><p>{!coverageKnown ? "首次采集将访问整个请求区间，并在采集时核验账号归属。" : !coveredDays ? "该人物有其他时间的本地内容，但本次请求仍需完整访问远端。" : gapDays === 0 ? "普通模式不会创建 Codex 任务，也不会访问远端。" : "覆盖记录、起止检查点和完成清单一致；只采集实际缺口。"}</p></div></div>
          </>}
        </div>

        <TaskHandoffPanel state={taskState} onCreate={createTask} onCopy={copyInstruction} onReopen={reopenTask} onCancel={cancelTask} onReset={() => setTaskState("draft")} onNavigate={onNavigate} handoffCode={handoffCode} needsRemoteAction={needsRemoteAction} identityVerified={identityVerified} expectedActions={expectedActions}/>
      </section>

      <DemoBar label="切换外部采集状态，检查所有界面分支">
        {[
          ["draft", "草稿"], ["checked", "已预检"], ["pending", "待领取"], ["running", "运行中"], ["waiting", "人工验证"], ["completed", "已完成"],
        ].map(([state, label]) => <button key={state} className={taskState === state ? "is-active" : ""} onClick={() => setTaskState(state)}>{label}</button>)}
      </DemoBar>
    </div>
  );
}

function TaskHandoffPanel({ state, onCreate, onCopy, onReopen, onCancel, onReset, onNavigate, handoffCode, needsRemoteAction, identityVerified, expectedActions }) {
  const titleByState = { draft: "等待范围设置", checked: "可以创建任务", pending: "等待 Codex 领取", running: "Codex 正在采集", waiting: "需要人工验证", completed: "采集与校验完成", cancelled: "采集任务已取消" };
  const toneByState = { draft: "neutral", checked: "info", pending: "warning", running: "info", waiting: "warning", completed: "success", cancelled: "error" };
  return (
    <div className={`panel handoff-panel handoff-${state}`}>
      <header className="panel-header"><div><p className="eyebrow">Codex 任务交接</p><h2>{titleByState[state]}</h2></div><StatusPill tone={toneByState[state]}>{state === "pending" ? "09:42 后过期" : state === "running" ? "心跳正常" : state === "waiting" ? "已暂停" : state === "completed" ? "清单有效" : state === "cancelled" ? "凭据已撤销" : "本机任务"}</StatusPill></header>
      {state === "draft" && <div className="handoff-empty"><span><Icon name="terminal" size={26}/></span><strong>还没有交接任务</strong><p>先完成本地覆盖检查。网页只在确认存在缺口后创建任务。</p></div>}
      {state === "checked" && <div className="handoff-ready"><div className="callout-row"><Icon name="check"/><span><strong>{needsRemoteAction ? "本地预检通过" : "无需创建采集任务"}</strong><small>{needsRemoteAction ? `将创建 ${expectedActions} 个待领取任务，不会立即访问雪球。` : "请求区间已完整覆盖，remote_action_count=0。"}</small></span></div><ul className="plain-checks"><li><Icon name="check"/>{identityVerified ? "账号 ID 与人物关系已核验" : "首次采集将核验账号 ID 与人物关系"}</li><li><Icon name="check"/>采集区间已换算为半开区间</li><li><Icon name="check"/>{needsRemoteAction ? "本地完整覆盖段已排除" : "完整覆盖证明阻止重复远端动作"}</li></ul>{needsRemoteAction ? <Button icon="terminal" className="full-button" onClick={onCreate}>创建 Codex 采集任务</Button> : <div className="no-remote-action"><Icon name="shield"/><span><strong>本地已有，不执行采集</strong><small>如需复查，请切换为「追加复查」。</small></span></div>}</div>}
      {state === "pending" && <div className="handoff-pending"><div className="handoff-code"><span><small>一次性执行指令</small><code>执行声迹采集交接 {handoffCode}</code></span><button onClick={onCopy} aria-label="复制执行指令"><Icon name="copy"/></button></div><p className="muted-copy">回到当前 Codex 桌面任务粘贴此指令。凭据被领取后立即失效，旧凭据不可重放。</p><div className="handoff-meta"><span><small>任务</small><strong className="mono">col_20260710_042</strong></span><span><small>状态</small><strong>pending_codex</strong></span><span><small>远端动作</small><strong>尚未发生</strong></span></div><div className="button-row"><Button icon="copy" onClick={onCopy}>复制执行指令</Button><Button variant="ghost" onClick={onCancel}>取消任务并撤销凭据</Button></div></div>}
      {state === "running" && <div className="handoff-running"><div className="live-line"><i></i><span><strong>Codex 已领取任务</strong><small>最近心跳：8 秒前 · 租约剩余 04:52</small></span></div><Progress value={64} label="采集进度 64% · 已到 2026-06-24"/><div className="run-stats"><span><small>已读取</small><strong>268</strong></span><span><small>新增</small><strong>186</strong></span><span><small>本地跳过</small><strong>82</strong></span></div><ol className="checkpoint-list"><li className="done"><i><Icon name="check" size={13}/></i><span>打开已登录的雪球账号页<small>22:08:12</small></span></li><li className="done"><i><Icon name="check" size={13}/></i><span>确认人物与账号归属<small>22:08:31</small></span></li><li className="active"><i></i><span>向更早帖子翻页并保存检查点<small>cursor_demo_18</small></span></li></ol></div>}
      {state === "waiting" && <div className="handoff-waiting"><div className="human-warning"><Icon name="alert"/><span><strong>雪球要求人工验证</strong><p>已保存 186 条有效帖子，完整覆盖尚未确认。请在同一个 Codex 内置浏览器完成验证。</p></span></div><dl className="checkpoint-data"><div><dt>错误</dt><dd className="mono">verification_required</dd></div><div><dt>最后检查点</dt><dd className="mono">cursor_demo_18 / post_demo_1208</dd></div><div><dt>最近心跳</dt><dd>22:11:46 · 已暂停</dd></div><div><dt>已安全保存</dt><dd>186 条帖子</dd></div></dl><Button icon="refresh" className="full-button" onClick={onReopen}>验证完成，重新开放领取</Button></div>}
      {state === "completed" && <div className="handoff-completed"><div className="success-seal"><Icon name="check" size={30}/></div><h3>缺失区间已完整覆盖</h3><p>结果清单、首尾检查点与本地写入数量一致。原有内容未覆盖。</p><div className="completion-grid"><span><small>新增帖子</small><strong>204</strong></span><span><small>新增版本</small><strong>3</strong></span><span><small>远端动作</small><strong>1 段</strong></span><span><small>覆盖至</small><strong>06-30</strong></span></div><Button icon="database" className="full-button" onClick={() => onNavigate("knowledge")}>查看知识库索引</Button></div>}
      {state === "cancelled" && <div className="handoff-empty handoff-cancelled"><span><Icon name="close" size={26}/></span><strong>任务与交接凭据已撤销</strong><p>当前 handoff 不再显示，也不能被复制或领取。重新创建任务将签发更高版本的新凭据。</p><Button variant="secondary" icon="refresh" onClick={onReset}>返回重新设置范围</Button></div>}
    </div>
  );
}

function KnowledgePage({ persons, knowledgeState, setKnowledgeState, showToast, onNavigate }) {
  const { vectorState, progress, generation } = knowledgeState;
  const generationName = (value) => `gen-20260710-${String(value).padStart(2, "0")}`;
  const currentGeneration = generationName(generation);
  const nextGeneration = generationName(generation + 1);
  const rebuild = () => { setKnowledgeState((current) => ({ ...current, progress: 18, vectorState: "building" })); showToast("已开始构建新代次", "旧代次继续提供查询，不中断当前检索。", "success"); };

  return (
    <div className="page page-knowledge">
      <PageIntro eyebrow="全文 + embedding 混合检索" title="知识库索引" description="每个人物拥有独立知识库边界；帖子、历史版本和语义分段可追溯到同一份原文。" actions={<><Button variant="secondary" icon="refresh" onClick={rebuild}>重建向量索引</Button><Button icon="chat" onClick={() => onNavigate("ask")}>使用知识库问答</Button></>} />
      {vectorState === "degraded" && <div className="degraded-banner"><Icon name="alert"/><div><strong>embedding 暂不可用，当前不是完整语义知识库</strong><p>仅保留全文精确搜索。检查 OpenAI-compatible embedding provider 后再重试。</p></div><Button variant="warning" icon="refresh" onClick={rebuild}>重新连接并构建</Button></div>}
      <section className="metrics-grid knowledge-metrics">
        <MetricCard label="已入库帖子" value="2,432" meta="30 份历史版本" icon="file" />
        <MetricCard label="知识分段" value="6,278" meta="按帖子版本可追溯" icon="layers" tone="purple" />
        <MetricCard label="全文索引" value="就绪" meta="FTS5 + CJK 二元索引" icon="search" tone="blue" />
        <MetricCard label="向量索引" value={vectorState === "ready" ? "6,278 / 6,278" : vectorState === "building" ? `${progress}%` : "不可用"} meta={vectorState === "ready" ? `当前代次 ${currentGeneration}` : vectorState === "building" ? `${currentGeneration} 仍在服务` : "仅全文检索"} icon="sparkle" tone={vectorState === "degraded" ? "amber" : "purple"} />
      </section>
      <section className="knowledge-layout">
        <div className="panel kb-people-panel">
          <header className="panel-header"><div><h2>人物知识库</h2><p>账号内容在人物边界内合并，不跨人物共享归属。</p></div><div className="compact-filter"><Icon name="filter"/><button>全部状态 <Icon name="chevron" size={14}/></button></div></header>
          <div className="kb-table-head"><span>人物与来源</span><span>内容</span><span>全文索引</span><span>向量索引</span><span>最近更新</span></div>
          <div className="kb-rows">
            {persons.map((person) => <div className="kb-row" key={person.id}><div className="kb-person"><Avatar person={person}/><span><strong>{person.name}</strong><small><b>雪球</b> @{person.account}</small></span></div><div><strong>{formatNumber(person.posts)}</strong><small>{formatNumber(person.chunks)} 个分段</small></div><div><StatusPill tone={person.posts ? "success" : "neutral"}>{person.posts ? "就绪" : "无内容"}</StatusPill><small>{person.posts ? "100%" : "—"}</small></div><div><StatusPill tone={!person.posts ? "neutral" : vectorState === "degraded" ? "warning" : person.indexStatus === "ready" ? "success" : "warning"}>{!person.posts ? "等待内容" : vectorState === "degraded" ? "已降级" : person.indexStatus === "ready" ? "就绪" : "构建中"}</StatusPill><small>{!person.posts ? "—" : vectorState === "degraded" ? "仅全文" : person.indexStatus === "ready" ? "当前代次" : "63%"}</small></div><div><strong>{person.posts ? person.lastPost.slice(0, 10) : "—"}</strong><small>{person.posts ? "内容更新" : "等待首次采集"}</small></div></div>)}
          </div>
        </div>
        <aside className="panel generation-panel">
          <header className="panel-header"><div><p className="eyebrow">当前可服务代次</p><h2 className="mono">{currentGeneration}</h2></div><StatusPill tone={vectorState === "ready" ? "success" : vectorState === "building" ? "info" : "warning"}>{vectorState === "ready" ? "语义知识库就绪" : vectorState === "building" ? "新代次构建中" : "全文降级"}</StatusPill></header>
          {vectorState === "building" && <div className="generation-progress"><Progress value={progress} label={`新代次 ${nextGeneration} · ${progress}%`}/><p>完成切换前，`{currentGeneration}` 继续提供混合检索。</p></div>}
          <dl className="provider-list"><div><dt>Embedding provider</dt><dd><strong>OpenAI-compatible</strong><StatusPill tone={vectorState === "degraded" ? "warning" : "success"}>{vectorState === "degraded" ? "连接失败" : "已连接"}</StatusPill></dd></div><div><dt>模型指纹</dt><dd><code>text-embedding-3-small-compatible</code></dd></div><div><dt>向量维度</dt><dd><strong>1536</strong></dd></div><div><dt>全文索引</dt><dd><strong>FTS5 + CJK bigram</strong></dd></div><div><dt>密钥来源</dt><dd><strong>环境变量</strong><small>页面不读取或展示</small></dd></div></dl>
          <div className="provider-note"><Icon name="shield"/><span>模型、地址和密钥均不写入人物知识库；索引保存模型指纹，便于检测不兼容变更。</span></div>
        </aside>
      </section>
      <DemoBar label="模拟向量 provider 与代次切换">
        <button className={vectorState === "ready" ? "is-active" : ""} onClick={() => setKnowledgeState((current) => ({ ...current, vectorState: "ready", progress: 100 }))}>就绪</button><button className={vectorState === "building" ? "is-active" : ""} onClick={() => setKnowledgeState((current) => ({ ...current, vectorState: "building", progress: 63 }))}>构建中</button><button className={vectorState === "degraded" ? "is-active" : ""} onClick={() => setKnowledgeState((current) => ({ ...current, vectorState: "degraded" }))}>降级</button>
      </DemoBar>
    </div>
  );
}

function AskPage({ persons, showToast, knowledgeState }) {
  const [selectedIds, setSelectedIds] = useState(["p-lin", "p-zhou"]);
  const [query, setQuery] = useState("高波动行情中，他们如何安排仓位与确认信号？");
  const [stage, setStage] = useState("idle");
  const [activeCitation, setActiveCitation] = useState("E1");
  const retrievalDegraded = knowledgeState.vectorState === "degraded";
  const selectedPersons = persons.filter((person) => selectedIds.includes(person.id));
  const selectedEvidence = FIXTURES.evidence.filter((item) => selectedIds.includes(item.personId));
  const visibleEvidence = retrievalDegraded ? selectedEvidence.filter((item) => item.match.includes("全文")) : selectedEvidence;
  const evidencePersonIds = new Set(visibleEvidence.map((item) => item.personId));
  const missingPersons = selectedPersons.filter((person) => !evidencePersonIds.has(person.id));
  const citationsComplete = selectedPersons.length > 0 && visibleEvidence.length > 0 && missingPersons.length === 0;
  const evidenceKey = visibleEvidence.map((item) => item.id).join("|");

  useEffect(() => {
    const next = { retrieving: ["answering", 850], answering: ["validating", 950], validating: ["ready", 700] }[stage];
    if (!next) return;
    const timer = setTimeout(() => setStage(next[0]), next[1]);
    return () => clearTimeout(timer);
  }, [stage]);

  useEffect(() => {
    if (!visibleEvidence.some((item) => item.id === activeCitation)) setActiveCitation(visibleEvidence[0]?.id || "");
  }, [evidenceKey]);

  const invalidateAnswer = () => { if (stage !== "idle") setStage("idle"); };
  const togglePerson = (id) => {
    setSelectedIds((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < 10 ? [...current, id] : current);
    invalidateAnswer();
  };
  const updateQuery = (value) => { setQuery(value); invalidateAnswer(); };
  const ask = () => {
    if (!query.trim()) return showToast("还没有研究问题", "输入问题后再开始混合检索。", "warning");
    if (!selectedIds.length) return showToast("至少选择一个人物", "多人问答最多可选择 10 个人物。", "warning");
    setStage("retrieving");
  };
  const cite = (id) => { setActiveCitation(id); document.querySelector(`[data-evidence='${id}']`)?.scrollIntoView({ behavior: "smooth", block: "nearest" }); };

  return (
    <div className="page page-ask">
      <PageIntro eyebrow="一份回答，分别标注人物观点" title="多人物知识问答" description={retrievalDegraded ? "embedding 当前不可用，仅以全文精确搜索召回证据，再交给当前 Codex 任务组织回答。" : "先以全文与向量混合召回证据，再将有限证据交给当前 Codex 任务组织回答。"} actions={<><StatusPill tone={retrievalDegraded ? "warning" : "success"} icon={retrievalDegraded ? "alert" : "layers"}>{retrievalDegraded ? "检索：仅全文" : "检索：混合"}</StatusPill><StatusPill tone="info" icon="sparkle">回答：当前 Codex 任务</StatusPill></>} />
      {retrievalDegraded && <div className="degraded-banner ask-degraded"><Icon name="alert"/><div><strong>语义召回已暂停</strong><p>当前回答只使用全文命中的证据；仅向量命中的材料不会进入回答，也不会显示为引用。</p></div></div>}
      <section className="panel ask-composer">
        <div className="composer-main"><label><span>研究问题</span><textarea value={query} onChange={(event) => updateQuery(event.target.value)} rows="2" placeholder="输入需要比较或综合的问题…"/></label><Button icon={retrievalDegraded ? "search" : "sparkle"} onClick={ask} disabled={stage !== "idle" && stage !== "ready"}>{stage !== "idle" && stage !== "ready" ? "正在生成可核验回答" : retrievalDegraded ? "全文检索并交给 Codex 回答" : "混合检索并交给 Codex 回答"}</Button></div>
        <div className="composer-options"><div className="person-picker"><span>人物知识库</span><div>{persons.map((person) => <button key={person.id} className={selectedIds.includes(person.id) ? "is-selected" : ""} onClick={() => togglePerson(person.id)}><Avatar person={person} size="xs"/><span>{person.name}</span>{selectedIds.includes(person.id) && <Icon name="check" size={14}/>}</button>)}</div></div><div className="ask-filters"><button><Icon name="calendar"/> 2025-01-01 至今 <Icon name="chevron" size={13}/></button><button><Icon name="filter"/> 雪球 · 当前版本 <Icon name="chevron" size={13}/></button></div></div>
      </section>

      {stage === "idle" && <section className="ask-empty panel"><span className="ask-orbit"><Icon name="search" size={26}/></span><h2>从人物材料中寻找可追溯答案</h2><p>{retrievalDegraded ? "当前仅使用全文精确搜索；仅向量命中的材料不会进入回答。证据不足时保留空白。" : "系统默认使用混合检索，不要求手动选择「关键词」或「语义」。证据不足时会明确保留空白。"}</p><div className="suggestion-list"><button onClick={() => updateQuery("比较他们对流动性确认的判断顺序。")}>比较他们对流动性确认的判断顺序<Icon name="arrow"/></button><button onClick={() => updateQuery("他们如何定义观察仓与进攻仓的边界？")}>他们如何定义观察仓与进攻仓的边界<Icon name="arrow"/></button><button onClick={() => updateQuery("哪些观点有直接证据，哪些仍缺少材料？")}>区分有证据观点与材料空白<Icon name="arrow"/></button></div></section>}
      {["retrieving", "answering", "validating"].includes(stage) && <AnswerProgress stage={stage} selectedPersons={selectedPersons} retrievalDegraded={retrievalDegraded} evidenceCount={visibleEvidence.length}/>}
      {stage === "ready" && <section className="answer-workspace">
        <section className="answer-document panel">
          <header className="answer-head"><div><p className="eyebrow">综合回答</p><h2>{query}</h2><p>基于 {visibleEvidence.length} 条证据 · {selectedPersons.length} 个人物 · 2025-01-01 至今</p></div><StatusPill tone={citationsComplete ? "success" : "warning"}>{citationsComplete ? "引用校验通过" : "存在证据空白"}</StatusPill></header>
          <div className="answer-notice"><Icon name="info"/><span>以下为演示性资料整理，不构成投资建议。结论只覆盖当前选择的本地材料。</span></div>
          <AnswerSynthesis selectedPersons={selectedPersons} visibleEvidence={visibleEvidence} missingPersons={missingPersons} activeCitation={activeCitation} onSelect={cite}/>
          <div className="person-opinions"><h3>人物观点</h3>{selectedPersons.map((person) => <PersonOpinion key={person.id} person={person} evidence={visibleEvidence.filter((item) => item.personId === person.id)} activeCitation={activeCitation} onSelect={cite}/>)}</div>
          <footer className="answer-footer"><div><Icon name="shield"/><span><strong>证据边界</strong><small>未使用跨人物引用；未把目录、转发或推测当作本人观点。</small></span></div><Button variant="secondary" icon="refresh" onClick={() => setStage("retrieving")}>使用当前条件重新回答</Button></footer>
        </section>
        <EvidenceRail evidence={visibleEvidence} activeId={activeCitation} onSelect={setActiveCitation}/>
      </section>}
      {stage === "invalid" && <AnswerInvalid onReturn={() => setStage("idle")}/>}

      <DemoBar label="快速检查问答生命周期与结果布局">
        {[["idle","待提问"],["retrieving",retrievalDegraded ? "全文检索" : "混合检索"],["answering","Codex 回答"],["validating","引用校验"],["ready","回答完成"],["invalid","引用无效"]].map(([id,label]) => <button key={id} className={stage === id ? "is-active" : ""} onClick={() => setStage(id)}>{label}</button>)}
      </DemoBar>
    </div>
  );
}

function Citation({ id, active, onSelect }) {
  return <button className={`citation ${active ? "is-active" : ""}`} onMouseEnter={() => onSelect(id)} onFocus={() => onSelect(id)} onClick={() => onSelect(id)}>{id}</button>;
}

function AnswerSynthesis({ selectedPersons, visibleEvidence, missingPersons, activeCitation, onSelect }) {
  const selected = new Set(selectedPersons.map((person) => person.id));
  const availableEvidenceIds = new Set(visibleEvidence.map((item) => item.id));
  const supportedPersons = selectedPersons.filter((person) => visibleEvidence.some((item) => item.personId === person.id));
  const firstEvidence = supportedPersons.map((person) => visibleEvidence.find((item) => item.personId === person.id)).filter(Boolean);
  const defaultPair = selectedPersons.length === 2 && selected.has("p-lin") && selected.has("p-zhou");
  const onlyPerson = selectedPersons.length === 1 ? selectedPersons[0] : null;
  const citations = (...ids) => ids.filter((id) => availableEvidenceIds.has(id)).map((id) => <Citation key={id} id={id} active={activeCitation === id} onSelect={onSelect}/>);

  let conclusion;
  if (!visibleEvidence.length) {
    conclusion = <>当前人物与时间范围内没有可用证据，因此不生成观点或人物比较。可先补采公开帖子，或扩大检索时间范围。</>;
  } else if (defaultPair) {
    conclusion = <>两人的共同顺序是「先确认约束，再增加仓位」：林舟强调把波动来源和仓位层级分开，周岚强调用成交承接与次日回撤验证流动性持续性。两者都不支持用单一情绪信号直接推导满仓动作。{citations("E1", "E3", "E4")}</>;
  } else if (onlyPerson?.id === "p-lin") {
    conclusion = <>林舟把高波动阶段的动作拆成观察仓与进攻仓：先解释波动来源，再决定是否增加仓位。{citations("E1", "E2")}</>;
  } else if (onlyPerson?.id === "p-zhou") {
    conclusion = <>周岚强调流动性确认的持续性，主张联合观察价格扩散、成交承接和次日回撤，不以单日放量或情绪极值直接决定仓位。{citations("E3", "E4")}</>;
  } else if (onlyPerson?.id === "p-an") {
    conclusion = <>当前只检索到安宁关于「开仓前先写退出条件」的直接材料，可确认其强调把风险预算落实为退出动作；不足以推断完整仓位体系。{citations("E5")}</>;
  } else {
    conclusion = <>当前选择中，{supportedPersons.map((person) => person.name).join("、") || "没有人物"}存在直接证据；{missingPersons.length ? `${missingPersons.map((person) => person.name).join("、")} 暂无支持材料。` : "所有人物均有支持材料。"} 综合结论仅保留可追溯的共同约束，不填补证据空白。{firstEvidence.map((item) => <Citation key={item.id} id={item.id} active={activeCitation === item.id} onSelect={onSelect}/>)}</>;
  }

  return <>
    <article className="answer-section"><h3>综合结论</h3><p>{conclusion}</p></article>
    <div className="consensus-grid"><article><span className="mini-icon success"><Icon name="check"/></span><div><h3>{visibleEvidence.length ? "可确认内容" : "当前结论"}</h3><p>{visibleEvidence.length ? `已找到 ${visibleEvidence.length} 条可回到原帖的直接证据，引用只属于当前所选人物。` : "没有证据时不生成推断性回答。"}</p></div></article><article><span className="mini-icon warning"><Icon name="alert"/></span><div><h3>证据边界</h3><p>{missingPersons.length ? `${missingPersons.map((person) => person.name).join("、")} 缺少支持材料，保留独立空白位置。` : "当前选择人物均有直接证据，但不据此判断观点适用范围。"}</p></div></article></div>
  </>;
}

function AnswerInvalid({ onReturn }) {
  return <section className="panel answer-invalid"><span><Icon name="alert" size={28}/></span><p className="eyebrow">citation_invalid</p><h2>引用归属校验未通过</h2><p>检测到引用人物、帖子版本或可访问状态不一致。该回答不会显示为可信结果，也不会保留「引用校验通过」标记。</p><div><code>E3 → selected_person_mismatch</code><Button variant="secondary" icon="refresh" onClick={onReturn}>返回调整检索范围</Button></div></section>;
}

function PersonOpinion({ person, evidence, activeCitation, onSelect }) {
  const isLin = person.id === "p-lin";
  const isZhou = person.id === "p-zhou";
  const isAn = person.id === "p-an";
  const evidenceIds = new Set(evidence.map((item) => item.id));
  const hasEvidence = evidenceIds.size > 0;
  const statusTone = !hasEvidence ? "neutral" : evidenceIds.size > 1 ? "success" : "warning";
  const statusText = !hasEvidence ? "无支持材料" : evidenceIds.size > 1 ? "直接证据" : "有限直接证据";
  const citation = (id) => evidenceIds.has(id) ? <Citation id={id} active={activeCitation === id} onSelect={onSelect}/> : null;
  let opinion;
  if (!hasEvidence) opinion = <p>当前检索模式与时间范围内未找到支持材料，因此不生成该人物的观点。</p>;
  else if (isLin) opinion = <p>可确认其先解释波动来源、再决定仓位层级；当前可用材料不足的部分不继续外推。{citation("E1")}{citation("E2")}</p>;
  else if (isZhou) opinion = <p>先验证流动性是否持续：价格扩散、成交承接、次日回撤要联合观察；单日放量和情绪极值都不够。{citation("E3")}{citation("E4")}</p>;
  else if (isAn) opinion = <p>只检索到「先写退出条件」的直接材料，尚不足以归纳其完整仓位框架。{citation("E5")}</p>;
  else opinion = <p>存在可用材料，但当前演示未定义可稳定归纳的观点模板。</p>;
  return <article className="opinion-card"><header><Avatar person={person}/><span><strong>{person.name}</strong><small>雪球 @{person.account}</small></span><StatusPill tone={statusTone}>{statusText}</StatusPill></header>{opinion}</article>;
}

function AnswerProgress({ stage, selectedPersons, retrievalDegraded, evidenceCount }) {
  const stages = [
    { id: "retrieving", title: retrievalDegraded ? "全文精确搜索" : "混合检索", detail: retrievalDegraded ? "全文召回 9 条 · 向量召回已暂停 · 当前不宣称语义检索" : "全文召回 9 条 · 向量召回 12 条 · 融合去重 8 条" },
    { id: "answering", title: "Codex 组织回答", detail: `已提交 ${evidenceCount} 条证据与人物归属，不提交 Cookie 或知识库文件` },
    { id: "validating", title: "引用与归属校验", detail: "检查证据存在、人物一致、版本可访问和引用位置" },
  ];
  const current = stages.findIndex((item) => item.id === stage);
  return <section className="panel answer-progress"><div className="progress-visual"><span className="search-pulse"><Icon name={stage === "retrieving" ? "search" : stage === "answering" ? "sparkle" : "shield"} size={28}/></span><div><p className="eyebrow">正在生成可核验回答</p><h2>{stages[current].title}</h2><p>{stages[current].detail}</p></div></div><div className="answer-stage-list">{stages.map((item,index) => <div key={item.id} className={`${index < current ? "done" : ""} ${index === current ? "active" : ""}`}><i>{index < current ? <Icon name="check" size={14}/> : index + 1}</i><span><strong>{item.title}</strong><small>{item.detail}</small></span></div>)}</div><div className="selected-context"><span>当前人物</span>{selectedPersons.map((person) => <span key={person.id}><Avatar person={person} size="xs"/>{person.name}</span>)}</div></section>;
}

function EvidenceRail({ evidence, activeId, onSelect }) {
  return <aside className="evidence-rail panel"><header className="panel-header"><div><p className="eyebrow">引用检查器</p><h2>证据原文</h2></div><StatusPill tone={evidence.length ? "success" : "warning"}>{evidence.length} 条有效</StatusPill></header><p className="rail-hint">悬停回答中的引用可定位对应材料。</p>{evidence.length ? <div className="evidence-list">{evidence.map((item) => <article key={item.id} data-evidence={item.id} className={`evidence-card ${activeId === item.id ? "is-active" : ""}`} onClick={() => onSelect(item.id)}><header><span className="evidence-id">{item.id}</span><span><strong>{item.person}</strong><small>雪球 @{item.account}</small></span><StatusPill tone="success" dot={false}>{item.status}</StatusPill></header><h3>{item.title}</h3><blockquote>“{item.excerpt}”</blockquote><div className="evidence-meta"><span><Icon name="calendar" size={13}/>{item.publishedAt}</span><span><Icon name="layers" size={13}/>{item.match}</span></div><footer><span>相关度 <strong>{Math.round(item.score * 100)}%</strong></span><button onClick={(event) => { event.stopPropagation(); }}>查看原帖（演示）<Icon name="external" size={14}/></button></footer></article>)}</div> : <div className="empty-evidence"><Icon name="search" size={24}/><strong>没有可用证据</strong><p>未生成引用，也不会把其他人物的材料补入当前位置。</p></div>}</aside>;
}

function RuntimePage({ showToast, knowledgeState }) {
  const retrievalDegraded = knowledgeState.vectorState === "degraded";
  return <div className="page page-runtime"><PageIntro eyebrow="本机职责与依赖" title="运行状态" description="将网页、采集执行、检索和回答拆开显示，避免把外部任务误报为网页能力。" actions={<Button variant="secondary" icon="refresh" onClick={() => showToast(retrievalDegraded ? "检查完成：存在降级" : "连接检查完成", retrievalDegraded ? "embedding provider 不可用；全文索引、Codex 任务桥接与本地服务仍可用。" : "本地服务、全文索引、embedding 与 Codex 任务桥接均可用。", retrievalDegraded ? "warning" : "success")}>重新检查连接</Button>}/>
    <section className="runtime-flow panel"><header className="panel-header"><div><h2>当前运行链路</h2><p>当前方案由 Codex 桌面完成采集和回答，网页可独立管理本地资料。</p></div><StatusPill tone={retrievalDegraded ? "warning" : "success"}>{retrievalDegraded ? "3 / 4 可用 · 已降级" : "4 / 4 可用"}</StatusPill></header><div className="flow-nodes"><article><span><Icon name="server"/></span><strong>本地网页</strong><small>建人物、建任务、看结果</small><StatusPill tone="success">运行中</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="terminal"/></span><strong>Codex 任务桥接</strong><small>领取任务、提交证据</small><StatusPill tone="success">已连接</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="collect"/></span><strong>内置浏览器</strong><small>沿用登录态、人工验证</small><StatusPill tone="info">由 Codex 管理</StatusPill></article><i><Icon name="arrow"/></i><article><span><Icon name="database"/></span><strong>本地知识库</strong><small>{retrievalDegraded ? "原文、版本、全文索引" : "原文、版本、混合索引"}</small><StatusPill tone={retrievalDegraded ? "warning" : "success"}>{retrievalDegraded ? "仅全文" : "就绪"}</StatusPill></article></div></section>
    <section className="runtime-grid"><div className="panel capability-panel"><header className="panel-header"><div><h2>能力检查</h2><p>各组件独立报告，不以单一「系统正常」掩盖降级。</p></div></header><div className="capability-list"><Capability icon="server" title="本地 HTTP 服务" detail="127.0.0.1 · 单用户" status="正常" tone="success"/><Capability icon="terminal" title="Codex 任务桥接" detail="最近心跳 8 秒前" status="可领取" tone="success"/><Capability icon="search" title="全文索引" detail="FTS5 + CJK bigram" status="就绪" tone="success"/><Capability icon="sparkle" title="Embedding provider" detail={retrievalDegraded ? "OpenAI-compatible · 连接失败" : "OpenAI-compatible · 1536 维"} status={retrievalDegraded ? "不可用" : "已连接"} tone={retrievalDegraded ? "warning" : "success"}/><Capability icon="chat" title="回答 provider" detail={retrievalDegraded ? "当前 Codex 任务 · 仅全文证据" : "当前 Codex 桌面任务"} status="当前模式" tone="info"/><Capability icon="collect" title="定时增量采集" detail="MVP 暂不启用" status="后续阶段" tone="neutral"/></div></div><div className="panel activity-panel"><header className="panel-header"><div><h2>最近运行记录</h2><p>关键动作保留时间、结果和可恢复入口。</p></div><button className="text-button">查看全部</button></header><div className="activity-list">{FIXTURES.activity.map((item,index) => <article key={index}><i className={`activity-dot ${item.tone}`}></i><time>{item.time}</time><span><strong>{item.title}</strong><small>{item.meta}</small></span><button aria-label={`查看 ${item.title}`}><Icon name="chevron"/></button></article>)}</div><div className="runtime-boundary"><Icon name="shield"/><div><strong>本地数据边界</strong><p>Cookie 留在浏览器；API 密钥来自环境变量；项目只保存公开帖子、索引和任务状态。</p></div></div></div></section>
    <section className="panel extension-panel"><div><span className="extension-icon"><Icon name="layers" size={22}/></span><div><p className="eyebrow">后续扩展口</p><h2>可替换的 RAG 与回答 provider</h2><p>接入本地 RAG 服务或 OpenAI-compatible API 后，网页可脱离当前 Codex 任务独立完成检索与回答；现有人物、证据和引用契约保持不变。</p></div></div><dl><div><dt>Embedding</dt><dd><code>VOICEVAULT_EMBEDDING_BASE_URL</code></dd></div><div><dt>回答模型</dt><dd><code>VOICEVAULT_ANSWER_BASE_URL</code></dd></div><div><dt>凭据</dt><dd><code>环境变量 · 不入库</code></dd></div></dl></section>
  </div>;
}

function Capability({ icon, title, detail, status, tone }) {
  return <article><span className={`capability-icon tone-${tone}`}><Icon name={icon}/></span><div><strong>{title}</strong><small>{detail}</small></div><StatusPill tone={tone}>{status}</StatusPill></article>;
}

function AddPersonModal({ open, onClose, persons, onAdd, showToast }) {
  const [name, setName] = useState("");
  const [alias, setAlias] = useState("");
  const [account, setAccount] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  useEffect(() => { if (open) { setName(""); setAlias(""); setAccount(""); setConfirmed(false); setSubmitted(false); } }, [open]);
  const duplicate = persons.some((person) => person.account.toLowerCase() === account.trim().toLowerCase());
  const valid = name.trim() && account.trim() && confirmed && !duplicate;
  const submit = () => {
    setSubmitted(true);
    if (!valid) return;
    const tones = ["teal", "blue", "amber"];
    onAdd({ id: `p-${Date.now()}`, name: name.trim(), alias: alias.trim() || "待补充研究标签", initials: name.trim().slice(0, 2).toUpperCase(), tone: tones[persons.length % tones.length], account: account.trim(), platform: "雪球", accountStatus: "待首次采集", posts: 0, revisions: 0, coverage: "尚未形成覆盖证明", lastPost: "—", indexStatus: "empty", chunks: 0 });
    onClose();
    showToast("人物与雪球账号已建立", "下一步可创建首次采集任务并核验账号归属。", "success");
  };
  return <Modal open={open} onClose={onClose} title="添加人物与平台账号" description="同一人物后续可继续绑定其他平台账号，内容统一归入该人物知识库。" footer={<><Button variant="ghost" onClick={onClose}>暂不添加</Button><Button icon="personAdd" onClick={submit}>建立人物知识库</Button></>}>
    <div className="form-section"><div className="form-section-number">1</div><div><h3>人物信息</h3><p>使用便于本机识别的名称，不要求与平台昵称相同。</p><div className="two-fields"><label className="field"><span>人物名称 <b>必填</b></span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：林舟"/>{submitted && !name.trim() && <em className="field-error">请输入人物名称</em>}</label><label className="field"><span>研究标签</span><input value={alias} onChange={(event) => setAlias(event.target.value)} placeholder="例如：周期与仓位观察"/></label></div></div></div>
    <div className="form-section"><div className="form-section-number">2</div><div><h3>首个平台账号</h3><p>MVP 先支持雪球。主页地址由平台适配器根据唯一 ID 构造。</p><div className="two-fields"><label className="field"><span>平台</span><div className="select-wrap"><select disabled><option>雪球</option></select><Icon name="chevron"/></div><small>微博等平台将在后续适配。</small></label><label className="field"><span>雪球用户唯一 ID <b>必填</b></span><input className="mono" value={account} onChange={(event) => setAccount(event.target.value)} placeholder="例如：linzhou_demo"/>{submitted && !account.trim() && <em className="field-error">请输入雪球用户唯一 ID</em>}{duplicate && <em className="field-error">此账号已绑定到其他人物</em>}</label></div></div></div>
    <label className={`confirm-box ${confirmed ? "is-checked" : ""}`}><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}/><i>{confirmed && <Icon name="check" size={15}/>}</i><span><strong>确认仅用于本机公开资料研究</strong><small>只采集公开帖子；登录态、Cookie、密钥和私人样本不写入项目。</small></span></label>
    {submitted && !confirmed && <p className="form-global-error"><Icon name="alert"/>需要确认本机研究用途后才能建立账号。</p>}
  </Modal>;
}

function App() {
  const [page, setPage] = useState("people");
  const [persons, setPersons] = useState(FIXTURES.persons);
  const [addOpen, setAddOpen] = useState(false);
  const [taskState, setTaskState] = useState("draft");
  const [collectionConfig, setCollectionConfig] = useState({ personId: "p-an", mode: "normal", startDate: "2026-06-01", endDate: "2026-06-30", handoffVersion: 0 });
  const [knowledgeState, setKnowledgeState] = useState({ vectorState: "ready", progress: 100, generation: 2 });
  const [toast, setToast] = useState(null);
  useEffect(() => {
    if (knowledgeState.vectorState !== "building") return;
    const timer = setInterval(() => setKnowledgeState((current) => {
      if (current.vectorState !== "building") return current;
      const nextProgress = Math.min(current.progress + 3, 100);
      if (nextProgress >= 100) return { ...current, progress: 100, vectorState: "ready", generation: current.generation + 1 };
      return { ...current, progress: nextProgress };
    }), 180);
    return () => clearInterval(timer);
  }, [knowledgeState.vectorState]);
  const showToast = (title, detail, tone = "success") => setToast({ title, detail, tone, key: Date.now() });
  const pageTitle = NAV_ITEMS.find((item) => item.id === page)?.label || "声迹";
  const navigate = (next) => { setPage(next); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const pageContent = useMemo(() => {
    if (page === "people") return <PeoplePage persons={persons} onAdd={() => setAddOpen(true)} onNavigate={navigate}/>;
    if (page === "collect") return <CollectionPage persons={persons} taskState={taskState} setTaskState={setTaskState} collectionConfig={collectionConfig} setCollectionConfig={setCollectionConfig} showToast={showToast} onNavigate={navigate}/>;
    if (page === "knowledge") return <KnowledgePage persons={persons} knowledgeState={knowledgeState} setKnowledgeState={setKnowledgeState} showToast={showToast} onNavigate={navigate}/>;
    if (page === "ask") return <AskPage persons={persons} showToast={showToast} knowledgeState={knowledgeState}/>;
    return <RuntimePage showToast={showToast} knowledgeState={knowledgeState}/>;
  }, [page, persons, taskState, collectionConfig, knowledgeState]);

  return <div className="app-shell"><Sidebar page={page} onNavigate={navigate}/><div className="app-main"><TopStatus pageTitle={pageTitle}/><main>{pageContent}</main></div><MobileNav page={page} onNavigate={navigate}/><AddPersonModal open={addOpen} onClose={() => setAddOpen(false)} persons={persons} onAdd={(person) => setPersons((current) => [...current, person])} showToast={showToast}/><Toast toast={toast} onClose={() => setToast(null)}/></div>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
