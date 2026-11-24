"use strict";
const $app = document.getElementById("app");
let pollTimer = null;

// ------------------------------------------------------------ utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => (n == null ? "–" : Number(n).toLocaleString("ko-KR"));
const fmtPct = (v) => (v == null ? "–" : (v * 100).toFixed(1) + "%");
const fmtUsd = (v) => (v == null ? "–" : "$" + Number(v).toFixed(4));
const fmtDt = (s) => (s ? new Date(s).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" }) : "–");
const fmtBytes = (b) => { if (!b) return "0 B"; const u = ["B", "KB", "MB", "GB", "TB"]; const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024))); return (b / 1024 ** i).toFixed(1) + " " + u[i]; };

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: opts.body ? { "content-type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof data === "object" ? (Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail) : data);
  return data;
}
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast("오류: " + e.message); throw e; }
}

const STATUS = {
  queued: ["대기", ""], searching: ["검색 중", "info"], downloading: ["다운로드 중", "info"],
  done: ["완료", "good"], partial: ["부분 완료", "warn"], failed: ["실패", "bad"],
  stored: ["저장됨", "good"], pending: ["대기", ""], expired_url: ["URL 재발급", "warn"], deleted: ["삭제됨", ""],
  approved: ["승인", "good"], rejected: ["반려", "bad"],
};
const ICON = { good: "✓ ", warn: "! ", bad: "✕ ", info: "• ", "": "" };
const pill = (s) => { const [l, c] = STATUS[s] || [s || "–", ""]; return `<span class="pill ${c}">${ICON[c]}${esc(l)}</span>`; };
const SOURCE = { reels_serp: "릴스 검색", hashtag_top: "해시태그·인기", hashtag_recent: "해시태그·최신" };

// ------------------------------------------------------------ router
const routes = [
  [/^#\/keywords$/, viewKeywords],
  [/^#\/runs\/(\d+)$/, viewRun],
  [/^#\/runs$/, viewRuns],
  [/^#\/dashboard$/, viewDashboard],
  [/^#\/retention$/, viewRetention],
];
async function route() {
  clearInterval(pollTimer);
  const h = location.hash || "#/keywords";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", h.startsWith(a.getAttribute("href"))));
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) { try { await fn(...m.slice(1)); } catch (e) { $app.innerHTML = `<div class="card">불러오기 실패: ${esc(e.message)}</div>`; } return; }
  }
  location.hash = "#/keywords";
}
window.addEventListener("hashchange", route);
function poll(fn, ms = 3000) { clearInterval(pollTimer); pollTimer = setInterval(fn, ms); }

// ------------------------------------------------------------ 키워드
async function viewKeywords() {
  const kws = await api("/keywords");
  $app.innerHTML = `
    <h1>키워드</h1><p class="sub">키워드를 등록하고 "수집 실행"을 누르면 릴스 검색 순위대로 공개 릴스를 수집하고 영상·썸네일을 저장합니다. 수집은 수동으로만 실행됩니다.</p>
    <div class="card"><h2>새 키워드</h2>
      <form id="kw-form" class="row">
        <label>키워드<input id="kw" required placeholder="예: 홈카페" size="22"></label>
        <label>목표 수량<input id="kw-target" type="number" min="1" max="1000" value="100" style="width:90px"></label>
        <label>로케일<input id="kw-locale" value="ko_KR" size="7"></label>
        <button class="btn">등록</button>
      </form></div>
    <div class="card table-wrap"><table>
      <thead><tr><th>키워드</th><th class="num">목표</th><th>로케일</th><th>최근 실행</th><th>상태</th><th></th></tr></thead>
      <tbody>${kws.map((k) => `
        <tr>
          <td><strong>${esc(k.keyword)}</strong> ${k.is_active ? "" : '<span class="pill">비활성</span>'}</td>
          <td class="num">${k.target_count}</td><td>${esc(k.locale)}</td>
          <td>${k.last_run ? `<a href="#/runs/${k.last_run.id}">#${k.last_run.id}</a> ${pill(k.last_run.status)}
              <div class="small muted">${k.last_run.found_count}개 / 영상 ${k.last_run.downloaded_count} · ${fmtDt(k.last_run.created_at)}</div>` : '<span class="muted">없음</span>'}</td>
          <td><button class="btn ghost sm" data-toggle="${k.id}" data-active="${k.is_active}">${k.is_active ? "비활성화" : "활성화"}</button></td>
          <td style="white-space:nowrap">
            <button class="btn sm" data-run="${k.id}" ${k.is_active ? "" : "disabled"}>수집 실행</button>
            <button class="btn ghost sm" data-edit="${k.id}">설정</button>
          </td>
        </tr>
        <tr id="edit-${k.id}" hidden><td colspan="6"><div class="row">
          <label>목표 수량<input id="e${k.id}-target" type="number" value="${k.target_count}" style="width:90px"></label>
          <label>로케일<input id="e${k.id}-locale" value="${esc(k.locale)}" size="7"></label>
          <button class="btn sm" data-save="${k.id}">저장</button></div></td></tr>`).join("") || '<tr><td colspan="6" class="muted">등록된 키워드가 없습니다.</td></tr>'}
      </tbody></table></div>`;

  document.getElementById("kw-form").onsubmit = async (e) => {
    e.preventDefault();
    await act(() => api("/keywords", { method: "POST", body: {
      keyword: document.getElementById("kw").value, target_count: +document.getElementById("kw-target").value,
      locale: document.getElementById("kw-locale").value } }), "등록했습니다");
    viewKeywords();
  };
  $app.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    const r = await act(() => api(`/keywords/${b.dataset.run}/runs`, { method: "POST" }), "수집을 시작했습니다");
    location.hash = `#/runs/${r.id}`;
  }));
  $app.querySelectorAll("[data-toggle]").forEach((b) => (b.onclick = async () => {
    await act(() => api(`/keywords/${b.dataset.toggle}`, { method: "PATCH", body: { is_active: b.dataset.active !== "true" } }));
    viewKeywords();
  }));
  $app.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => { const r = document.getElementById("edit-" + b.dataset.edit); r.hidden = !r.hidden; }));
  $app.querySelectorAll("[data-save]").forEach((b) => (b.onclick = async () => {
    const id = b.dataset.save;
    await act(() => api(`/keywords/${id}`, { method: "PATCH", body: {
      target_count: +document.getElementById(`e${id}-target`).value, locale: document.getElementById(`e${id}-locale`).value } }), "저장했습니다");
    viewKeywords();
  }));
}

// ------------------------------------------------------------ 실행 목록
function runProgressBar(r) {
  const tgt = r.target_count || 100;
  return `<div class="progress" title="검색 ${r.found_count}/${tgt}"><span style="width:${Math.min(100, (r.found_count / tgt) * 100)}%"></span></div>
    <div class="small muted">검색 ${r.found_count}/${tgt} · 영상 ${r.downloaded_count}</div>`;
}
async function viewRuns() {
  const render = async () => {
    const runs = await api("/runs");
    $app.innerHTML = `<h1>실행</h1><p class="sub">최근 수집 실행. 진행 중인 실행은 자동으로 갱신됩니다.</p>
      <div class="card table-wrap"><table><thead><tr><th>#</th><th>키워드</th><th>상태</th><th>진행</th><th>제공사</th><th>표시</th><th class="num">비용</th><th>시작</th><th>종료</th></tr></thead>
      <tbody>${runs.map((r) => `<tr>
        <td><a href="#/runs/${r.id}">#${r.id}</a></td><td>${esc(r.keyword)}</td>
        <td>${pill(r.status)}</td><td>${runProgressBar(r)}</td>
        <td>${esc(r.provider)}${r.provider_switched_from ? `<div class="small muted">← ${esc(r.provider_switched_from)} 전환</div>` : ""}</td>
        <td>${r.screened ? '<span class="pill warn">차단 키워드</span> ' : ""}${r.search_exhausted ? '<span class="pill warn">검색 소진</span>' : ""}${r.error ? `<div class="small" style="color:var(--bad)">${esc(r.error).slice(0, 80)}</div>` : ""}</td>
        <td class="num">${fmtUsd(r.cost_usd)}</td><td class="small">${fmtDt(r.started_at || r.created_at)}</td><td class="small">${fmtDt(r.finished_at)}</td>
      </tr>`).join("") || '<tr><td colspan="9" class="muted">실행 기록이 없습니다.</td></tr>'}</tbody></table></div>`;
    return runs.some((r) => ["queued", "searching", "downloading"].includes(r.status));
  };
  if (await render()) poll(async () => { if (!(await render())) clearInterval(pollTimer); });
}

// ------------------------------------------------------------ 실행 상세
async function viewRun(rid) {
  let source = "all";
  const render = async () => {
    const [r, results, pages] = await Promise.all([api(`/runs/${rid}`), api(`/runs/${rid}/results`), api(`/runs/${rid}/raw-pages`)]);
    const p = r.progress;
    const rows = results.filter((x) => source === "all" || x.source === source);
    const counts = results.reduce((a, x) => ((a[x.source] = (a[x.source] || 0) + 1), a), {});
    $app.innerHTML = `
      <div class="row" style="justify-content:space-between;align-items:center">
        <div><h1>#${r.id} · ${esc(r.keyword)} ${pill(r.status)}</h1>
        <p class="sub">${esc(r.provider)}${r.provider_switched_from ? ` (← ${esc(r.provider_switched_from)} 전환)` : ""} · 시작 ${fmtDt(r.started_at)} · 종료 ${fmtDt(r.finished_at)} · 페이지 ${r.pages}</p></div>
        <a class="btn" href="/api/runs/${r.id}/export.csv">CSV 내려받기</a>
      </div>
      <div class="grid" style="margin-bottom:16px">
        <div class="stat"><div class="k">고유 릴스</div><div class="v">${r.found_count}<span class="muted" style="font-size:14px"> / ${r.target_count}</span></div>
          <div class="d">${r.screened ? "차단 키워드 · " : ""}${r.search_exhausted ? "검색 소진 · " : ""}${Object.entries(counts).map(([k, v]) => `${SOURCE[k] || k} ${v}`).join(" · ")}</div></div>
        <div class="stat"><div class="k">영상 저장</div><div class="v">${p.stored}</div>
          <div class="d">대기 ${p.pending} · 진행 ${p.downloading} · 실패 ${p.failed}</div></div>
        <div class="stat"><div class="k">저장률</div><div class="v">${fmtPct(r.found_count ? p.stored / r.found_count : null)}</div><div class="d">완료 기준 95% 이상</div></div>
        <div class="stat"><div class="k">비용</div><div class="v">${fmtUsd(r.cost_usd)}</div><div class="d">api_usage 기준</div></div>
      </div>
      ${r.error ? `<div class="card"><strong style="color:var(--bad)">오류</strong><div class="note">${esc(r.error)}</div></div>` : ""}
      ${r.note ? `<div class="card"><strong>실행 메모</strong><div class="note">${esc(r.note)}</div></div>` : ""}
      <div class="card">
        <div class="tabs">${[["all", "전체"], ...Object.keys(SOURCE).map((k) => [k, SOURCE[k]])].map(([k, l]) => `<button data-src="${k}" class="${source === k ? "active" : ""}">${l} ${k === "all" ? results.length : counts[k] || 0}</button>`).join("")}</div>
        <div class="table-wrap"><table><thead><tr><th class="num">순위</th><th></th><th>릴스</th><th>작성자</th><th>캡션</th><th class="num">조회</th><th class="num">좋아요</th><th class="num">댓글</th><th class="num">공유</th><th>오디오</th><th>영상</th><th></th></tr></thead>
        <tbody>${rows.map((x) => `<tr>
          <td class="num"><strong>${x.rank}</strong><div class="small muted">p${x.page_no}</div></td>
          <td>${x.thumb_status === "stored" ? `<img class="thumb" loading="lazy" src="/api/media/${x.media_pk}/thumbnail" data-play="${x.media_pk}" alt="">` : '<div class="thumb"></div>'}</td>
          <td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.shortcode)}</a>
            <div class="small muted">${SOURCE[x.source] || x.source}</div>${x.is_available ? "" : '<span class="pill bad">원본 없음</span>'}
            <div class="small muted">${x.duration_sec ? x.duration_sec.toFixed(1) + "초" : ""} ${fmtDt(x.taken_at)}</div></td>
          <td>${esc(x.username)}${x.is_verified ? " ✔" : ""}<div class="small muted">${esc(x.full_name)}</div></td>
          <td><div class="caption small">${esc(x.caption)}</div></td>
          <td class="num">${fmtN(x.play_count)}</td><td class="num">${fmtN(x.like_count)}</td><td class="num">${fmtN(x.comment_count)}</td><td class="num">${fmtN(x.share_count)}</td>
          <td class="small">${esc(x.music_title)}<div class="muted">${esc(x.music_artist)}</div></td>
          <td>${pill(x.video_status)}${x.video_status === "stored" ? `<div class="small muted">${esc(x.video_resolution)} ${esc(x.video_codec)} · ${fmtBytes(x.video_bytes)}</div><div class="small muted">삭제 예정 ${fmtDt(x.delete_after)}</div>` : x.video_error ? `<div class="small" style="color:var(--bad)">${esc(x.video_error).slice(0, 60)}</div>` : ""}</td>
          <td style="white-space:nowrap">${x.video_status === "stored" ? `<button class="btn sm" data-play="${x.media_pk}">재생</button> <button class="btn ghost sm" data-ext="${x.media_pk}">보관 연장</button>` : ""}</td>
        </tr>`).join("") || '<tr><td colspan="12" class="muted">아직 결과가 없습니다.</td></tr>'}</tbody></table></div></div>
      <details class="card"><summary>제공사 원본 응답 ${pages.length}페이지 (30일 보관)</summary>
        <table><tbody>${pages.map((pg) => `<tr><td>p${pg.page_no}</td><td>${esc(pg.endpoint)}</td><td class="small muted">${esc(JSON.stringify(pg.cursor_in))}</td><td class="small">${fmtDt(pg.fetched_at)}</td><td><a href="/api/raw-pages/${pg.id}" target="_blank">JSON</a></td></tr>`).join("")}</tbody></table></details>`;

    $app.querySelectorAll("[data-src]").forEach((b) => (b.onclick = () => { source = b.dataset.src; render(); }));
    $app.querySelectorAll("[data-play]").forEach((b) => (b.onclick = () => openPlayer(results.find((x) => x.media_pk === b.dataset.play))));
    $app.querySelectorAll("[data-ext]").forEach((b) => (b.onclick = () => requestExtension(b.dataset.ext)));
    return ["queued", "searching", "downloading"].includes(r.status);
  };
  if (await render()) poll(async () => { if (document.getElementById("player").open) return; if (!(await render())) clearInterval(pollTimer); });
}

function openPlayer(x) {
  const d = document.getElementById("player");
  document.getElementById("player-title").textContent = `#${x.rank} ${x.shortcode} · @${x.username || ""}`;
  const v = document.getElementById("player-video");
  v.src = `/api/media/${x.media_pk}/video`;
  document.getElementById("player-meta").textContent = `${x.video_resolution || ""} ${x.video_codec || ""} · ${x.video_duration?.toFixed(2) || "?"}초 · SHA-256 ${x.video_sha256?.slice(0, 16)}… · 내부 분석 전용, 재배포 금지`;
  d.showModal();
  d.onclose = () => { v.pause(); v.removeAttribute("src"); v.load(); };
  v.play().catch(() => {});
}

async function requestExtension(pk) {
  const days = prompt("연장 일수 (최대 365)", "90"); if (!days) return;
  const reason = prompt("연장 사유 (승인 담당자가 검토합니다)"); if (!reason) return;
  const by = prompt("요청자 이름", "") || null;
  await act(() => api(`/reels/${pk}/extensions`, { method: "POST", body: { days: +days, reason, requested_by: by } }), "연장 요청을 등록했습니다. 보관·알림 화면에서 승인합니다.");
}

// ------------------------------------------------------------ 차트 (단일 계열: 범례 없음, 제목이 계열 이름)
const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true; document.body.appendChild(tip);
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (e) => { tip.textContent = el.dataset.tip; tip.hidden = false; tip.style.left = e.clientX + 12 + "px"; tip.style.top = e.clientY - 28 + "px"; });
    el.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}
function barChart(data, { fmt, labelEvery = 1 }) {
  const W = 600, H = 180, L = 48, B = 22, T = 8;
  const max = Math.max(...data.map((d) => d.v), 0) || 1;
  const bw = (W - L) / data.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [0, max / 2, max];
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">
    ${ticks.map((t) => `<line class="grid-line" x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/><text class="axis" x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${fmt(t)}</text>`).join("")}
    ${data.map((d, i) => { const h = Math.max(0, H - B - y(d.v)); const x = L + i * bw + 1; const w = Math.max(2, bw - 2);
      return `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent" data-tip="${esc(d.label)}: ${esc(fmt(d.v))}"/>
      ${h > 0 ? `<path class="bar" pointer-events="none" d="M${x},${H - B} V${y(d.v) + Math.min(4, h)} q0,-4 4,-4 H${x + w - 4} q4,0 4,4 V${H - B} Z"/>` : ""}
      ${i % labelEvery === 0 ? `<text class="axis" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(d.short)}</text>` : ""}`; }).join("")}
  </svg>`;
}
// ------------------------------------------------------------ 대시보드
async function viewDashboard(days = 7) {
  const d = await api(`/dashboard?days=${days}`);
  const statusOrder = ["done", "partial", "failed", "searching", "downloading", "queued"];
  const tile = (k, v, sub, ok) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${ok === undefined ? "" : ok ? '<span class="pill good">✓ 정상</span> ' : '<span class="pill bad">✕ 기준 미달</span> '}${sub}</div></div>`;
  $app.innerHTML = `<div class="row" style="justify-content:space-between;align-items:center"><div><h1>대시보드</h1><p class="sub">실행 현황, 실패 사유, 비용</p></div>
      <div class="tabs">${[1, 7, 30].map((n) => `<button data-days="${n}" class="${n === days ? "active" : ""}">${n === 1 ? "24시간" : n + "일"}</button>`).join("")}</div></div>
    <div class="grid" style="margin-bottom:16px">
      ${tile("실행 성공률", fmtPct(d.success_rate), "목표 90% · 알림 80% 미만", d.success_rate == null ? undefined : d.success_rate >= 0.9)}
      ${tile("영상 저장률", fmtPct(d.save_rate), "목표 95% · 알림 90% 미만", d.save_rate == null ? undefined : d.save_rate >= 0.95)}
      ${tile("제공사 오류율", fmtPct(d.provider_error_rate), `410 응답 ${d.gone_410}건 · 알림 15% 이상`, d.provider_error_rate == null ? undefined : d.provider_error_rate < 0.05 && !d.gone_410)}
      ${tile("총 비용", fmtUsd(d.total_cost), `실행당 평균 ${fmtUsd(d.avg_cost_per_run)}`)}
      ${tile("저장 용량", fmtBytes(d.storage_bytes), "보관 중인 영상·썸네일")}
    </div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(420px,1fr))">
      <div class="card chart"><h2>일별 제공사 비용 (USD)</h2>${barChart(d.cost_by_day.map((c) => ({ v: c.cost, label: c.day, short: c.day.slice(5) })), { fmt: (v) => "$" + v.toFixed(v < 1 ? 3 : 2), labelEvery: Math.ceil(d.cost_by_day.length / 10) })}</div>
    </div>
    <div class="grid">
      <div class="card"><h2>실행 상태 (${d.runs_total}회)</h2><table><tbody>${statusOrder.filter((s) => d.runs_by_status[s]).map((s) => `<tr><td>${pill(s)}</td><td class="num">${d.runs_by_status[s]}</td></tr>`).join("") || '<tr><td class="muted">없음</td></tr>'}</tbody></table></div>
      <div class="card"><h2>제공사 호출</h2><table><thead><tr><th>제공사</th><th class="num">호출</th><th class="num">오류</th><th class="num">비용</th></tr></thead><tbody>${Object.entries(d.providers).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmtN(v.calls)}</td><td class="num">${fmtN(v.errors)}</td><td class="num">${fmtUsd(v.cost)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">없음</td></tr>'}</tbody></table></div>
      <div class="card"><h2>실행 실패 사유</h2><table><tbody>${d.run_errors.map(([k, v]) => `<tr><td class="small">${esc(k)}</td><td class="num">${v}</td></tr>`).join("") || '<tr><td class="muted">없음</td></tr>'}</tbody></table></div>
      <div class="card"><h2>영상 다운로드 실패 사유</h2><table><tbody>${d.file_errors.map(([k, v]) => `<tr><td class="small">${esc(k)}</td><td class="num">${v}</td></tr>`).join("") || '<tr><td class="muted">없음</td></tr>'}</tbody></table></div>
    </div>`;
  $app.querySelectorAll("[data-days]").forEach((b) => (b.onclick = () => viewDashboard(+b.dataset.days)));
  bindTips($app);
}

// ------------------------------------------------------------ 보관·알림
async function viewRetention() {
  const [alerts, exts] = await Promise.all([api("/alerts"), api("/extensions")]);
  const lvl = { critical: "bad", warning: "warn" };
  $app.innerHTML = `<h1>보관·알림</h1><p class="sub">보관 기한: 영상 90일 · 메타데이터 1년 · 원본 응답 30일. 자동 삭제는 없으며 \"보관 정리 지금 실행\"을 눌러야 기한이 지난 데이터가 지워집니다. 알림도 \"지금 점검\"을 눌렀을 때만 확인합니다. 연장은 승인 후 반영됩니다.</p>
    <div class="card"><div class="row" style="justify-content:space-between;align-items:center"><h2 style="margin:0">알림</h2>
      <div><button class="btn ghost sm" id="eval">지금 점검</button> <button class="btn ghost sm" id="ret">보관 정리 지금 실행</button></div></div>
      <table style="margin-top:12px"><tbody>${alerts.map((a) => `<tr><td><span class="pill ${lvl[a.level] || "warn"}">${a.level === "critical" ? "✕ 심각" : "! 경고"}</span></td><td>${esc(a.message)}<div class="small muted">${esc(a.code)}</div></td><td class="small">${fmtDt(a.created_at)}</td>
        <td>${a.acknowledged ? '<span class="muted small">확인됨</span>' : `<button class="btn ghost sm" data-ack="${a.id}">확인</button>`}</td></tr>`).join("") || '<tr><td class="muted">알림이 없습니다.</td></tr>'}</tbody></table></div>
    <div class="card table-wrap"><h2>보관 연장 요청</h2><table><thead><tr><th>릴스</th><th class="num">일수</th><th>사유</th><th>요청자</th><th>상태</th><th>현재 삭제 예정</th><th></th></tr></thead>
      <tbody>${exts.map((e) => `<tr><td><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.shortcode)}</a></td><td class="num">${e.days}</td><td class="small">${esc(e.reason)}</td><td>${esc(e.requested_by)}</td>
        <td>${pill(e.status)}${e.decided_by ? `<div class="small muted">${esc(e.decided_by)} · ${fmtDt(e.decided_at)}</div>` : ""}</td><td class="small">${fmtDt(e.delete_after)}</td>
        <td style="white-space:nowrap">${e.status === "pending" ? `<button class="btn sm" data-dec="approve" data-id="${e.id}">승인</button> <button class="btn danger sm" data-dec="reject" data-id="${e.id}">반려</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">요청이 없습니다. 실행 상세 화면의 "보관 연장"으로 요청합니다.</td></tr>'}</tbody></table></div>`;
  document.getElementById("eval").onclick = async () => { const r = await act(() => api("/admin/alerts/evaluate", { method: "POST" })); toast(r.new.length ? `새 알림 ${r.new.length}건` : "새 알림 없음"); viewRetention(); };
  document.getElementById("ret").onclick = async () => { const r = await act(() => api("/admin/retention/run", { method: "POST" })); toast(`영상 ${r.files_deleted} · 원본 응답 ${r.raw_pages_deleted} · 릴스 ${r.reels_deleted} 삭제`); viewRetention(); };
  $app.querySelectorAll("[data-ack]").forEach((b) => (b.onclick = async () => { await act(() => api(`/alerts/${b.dataset.ack}/ack`, { method: "POST" })); viewRetention(); }));
  $app.querySelectorAll("[data-dec]").forEach((b) => (b.onclick = async () => {
    const who = prompt("처리자 이름"); if (!who) return;
    await act(() => api(`/extensions/${b.dataset.id}/${b.dataset.dec}`, { method: "POST", body: { decided_by: who } }), "처리했습니다");
    viewRetention();
  }));
}

// ------------------------------------------------------------ boot
api("/providers").then((ps) => {
  document.getElementById("provider-badge").innerHTML = ps.map((p) => `${p.role === "primary" ? "주" : "보조"}: <strong>${esc(p.name)}</strong> ${p.ok ? "✓" : `<span style="color:var(--bad)" title="${esc(p.detail)}">✕</span>`}`).join(" · ");
}).catch(() => {});
route();
