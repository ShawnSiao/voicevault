(function () {
  const API = window.VoiceVaultApi;
  const root = document.getElementById("root");
  const pages = [
    ["people", "人物与账号", "users"],
    ["collect", "采集交接", "collect"],
    ["knowledge", "知识库索引", "database"],
    ["ask", "多人物问答", "chat"],
    ["runtime", "运行状态", "activity"],
  ];
  const state = {
    page: "people",
    connected: false,
    busy: false,
    message: "正在连接本地资源 API…",
    error: "",
    persons: [],
    collectionJobs: [],
    indexJobs: [],
    capabilities: null,
    retrieval: null,
    question: null,
    evidence: null,
    coverage: null,
    collectionDraft: { account_id: "", mode: "normal", start_date: "", end_date: "" },
  };

  const esc = (value) => String(value == null ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const accountOptions = () => state.persons.flatMap((person) =>
    (person.accounts || []).map((account) => ({ person, account }))
  );
  const INDEX_TERMINAL_STATUSES = new Set(["succeeded", "failed", "interrupted"]);
  const INDEX_ACTIVE_STATUSES = new Set(["pending", "running"]);
  const INDEX_STATUS_LABELS = {
    pending: "待执行",
    running: "构建中",
    succeeded: "已完成",
    failed: "失败",
    interrupted: "已中断",
  };
  const latestIndexJob = (personId) => state.indexJobs
    .filter((job) => job.person_id === personId).at(-1) || null;
  const upsertIndexJob = (job) => {
    state.indexJobs = state.indexJobs.filter((item) => item.job_id !== job.job_id);
    state.indexJobs.push(job);
  };

  async function load() {
    try {
      const [people, jobs, indexes, capabilities] = await Promise.all([
        API.get("/api/persons"),
        API.get("/api/collection-jobs"),
        API.get("/api/index-jobs"),
        API.get("/api/capabilities"),
      ]);
      state.persons = people.persons;
      state.collectionJobs = jobs.jobs;
      state.indexJobs = indexes.jobs;
      state.capabilities = capabilities.capabilities;
      state.connected = true;
      state.error = "";
      state.message = "本地资源已同步";
    } catch (error) {
      state.connected = false;
      state.error = error.message || "本地服务未连接";
      state.message = "本地服务未连接";
    }
    render();
  }

  function shell(content) {
    return `<div class="app-shell">
      <aside class="sidebar">
        <div class="brand"><span class="brand-mark"><i></i><i></i><i></i></span><span><strong>声迹</strong><small>VoiceVault</small></span></div>
        <div class="local-scope"><span><strong>仅本机</strong><small>资源 API · 单用户研究</small></span></div>
        <nav>${pages.map(([id, label], index) => `<button class="nav-item ${state.page === id ? "is-active" : ""}" data-page="${id}"><span>${index + 1}</span>${label}</button>`).join("")}</nav>
        <div class="sidebar-note"><strong>人物知识库 MVP</strong><span>实时本地状态 · 不读取 Cookie</span></div>
      </aside>
      <main class="main-shell">
        <div class="top-status"><strong>${esc(pages.find(([id]) => id === state.page)[1])}</strong><span class="status-pill ${state.connected ? "status-success" : "status-warning"}">${esc(state.message)}</span></div>
        ${state.error ? `<div class="proof-box proof-warning"><strong>本地服务未连接</strong><p>${esc(state.error)}</p><button class="button button-secondary" data-action="reload">重新连接</button></div>` : ""}
        ${content}
      </main>
    </div>`;
  }

  function peoplePage() {
    return `<section class="page"><header class="page-intro"><div><p class="eyebrow">人物知识库边界</p><h1>人物与账号</h1><p class="page-description">创建人物并绑定公开平台唯一账号；页面不直接访问平台。</p></div></header>
      <section class="metrics"><article class="metric-card"><p>人物</p><strong>${state.persons.length}</strong></article><article class="metric-card"><p>归档帖子</p><strong>${state.persons.reduce((n,p)=>n+(p.archive?.post_count||0),0)}</strong></article></section>
      <section class="panel"><header class="panel-header"><h2>本地人物</h2></header><div class="action-table-scroll"><table><thead><tr><th>人物</th><th>账号</th><th>帖子</th><th>索引</th></tr></thead><tbody>${state.persons.map((p)=>`<tr><td>${esc(p.display_name)}</td><td>${(p.accounts||[]).map((a)=>`${esc(a.platform)} @${esc(a.external_user_id)}`).join("<br>")||"—"}</td><td>${p.archive?.post_count||0}</td><td>${esc(p.index_head?.retrieval_mode||"未建立")}</td></tr>`).join("")||'<tr><td colspan="4">暂无人物。创建后再绑定雪球账号。</td></tr>'}</tbody></table></div></section>
      <form id="person-form" class="panel form-stack"><h2>添加人物与雪球账号</h2><input name="display_name" placeholder="人物名称" required><input name="external_user_id" placeholder="雪球数字用户 ID" required><label><input type="checkbox" name="confirmed" required> 已确认仅归档公开内容</label><button class="button button-primary" ${state.busy?"disabled":""}>创建并绑定</button></form></section>`;
  }

  function collectPage() {
    const options = accountOptions();
    const latest = state.collectionJobs.at(-1);
    const instruction = latest?.active_handoff ? `执行声迹采集交接 ${latest.active_handoff.handoff_id}` : "";
    return `<section class="page"><header class="page-intro"><div><p class="eyebrow">当前 Codex 桌面任务执行</p><h1>采集交接</h1><p class="page-description">先检查覆盖，再创建本地任务；网页不会读取 Cookie 或启动 Codex。</p></div></header>
      <form id="collection-form" class="panel form-stack"><select name="account_id" required><option value="">选择平台账号</option>${options.map(({person,account})=>`<option value="${esc(account.account_id)}" data-person="${esc(person.person_id)}" ${state.collectionDraft.account_id===account.account_id?"selected":""}>${esc(person.display_name)} · ${esc(account.platform)} @${esc(account.external_user_id)}</option>`).join("")}</select><select name="mode"><option value="normal" ${state.collectionDraft.mode==="normal"?"selected":""}>普通采集</option><option value="recheck" ${state.collectionDraft.mode==="recheck"?"selected":""}>追加复查</option></select><input type="date" name="start_date" value="${esc(state.collectionDraft.start_date)}" required><input type="date" name="end_date" value="${esc(state.collectionDraft.end_date)}" required><div class="button-row"><button type="button" class="button button-secondary" data-action="coverage">检查本地覆盖</button><button class="button button-primary" ${state.busy?"disabled":""}>创建采集任务</button></div>${state.coverage?`<div class="proof-box"><strong>${state.coverage.proof_complete?"区间已有完整覆盖":"存在待采集区间"}</strong><p>已覆盖 ${state.coverage.covered.length} 段，待采集 ${state.coverage.missing.length} 段。</p></div>`:""}</form>
      <section class="panel"><h2>任务状态</h2>${latest?`<p><span class="status-pill">${esc(latest.status)}</span> · 远端动作 ${latest.remote_action_count||0}</p>${instruction?`<div class="handoff-code"><code>${esc(instruction)}</code><button data-copy="${esc(instruction)}">复制</button></div>`:""}<div class="button-row"><button class="button button-secondary" data-action="poll-collection" data-id="${esc(latest.job_id)}">刷新</button><button class="button button-ghost" data-action="cancel-collection" data-id="${esc(latest.job_id)}">取消</button>${latest.status==="interrupted"?`<button class="button" data-action="resume-collection" data-id="${esc(latest.job_id)}">恢复</button>`:""}</div>`:"<p>暂无采集任务。</p>"}</section></section>`;
  }

  function knowledgePage() {
    const rows = state.persons.map((person) => {
      const latest = latestIndexJob(person.person_id);
      const active = INDEX_ACTIVE_STATUSES.has(latest?.status);
      const status = latest ? INDEX_STATUS_LABELS[latest.status] || latest.status : "未创建";
      const errorCode = latest?.error?.code;
      return `<tr><td>${esc(person.display_name)}</td><td>${person.archive?.post_count || 0}</td><td>${esc(person.index_head?.generation_id || "—")}</td><td>${esc(person.index_head?.retrieval_mode || "未建立")}</td><td><span class="status-pill">${esc(status)}</span></td><td>${errorCode ? `<code>${esc(errorCode)}</code>` : "—"}</td><td><button class="button button-secondary" data-action="index" data-id="${esc(person.person_id)}" ${active || state.busy ? "disabled" : ""}>${active ? "索引构建中" : "重建索引"}</button></td></tr>`;
    }).join("");
    return `<section class="page"><header class="page-intro"><div><p class="eyebrow">全文 + embedding</p><h1>知识库索引</h1><p class="page-description">每个人物独立建立代次；embedding 未配置时明确降级为仅全文。</p></div></header><section class="panel"><div class="action-table-scroll"><table><thead><tr><th>人物</th><th>帖子</th><th>当前代次</th><th>模式</th><th>最新任务</th><th>稳定错误</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="7">暂无人物。</td></tr>'}</tbody></table></div></section></section>`;
  }

  function askPage() {
    const result = state.question?.result;
    return `<section class="page"><header class="page-intro"><div><p class="eyebrow">一份综合回答 · 逐人物引用</p><h1>多人物知识问答</h1><p class="page-description">选择 1–10 人物，先冻结检索，再由当前 Codex 任务提交结构化候选回答。</p></div></header>
      <form id="ask-form" class="panel form-stack"><textarea name="query" placeholder="输入问题" required></textarea><div class="person-select-grid">${state.persons.map((p)=>`<label><input type="checkbox" name="person_id" value="${esc(p.person_id)}"> ${esc(p.display_name)}</label>`).join("")}</div><button class="button button-primary" ${state.busy?"disabled":""}>开始检索与问答</button></form>
      <section class="panel"><h2>问答状态</h2>${state.question?`<p><span class="status-pill">${esc(state.question.status)}</span></p>${state.question.codex_instruction?`<div class="handoff-code"><code>${esc(state.question.codex_instruction)}</code><button data-copy="${esc(state.question.codex_instruction)}">复制</button></div>`:""}<div class="button-row"><button class="button button-secondary" data-action="question-refresh" data-id="${esc(state.question.run_id)}">刷新结果</button><button class="button button-secondary" data-action="evidence" data-id="${esc(state.question.run_id)}">读取冻结证据</button></div>`:"<p>尚未创建问答运行。</p>"}${result?`<article class="answer-card"><h3>综合结论</h3><p>${esc(result.combined_answer)}</p></article>`:""}${state.evidence?`<pre class="evidence-json">${esc(JSON.stringify(state.evidence,null,2))}</pre>`:""}</section></section>`;
  }

  function runtimePage() {
    const c = state.capabilities || {};
    return `<section class="page"><header class="page-intro"><div><p class="eyebrow">配置与本地状态</p><h1>运行状态</h1><p class="page-description">这里只读取本地配置状态，不探测外部模型。</p></div></header><section class="runtime-grid"><div class="panel capability-list"><h2>能力检查</h2>${Object.entries(c).map(([key,value])=>`<article><strong>${esc(key)}</strong><small>${esc(typeof value==="object"?JSON.stringify(value):value)}</small></article>`).join("")||"<p>本地服务未连接</p>"}</div><div class="panel"><h2>边界</h2><p>Cookie 留在浏览器；密钥来自环境变量；定时增量采集尚未实现。</p><button class="button button-secondary" data-action="reload">重新检查连接</button></div></section></section>`;
  }

  function render() {
    const page = state.page === "people" ? peoplePage() : state.page === "collect" ? collectPage() : state.page === "knowledge" ? knowledgePage() : state.page === "ask" ? askPage() : runtimePage();
    root.innerHTML = shell(page);
  }

  async function perform(action) {
    if (state.busy) return;
    state.busy = true; state.error = ""; render();
    try { await action(); await load(); }
    catch (error) { state.error = `${error.code ? `${error.code}: ` : ""}${error.message}`; }
    finally { state.busy = false; render(); }
  }

  root.addEventListener("click", (event) => {
    const page = event.target.closest("[data-page]");
    if (page) { state.page = page.dataset.page; render(); return; }
    const copy = event.target.closest("[data-copy]");
    if (copy) { navigator.clipboard?.writeText(copy.dataset.copy); return; }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "reload") return load();
    if (action === "coverage") {
      const form = document.getElementById("collection-form");
      const account = form.account_id.selectedOptions[0];
      const request = {
        person: account?.dataset.person,
        account_id: form.account_id.value,
        mode: form.mode.value,
        start_date: form.start_date.value,
        end_date: form.end_date.value,
      };
      state.collectionDraft = request;
      return perform(async()=>{
      if (!request.person || !request.account_id || !request.start_date || !request.end_date) throw new Error("请选择账号和完整日期范围");
      const q=new URLSearchParams({account_id:request.account_id,start_date:request.start_date,end_date:request.end_date});
      const payload=await API.get(`/api/persons/${encodeURIComponent(request.person)}/coverage?${q}`); state.coverage=payload.coverage;
      });
    }
    if (action === "index") return perform(async()=>{
      const accepted = await API.post("/api/index-jobs", {person_id: button.dataset.id});
      upsertIndexJob(accepted.job);
      render();
      const terminal = await API.poll(
        accepted.status_url,
        (payload) => INDEX_TERMINAL_STATUSES.has(payload.job.status),
        (payload) => { upsertIndexJob(payload.job); render(); }
      );
      upsertIndexJob(terminal.job);
    });
    if (action === "poll-collection") return perform(async()=>{ await API.get(`/api/collection-jobs/${encodeURIComponent(button.dataset.id)}`); });
    if (action === "cancel-collection") return perform(async()=>{ await API.post(`/api/collection-jobs/${encodeURIComponent(button.dataset.id)}/cancel`,{}); });
    if (action === "resume-collection") return perform(async()=>{ await API.post(`/api/collection-jobs/${encodeURIComponent(button.dataset.id)}/resume`,{}); });
    if (action === "question-refresh") return perform(async()=>{ const p=await API.get(`/api/question-runs/${encodeURIComponent(button.dataset.id)}`); state.question={...state.question,...p.run}; });
    if (action === "evidence") return perform(async()=>{ state.evidence=(await API.get(`/api/question-runs/${encodeURIComponent(button.dataset.id)}/evidence`)).bundle; });
  });

  root.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    if (form.id === "person-form") return perform(async()=>{
      const person=(await API.post("/api/persons",{display_name:form.display_name.value,aliases:[]})).person;
      await API.post(`/api/persons/${encodeURIComponent(person.person_id)}/accounts`,{platform:"xueqiu",external_user_id:form.external_user_id.value,archive_basis_confirmed:true});
    });
    if (form.id === "collection-form") {
      const request={account_id:form.account_id.value,mode:form.mode.value,start_date:form.start_date.value,end_date:form.end_date.value};
      state.collectionDraft=request;
      return perform(async()=>{ await API.post("/api/collection-jobs",request); });
    }
    if (form.id === "ask-form") return perform(async()=>{
      const people=[...form.querySelectorAll('input[name="person_id"]:checked')].map((item)=>item.value);
      if (!people.length || people.length>10) throw new Error("请选择 1–10 个人物");
      const accepted=await API.post("/api/retrieval-runs",{query:form.query.value,person_ids:people});
      const retrieval=await API.poll(accepted.status_url,(p)=>["succeeded","failed","interrupted"].includes(p.run.status),(p)=>{state.retrieval=p.run;render();});
      if(retrieval.run.status!=="succeeded") throw new Error("检索运行未成功");
      const question=await API.post("/api/question-runs",{retrieval_run_id:retrieval.run.run_id});
      state.question={...question.run,codex_instruction:question.codex_instruction,status_url:question.status_url,evidence_url:question.evidence_url};
    });
  });

  render();
  load();
})();
