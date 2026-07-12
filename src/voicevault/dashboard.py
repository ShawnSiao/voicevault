from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from .collections import list_reports
from .diagnostics import inspect_kb
from .events import list_events
from .kb import KnowledgeBase
from .roles import list_role_summaries
from .sync import read_capture_status, read_sync_status


def write_dashboard(kb: KnowledgeBase, out_dir: Path | None = None) -> Path:
    target_dir = out_dir or kb.exports_dir / "dashboard"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "index.html"
    path.write_text(_html(kb), encoding="utf-8", newline="\n")
    return path


def _html(kb: KnowledgeBase) -> str:
    report = inspect_kb(kb)
    roles = list_role_summaries(kb)
    events = list_events(kb)
    reports = list_reports(kb)
    sync_status = read_sync_status(kb)
    capture_status = read_capture_status(kb)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VoiceVault Dashboard</title>
  <style>
    :root {{
      --bg: #f4f6f8;
      --surface: #ffffff;
      --surface-alt: #eef6f5;
      --border: #d7dde3;
      --text: #15232a;
      --muted: #65717b;
      --accent: #116d6e;
      --warning: #b35c00;
      --error: #b34055;
      --ok: #228b5a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft YaHei", "Noto Sans SC", Arial, sans-serif;
      line-height: 1.65;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 32px; }}
    header {{ display: grid; grid-template-columns: 1fr auto; gap: 24px; align-items: start; margin-bottom: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 30px; line-height: 1.2; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    p {{ margin: 0; color: var(--muted); }}
    .badge {{ display: inline-flex; align-items: center; min-height: 34px; padding: 0 12px; border: 1px solid #b7d8d4; border-radius: 8px; color: var(--accent); background: #e9f5f3; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .panel {{ border: 1px solid var(--border); border-radius: 8px; background: var(--surface); padding: 18px; box-shadow: 0 8px 18px rgba(20,43,54,.07); }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; font-weight: 700; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 30px; line-height: 1; }}
    .layout {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 18px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 8px; border-top: 1px solid var(--border); text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    code {{ color: var(--accent); font-family: "Cascadia Mono", Consolas, monospace; font-size: 13px; }}
    .status-ok {{ color: var(--ok); }}
    .status-warn {{ color: var(--warning); }}
    .status-error {{ color: var(--error); }}
    .stack {{ display: grid; gap: 18px; }}
    .note {{ margin-top: 14px; padding: 12px; border-radius: 8px; background: var(--surface-alt); border: 1px solid var(--border); color: var(--muted); font-size: 13px; }}
    @media (max-width: 860px) {{
      main {{ padding: 20px; }}
      header, .layout, .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>声迹 VoiceVault</h1>
        <p>本地优先的公开观点归档与投资角色分析工作台。No runtime server required.</p>
      </div>
      <span class="badge">{_status_text(report["ok"])}</span>
    </header>
    <section class="grid" aria-label="Metrics">
      {_metric("Roles", report["role_count"])}
      {_metric("Statements", report["statement_count"])}
      {_metric("Events", len(events))}
      {_metric("Reports", len(reports))}
      {_metric("Pending Files", capture_status.get("pending_count", 0))}
      {_metric("Sync Errors", len((sync_status.get("last_result") or {}).get("errors", [])))}
    </section>
    <section class="layout">
      <div class="stack">
        {_roles_panel(roles)}
        {_events_panel(events)}
        {_reports_panel(reports)}
      </div>
      <div class="stack">
        {_sync_panel(sync_status)}
        {_capture_panel(capture_status)}
        {_paths_panel(kb, report)}
      </div>
    </section>
  </main>
</body>
</html>
"""


def _metric(label: str, value: Any) -> str:
    return f'<article class="panel metric"><span>{escape(str(label))}</span><strong>{escape(str(value))}</strong></article>'


def _roles_panel(roles: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"<tr><td><code>{escape(role['role_id'])}</code></td><td>{escape(role['profile_status'])}</td><td>{role['statement_count']}</td></tr>"
        for role in roles[:12]
    )
    return f"""<section class="panel">
  <h2>Roles</h2>
  <table><thead><tr><th>Role</th><th>Profile</th><th>Statements</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def _events_panel(events: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"<tr><td>{escape(event['date'])}</td><td><code>{escape(event['event_id'])}</code></td><td>{escape(event['title'])}</td></tr>"
        for event in events[:12]
    )
    return f"""<section class="panel">
  <h2>Events</h2>
  <table><thead><tr><th>Date</th><th>Event</th><th>Title</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def _reports_panel(reports: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(report['generated_at'] or 'unknown')}</td>"
        f"<td>{escape(report['title'])}</td>"
        f"<td>{escape(str(report['matches']))}</td>"
        f"<td><code>{escape(report['kind'])}</code></td>"
        "</tr>"
        for report in reports[:12]
    )
    return f"""<section class="panel">
  <h2>Reports</h2>
  <table><thead><tr><th>Generated</th><th>Title</th><th>Matches</th><th>Kind</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def _sync_panel(status: dict[str, Any]) -> str:
    last = status.get("last_result") or {}
    errors = last.get("errors", [])
    error_rows = "".join(
        f"<li><code>{escape(error.get('source_file', ''))}</code>: {escape(error.get('message', ''))}</li>"
        for error in errors[:6]
    )
    if not error_rows:
        error_rows = "<li>No sync errors recorded.</li>"
    status_class = "status-ok" if status.get("ok") else "status-error"
    return f"""<section class="panel">
  <h2>Sync Status</h2>
  <p class="{status_class}">{_status_text(bool(status.get('ok')))}</p>
  <div class="note">Last run: {escape(str(status.get('last_run_at') or 'never'))}</div>
  <ul>{error_rows}</ul>
</section>"""


def _capture_panel(status: dict[str, Any]) -> str:
    summary = status.get("summary") or {}
    status_class = "status-ok" if status.get("ok") and status.get("pending_count") == 0 else "status-error"
    return f"""<section class="panel">
  <h2>Capture Status</h2>
  <p class="{status_class}">{_status_text(bool(status.get('ok') and status.get('pending_count') == 0))}</p>
  <div class="note">Pending Files: {escape(str(status.get('pending_count', 0)))}</div>
  <table><thead><tr><th>Processed</th><th>Failed</th><th>Records</th><th>Written</th><th>Duplicates</th></tr></thead><tbody><tr><td>{escape(str(summary.get('processed', 0)))}</td><td>{escape(str(summary.get('failed', 0)))}</td><td>{escape(str(summary.get('records_seen', 0)))}</td><td>{escape(str(summary.get('notes_written', 0)))}</td><td>{escape(str(summary.get('duplicates_skipped', 0)))}</td></tr></tbody></table>
</section>"""


def _paths_panel(kb: KnowledgeBase, report: dict[str, Any]) -> str:
    return f"""<section class="panel">
  <h2>Local Paths</h2>
  <p>Knowledge base</p>
  <code>{escape(str(kb.root))}</code>
  <div class="note">Index: <code>{escape(str(report['index_path']))}</code></div>
</section>"""


def _status_text(ok: bool) -> str:
    return "Status: ok" if ok else "Status: needs attention"
