'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => Number(n || 0).toLocaleString('ko-KR') + '원';
const asDate = d => new Date(d + 'T12:00:00+09:00');
const shortDate = d => asDate(d).toLocaleDateString('ko-KR', {timeZone:'Asia/Seoul',month:'numeric',day:'numeric'}).replace(/\.$/, '');
const shiftDate = (d,n) => { const date=asDate(d); date.setUTCDate(date.getUTCDate()+n); return date.toISOString().slice(0,10); };
const statusInfo = {approved:['✓','인증'],pending:['·','확인 대기'],rejected:['×','반려'],missed:['−','미인증'],rest:['☾','휴식'],upcoming:['·','예정'],inactive:['—','대상 아님'],open:['+','오늘']};
let selectedPeople=new Set((new URLSearchParams(location.search).get('members')||'').split(',').filter(Boolean).map(Number));
let state, csrf, selectedWeek = new URLSearchParams(location.search).get('week') || '', tab = new URLSearchParams(location.search).get('view')==='share'?'share':selectedWeek ? 'settlement' : 'home', busy = false, toastTimer, authIntent = false, photoObjectURL, adminCodesVisible = false, refreshId = 0;
const dialog = $('#dialog');
async function api(path, {method='GET', body}={}) {
  const options = {method, credentials:'same-origin', headers:{}};
  if (method !== 'GET') options.headers['X-CSRF-Token'] = csrf;
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) { options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  const response=await fetch(path,options);
  let result; try { result=await response.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.'); }
  if(!response.ok) throw new Error(result.error || '요청을 처리하지 못했습니다.');
  return result;
}
async function session() { const result=await api('/api/session'); csrf=result.csrf; return result; }
async function refresh() {
  const id=++refreshId;
  const next=await api('/api/state'+(selectedWeek&&tab!=='home'?'?week='+encodeURIComponent(selectedWeek):''));
  if(id!==refreshId) return;
  state=next; selectedWeek=state.week; render();
}
function toast(text) { clearTimeout(toastTimer); $('#toast').textContent=text; $('#toast').classList.add('show'); toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),4200); }
function closeDialog() { dialog.close(); if(photoObjectURL) {URL.revokeObjectURL(photoObjectURL);photoObjectURL=null;} }
function showDialog(title, content) {
  if(dialog.open) dialog.close();
  dialog.innerHTML=`<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button type="button" class="close" data-action="close" aria-label="닫기">×</button></div>${content}`;
  dialog.showModal();
}
const errorBox = '<p class="form-error" role="alert"></p>';
function formError(message) { const target=$('.form-error',dialog); if(target) target.textContent=message; else toast(message); }
function helpToggle(title, content) { return `<details class="guide"><summary>${esc(title)}</summary><div class="guide-content">${content}</div></details>`; }
function guide() {
 return helpToggle('이용 안내', `<p>당일 학습 표시와 연도·월이 보이는 월간 달력을 제출합니다. 하루 한 장, 한국 시간 자정 마감이며 당일 교체할 수 있습니다.</p><p>월~일 기준 주 6일 인증. 미인증일 중 하루는 자동 휴식, 나머지는 하루 1,000원입니다. 첫 주는 가입일부터 계산합니다.</p><p>사진을 올리면 인증 완료로 반영됩니다. 운영자가 문제가 있는 기록을 반려할 수 있으며, 주간 마감 후 벌금을 정산합니다.</p><p>날짜를 누르면 사진을 볼 수 있습니다. 사진과 정산은 누구나 열람할 수 있습니다. 사진은 업로드 후 3주(21일)가 지나면 삭제되며 인증·정산 기록은 유지됩니다.</p>${legend()}${state.viewer?.is_admin?'<p>운영자는 인증·휴식일을 수정할 수 있습니다. 기록 검토·수정 시 해당 주의 납부 확인이 초기화됩니다.</p>':''}`);
}
function legend() { return '<div class="legend" aria-label="기록 표시 안내"><span><i class="dot dot-approved"></i>인증</span><span><i class="dot dot-missed"></i>미인증·반려</span><span><i class="dot dot-rest"></i>휴식</span></div>'; }
function weekbar() { return `<div class="weekbar"><div class="week-picker"><button data-action="week-prev" aria-label="지난주">‹</button><strong>${shortDate(state.week)} — ${shortDate(state.week_end)}</strong><button data-action="week-next" aria-label="다음 주" ${state.week>=state.current_week?'disabled':''}>›</button></div>${state.week!==state.current_week?'<button class="link-button" data-action="week-current">이번 주</button>':`<span class="badge ${state.closed?'':'green'}">${state.closed?'정산 주간':'이번 주'}</span>`}</div>`; }
function days(member) {
  return `<div class="days">${member.days.map((day,i)=>{const [symbol,label]=statusInfo[day.status]||statusInfo.missed; return `<div class="day ${day.date===state.today?'today':''}"><div class="day-label">${['월','화','수','목','금','토','일'][i]} <span>${Number(day.date.slice(-2))}</span></div><button class="day-button status-${esc(day.status)}" data-action="day" data-member="${member.id}" data-date="${day.date}" aria-label="${esc(member.nickname)} ${day.date} ${label}${day.rest_manual?' 운영자 지정':''}" ${day.status==='inactive'?'disabled':''}><span aria-hidden="true">${symbol}</span><small>${label}</small></button></div>`;}).join('')}</div>`;
}
function memberCard(member, personal=false) {
  return `<section class="card member-card"><div class="section-head"><div>${personal?'<h2>이번 주</h2>':`<div class="member-title"><h3>${esc(member.nickname)}</h3>${member.id===state.viewer?.id?'<span class="badge green">나</span>':''}</div>`}${personal?`<p>${shortDate(state.week)} — ${shortDate(state.week_end)}</p>`:''}</div><div class="progress-number">${member.approved_count}<small> / ${member.target_days}일</small></div></div>${personal?`<progress class="progress-track" aria-label="이번 주 인증" max="${Math.max(1,member.target_days,member.approved_count)}" value="${member.approved_count}">${member.approved_count}일</progress>`:''}${days(member)}${member.pending_count?`<p class="pending-note">확인 대기 ${member.pending_count}일</p>`:''}${state.viewer?.is_admin?`<div class="member-controls"><button class="link-button" data-action="edit-record" data-member="${member.id}">기록 수정</button><button class="link-button" data-action="edit-rest" data-member="${member.id}">휴식일 설정</button></div>`:''}</section>`;
}
function home() {
  const mine=state.members.find(m=>m.id===state.viewer?.id), today=mine?.days.find(d=>d.date===state.today);
  const unpaidTotal=state.unpaid.reduce((sum,x)=>sum+x.amount,0);
  return `<div class="home-grid">${unpaidTotal?`<div class="notice"><strong>벌금 ${money(unpaidTotal)}</strong><button data-action="unpaid">내역</button></div>`:''}<section class="card upload-card"><h1 class="upload-date">${asDate(state.today).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul',month:'long',day:'numeric',weekday:'long'})}</h1><button class="primary" data-action="upload">${today?.submission?'사진 교체':'인증하기'}</button>${today?.submission?`<div class="upload-status"><span class="badge ${today.submission.status==='approved'?'green':today.submission.status==='pending'?'orange':'red'}">${today.submission.status==='approved'?'인증 완료':statusInfo[today.submission.status]?.[1]||'등록 완료'}</span><button class="link-button" data-action="day" data-member="${mine.id}" data-date="${state.today}">사진 보기</button></div>`:''}</section>${mine?memberCard(mine,true):''}</div>`;
}
function overview() {
 return `${weekbar()}${state.members.length?`<div class="member-grid">${state.members.map(m=>memberCard(m)).join('')}</div>`:'<div class="empty">참여자 없음</div>'}`;
}
function settlement() {
  return `${weekbar()}${!state.closed?'<p class="settlement-status"><span class="badge">집계 중</span></p>':''}${state.totals.pending?`<p class="settlement-status"><span class="badge orange">확인 대기 ${state.totals.pending}건</span></p>`:''}<div class="summary-grid"><div><small>확정 벌금</small><strong>${Number(state.totals.fine||0).toLocaleString()}<small>원</small></strong></div><div><small>납부 완료</small><strong>${Number(state.totals.paid||0).toLocaleString()}<small>원</small></strong></div><div><small>벌금</small><strong>${Number(state.totals.unpaid||0).toLocaleString()}<small>원</small></strong></div></div>${state.members.length?`<div class="table-wrap"><table class="settlement-table"><thead><tr><th scope="col">별명</th><th scope="col">인증</th><th scope="col">벌금</th><th scope="col">납부</th></tr></thead><tbody>${state.members.map(m=>`<tr><td>${esc(m.nickname)}${m.id===state.viewer?.id?' <span class="badge green">나</span>':''}</td><td>${m.approved_count}/${m.target_days}일</td><td>${m.settlement_status==='final'?money(m.fine):`<span class="muted text-small">${m.settlement_status==='pending'?'검토 중':'집계 중'}</span>`}</td><td>${m.settlement_status!=='final'?'<span class="muted">—</span>':!m.fine?'<span class="badge green">없음</span>':state.viewer?.is_admin?`<label class="payment-toggle"><input type="checkbox" aria-label="${esc(m.nickname)} 벌금 납부 완료" data-payment="${m.id}" ${m.paid?'checked':''}>${m.paid?'완료':'벌금'}</label>`:`<span class="badge ${m.paid?'green':'orange'}">${m.paid?'완료':'벌금'}</span>`}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">정산 내역 없음</div>'}`;
}
function admin() {
 return `<div class="admin-tools"><button class="quiet" data-action="tab" data-tab="share-edit">인증 공유</button><button class="quiet" data-action="share">정산 링크</button><button class="quiet" data-action="admin-codes">${adminCodesVisible?'식별코드 숨기기':'별명별 식별코드 보기'}</button><button class="quiet" data-action="tab" data-tab="settlement">벌금 납부 체크</button><button class="quiet" data-action="admin-logout">운영자 모드 종료</button></div><div id="admin-codes"></div>${weekbar()}<div class="member-grid">${state.members.map(m=>memberCard(m)).join('')}</div>`;
}
function render() {
 if(['admin','share-edit'].includes(tab)&&!state.viewer?.is_admin) tab='home';
 $('#app').innerHTML=`<div class="shell"><header class="header"><button class="brand" data-action="home" aria-label="영써티 홈"><span class="brand-mark" aria-hidden="true">✓</span>영써티 <small class="brand-en">engthirty</small></button><div class="header-actions">${state.viewer?.is_admin?'<span class="admin-pill">운영자</span>':''}<button class="user-button" data-action="${state.viewer?'profile':'auth'}">${state.viewer?esc(state.viewer.nickname)+' 님':'참여하기'}</button></div></header><nav class="nav" aria-label="주 메뉴">${[['home','내 인증'],['overview','전체 현황'],['settlement','주간 정산'],...(state.viewer?.is_admin?[['admin','운영']]:[])].map(([id,label])=>`<button data-action="tab" data-tab="${id}" class="${(tab===id||(id==='admin'&&tab==='share-edit'))?'active':''}" ${(tab===id||(id==='admin'&&tab==='share-edit'))?'aria-current="page"':''}>${label}</button>`).join('')}</nav><main id="main" tabindex="-1">${({home,overview,settlement,admin,share:sharePage,'share-edit':shareEditor})[tab]()}</main><footer class="footer">${guide()}<button data-action="admin-entry">${state.viewer?.is_admin?'운영 관리':'운영자 입장'}</button></footer></div>`;
 if(tab==='admin'&&adminCodesVisible) loadCodes().catch(error=>toast(error.message));
}
function auth(mode='login') {
 showDialog('입장',`<div class="auth-switch"><button data-action="auth-mode" data-mode="login" class="${mode==='login'?'active':''}">코드로 입장</button><button data-action="auth-mode" data-mode="register" class="${mode==='register'?'active':''}">처음 참여</button></div><form id="auth-form" data-mode="${mode}">${mode==='login'?'<label class="form-field"><span>내 식별코드</span><input name="code" type="text" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" minlength="4" placeholder="4자리 숫자" autocomplete="off" required autofocus></label>':'<label class="form-field"><span>별명</span><input name="nickname" maxlength="20" placeholder="예: 꾸준이" autocomplete="nickname" required autofocus></label>'}${errorBox}<button class="primary full" type="submit">${mode==='login'?'입장하기':'참여하고 코드 받기'}</button>${helpToggle('계정 안내','<p>같은 기기에서는 입장 상태가 유지됩니다. 다른 기기나 로그아웃 후에는 4자리 코드로 입장합니다. 코드는 본인만 보관하세요.</p>')}</form>`);
}
function profile() {
 showDialog('내 계정',`<form id="profile-form"><label class="form-field"><span>별명</span><input name="nickname" value="${esc(state.viewer.nickname)}" maxlength="20" required></label>${errorBox}<button class="primary full" type="submit">별명 저장</button></form>${helpToggle('코드 안내','<p>코드를 잊었다면 운영자에게 확인하세요.</p>')}<div class="dialog-actions"><button class="secondary" data-action="logout">이 기기에서 나가기</button></div>`);
}
function beginUpload() { if(!state.viewer) {authIntent=true;auth('login');return;} $('#photo-input').value=''; $('#photo-input').click(); }
async function uploadJPEG(file) {
 let picture, objectURL;
 try {
  if(!file||file.size>10*1024*1024||!['image/jpeg','image/png','image/webp'].includes(file.type)) throw new Error();
  if(globalThis.createImageBitmap) picture=await createImageBitmap(file,{imageOrientation:'from-image'});
  else {
   objectURL=URL.createObjectURL(file);picture=new Image();
   await new Promise((resolve,reject)=>{picture.onload=resolve;picture.onerror=reject;picture.src=objectURL;});
  }
  if(!picture.width||!picture.height||picture.width*picture.height>25000000) throw new Error();
  const scale=Math.min(1,1800/Math.max(picture.width,picture.height)), canvas=document.createElement('canvas');
  canvas.width=Math.max(1,Math.round(picture.width*scale));canvas.height=Math.max(1,Math.round(picture.height*scale));
  const context=canvas.getContext('2d');
  while(true){
   context.fillStyle='#fff';context.fillRect(0,0,canvas.width,canvas.height);
   context.drawImage(picture,0,0,canvas.width,canvas.height);
   for(const quality of [.85,.75,.65]){
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',quality));
    if(!blob||blob.type!=='image/jpeg')throw new Error();
    if(blob.size<=2000000)return blob;
   }
   if(canvas.width===1&&canvas.height===1)throw new Error();
   canvas.width=Math.max(1,Math.floor(canvas.width*.8));canvas.height=Math.max(1,Math.floor(canvas.height*.8));
  }
 }catch {throw new Error('사진을 처리할 수 없습니다. 다른 사진을 선택해 주세요.');}
 finally {picture?.close?.();if(objectURL)URL.revokeObjectURL(objectURL);}
}
function photoDay(memberId,date) {
 const member=state.members.find(m=>m.id===Number(memberId)), day=member?.days.find(d=>d.date===date); if(!day)return;
 const s=day.submission, label=statusInfo[day.status]?.[1]||day.status;
 showDialog(`${member.nickname} · ${shortDate(date)}`,`${s?.photo_url?`<img class="photo" src="${esc(s.photo_url)}" alt="${esc(member.nickname)}의 ${date} 인증 사진">`:`<div class="empty">${s?s.has_photo?'사진을 불러올 수 없습니다.':'사진 없음':day.status==='rest'?'휴식':day.status==='upcoming'?'예정':'사진 없음'}</div>`}<div class="record-meta"><span class="badge ${day.status==='approved'?'green':day.status==='pending'?'orange':''}">${label}</span>${day.rest_manual?'<span class="badge">운영자 지정 휴식</span>':''}</div>${s?.reason?helpToggle('검토 내용',`<p>${esc(s.reason)}</p>`):''}${s?.submitted_at?`<p class="photo-details">제출 ${esc(new Date(s.submitted_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}))}</p>`:''}${state.viewer?.is_admin&&s?`<form id="review-form" data-id="${s.id}"><label class="form-field"><span>검토 메모 (선택)</span><input name="reason" maxlength="300" placeholder="예: 당일 학습 표시가 확인되지 않아요"></label>${errorBox}<div class="dialog-actions"><button class="secondary danger" type="submit" name="status" value="rejected">반려</button><button class="primary" type="submit" name="status" value="approved">인증 승인</button></div></form>`:''}${state.viewer?.is_admin?`<div class="dialog-actions"><button class="secondary" data-action="edit-record" data-member="${member.id}" data-date="${date}">이 날짜 기록 수정</button></div>`:''}`);
}
function editRecord(memberId,date) {
 const member=state.members.find(m=>m.id===Number(memberId));
 showDialog(`${member.nickname} · 기록 수정`,`<form id="record-form"><input type="hidden" name="member_id" value="${member.id}"><label class="form-field"><span>날짜</span><input type="date" name="date" value="${date|| (state.week>member.joined_on?state.week:member.joined_on)}" min="${member.joined_on}" max="${state.today}" required></label><label class="form-field"><span>인증 상태</span><select name="status"><option value="approved">인증 승인</option><option value="rejected">인증 반려</option><option value="absent">미인증 (사진 삭제)</option></select></label><label class="form-field"><span>수정 메모 (선택)</span><textarea name="reason" maxlength="300"></textarea></label>${helpToggle('변경 안내','<p>기록 수정 시 해당 주의 납부 확인이 초기화됩니다. 미인증으로 변경하면 해당 기록과 사진이 삭제됩니다.</p>')}${errorBox}<div class="dialog-actions"><button class="primary" type="submit">변경 저장</button></div></form>`);
}
function editRest(memberId) {
 const member=state.members.find(m=>m.id===Number(memberId)), rest=member.days.find(d=>d.rest_manual);
 showDialog(`${member.nickname} · 휴식일`,`<form id="rest-form"><input type="hidden" name="member_id" value="${member.id}"><p class="text-small muted">${shortDate(state.week)} — ${shortDate(state.week_end)}</p><label class="form-field"><span>휴식일 지정</span><select name="date"><option value="">자동 지정으로 되돌리기</option>${member.days.filter(d=>d.status!=='inactive').map(d=>`<option value="${d.date}" ${rest?.date===d.date?'selected':''}>${shortDate(d.date)} (${['월','화','수','목','금','토','일'][member.days.indexOf(d)]})</option>`).join('')}</select></label>${helpToggle('휴식 안내','<p>주 1일 휴식. 자동 지정은 미인증일 중 하루를 선택합니다. 지정한 날에도 인증을 제출할 수 있습니다.</p>')}${errorBox}<div class="dialog-actions"><button class="primary" type="submit">설정 저장</button></div></form>`);
}
function adminLogin() {
 if(state.viewer?.is_admin){tab='admin';render();return;}
 if(!state.viewer){authIntent=false;auth();toast('먼저 내 식별코드로 입장한 뒤 운영자 입장을 눌러주세요.');return;}
 showDialog('운영자 입장',`${state.configured_admin?`<form id="admin-form"><label class="form-field"><span>운영자 비밀번호</span><input type="password" name="password" autocomplete="current-password" required></label>${errorBox}<button class="primary full" type="submit">운영자 모드 시작</button></form>`:'<p class="form-error">운영자 비밀번호가 아직 설정되지 않았습니다. 앱 관리자에게 설정을 요청해 주세요.</p>'}`);
}
async function loadCodes() {
 const result=await api('/api/admin/members');
 if(!adminCodesVisible||tab!=='admin'||!state.viewer?.is_admin)return;
 const members=Array.isArray(result)?result:result.members;
 const container=$('#admin-codes'); if(container) container.innerHTML=`<section class="card"><h3>별명별 식별코드</h3><div class="code-list">${members.map(m=>`<div class="code-row"><span>${esc(m.nickname)}</span><code>${m.ended_on?'참여 종료':esc(m.code)}</code>${!m.ended_on&&m.id!==state.viewer.id?`<button class="link-button" data-action="retire" data-member="${m.id}">참여 종료</button>`:''}</div>`).join('')}</div></section>`;
}
async function navigateWeek(week) { selectedWeek=week; const url=new URL(location.href);url.searchParams.set('week',week);history.replaceState(null,'',url); await refresh(); }
async function share() {
 requireShareAdmin();
 const url=new URL(location.href);url.search='';url.searchParams.set('week',state.week);
 const text=`${shortDate(state.week)} — ${shortDate(state.week_end)} 스터디 정산`;
 if(navigator.share) {try{await navigator.share({title:'영써티 · 주간 정산',text,url:url.href});}catch(error){if(error.name!=='AbortError')throw error;}}
 else if(navigator.clipboard) {await navigator.clipboard.writeText(url.href);toast('링크 복사 완료');}
 else {showDialog('정산 링크',`<label class="form-field"><span>링크를 복사해서 공유하세요</span><input value="${esc(url.href)}" readonly></label>`);$('input',dialog).select();}
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
 const action=button.dataset.action;
 try {
  switch(action){
   case 'retire':if(confirm('이 회원의 코드를 비활성화할까요? 기존 기록은 보존되며 내일부터 인증 의무가 종료됩니다.')){await api('/api/admin/members/'+button.dataset.member,{method:'DELETE'});await refresh();toast('참여 종료');}break;
   case 'export-image':await exportWeekImage();break;
   case 'share-week':await shareWeekLink();break;
   case 'close':closeDialog();break;
   case 'home':case 'week-current':if(action==='home')tab='home';await navigateWeek(state.current_week);break;
   case 'tab':tab=button.dataset.tab;if(tab==='home')await navigateWeek(state.current_week);else render();break;
   case 'auth':authIntent=false;auth();break;
   case 'auth-mode':auth(button.dataset.mode);break;
   case 'profile':profile();break;
   case 'upload':beginUpload();break;
   case 'continue-upload':closeDialog();if(authIntent){authIntent=false;beginUpload();}break;
   case 'day':photoDay(button.dataset.member,button.dataset.date);break;
   case 'week-prev':await navigateWeek(shiftDate(state.week,-7));break;
   case 'week-next':await navigateWeek(shiftDate(state.week,7));break;
   case 'unpaid':tab='settlement';await navigateWeek(state.unpaid[0].week);break;
   case 'share':await share();break;
   case 'admin-entry':adminLogin();break;
   case 'edit-record':editRecord(button.dataset.member,button.dataset.date);break;
   case 'edit-rest':editRest(button.dataset.member);break;
   case 'admin-codes':adminCodesVisible=!adminCodesVisible;render();break;
   case 'logout':await api('/api/logout',{method:'POST'});adminCodesVisible=false;closeDialog();await session();await refresh();toast('로그아웃');break;
   case 'admin-logout':await api('/api/admin/logout',{method:'POST'});adminCodesVisible=false;await session();tab='home';selectedWeek=state.current_week;await refresh();toast('운영자 모드 종료');break;
  }
 }catch(error){if(dialog.open)formError(error.message);else toast(error.message);}
});
document.addEventListener('submit',async event=>{
 const form=event.target;if(!form.id)return;event.preventDefault();if(busy)return;busy=true;
 const data=Object.fromEntries(new FormData(form));const submitter=event.submitter;const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
 const uploadButton=form.id==='upload-form'?form.querySelector('[type="submit"]'):null, uploadLabel=uploadButton?.textContent;
 if(uploadButton){uploadButton.textContent='등록 중';form.setAttribute('aria-busy','true');}
 const error=$('.form-error',form);if(error)error.textContent='';
 try{
  if(form.id==='auth-form'){
   const mode=form.dataset.mode,result=await api('/api/'+(mode==='register'?'register':'login'),{method:'POST',body:data});
   await session();selectedWeek=state.current_week;tab='home';await refresh();
   if(mode==='register')showDialog('내 식별코드',`<div class="code">${esc(result.code)}</div>${helpToggle('코드 안내','<p>재입장할 때 필요한 코드입니다. 따로 보관하고 분실하면 운영자에게 확인하세요.</p>')}<div class="dialog-actions"><button class="primary" data-action="continue-upload">${authIntent?'인증하기':'확인'}</button></div>`);
   else{closeDialog();toast('입장 완료');if(authIntent){authIntent=false;beginUpload();}}
  }else if(form.id==='profile-form'){await api('/api/profile',{method:'PATCH',body:data});closeDialog();await refresh();toast('저장 완료');}
  else if(form.id==='admin-form'){await api('/api/admin/login',{method:'POST',body:data});await session();closeDialog();tab='admin';await refresh();toast('운영자 모드');}
  else if(form.id==='upload-form'){
   const file=$('#photo-input').files[0], photo=await uploadJPEG(file), body=new FormData();body.append('photo',photo,'proof.jpg');const result=await api('/api/submissions',{method:'POST',body});closeDialog();selectedWeek=state.current_week;tab='home';await refresh();toast(result.status==='approved'?'인증 완료':'등록 완료');
  }else if(form.id==='review-form'){await api('/api/admin/submissions/'+form.dataset.id,{method:'POST',body:{status:submitter.value,reason:data.reason}});closeDialog();await refresh();toast('검토 완료');}
  else if(form.id==='record-form'){await api('/api/admin/records',{method:'PUT',body:{...data,member_id:Number(data.member_id)}});closeDialog();await refresh();toast('수정 완료');}
  else if(form.id==='rest-form'){await api('/api/admin/rest',{method:'PUT',body:{member_id:Number(data.member_id),week:state.week,date:data.date||null}});closeDialog();await refresh();toast('저장 완료');}
 }catch(error){formError(error.message);}finally{busy=false;buttons.forEach(b=>b.disabled=false);if(uploadButton){uploadButton.textContent=uploadLabel;form.removeAttribute('aria-busy');}}
});
$('#photo-input').addEventListener('change',event=>{
 const file=event.target.files[0];if(!file)return;
 if(file.size>10*1024*1024){toast('사진은 10MB 이하로 선택해 주세요.');event.target.value='';return;}
 if(!['image/jpeg','image/png','image/webp'].includes(file.type)){toast('JPG, PNG, WebP 사진을 선택해 주세요.');event.target.value='';return;}
 if(photoObjectURL)URL.revokeObjectURL(photoObjectURL);photoObjectURL=URL.createObjectURL(file);
 showDialog('오늘의 인증 사진',`<img class="photo" src="${photoObjectURL}" alt="업로드할 인증 사진 미리보기">${helpToggle('사진 안내',`<p>${shortDate(state.today)} 학습 표시와 연도·월이 보이는 월간 달력을 제출하세요. 사진은 2MB 이하로 압축해 업로드되며 누구나 열람할 수 있습니다. 업로드 후 3주(21일)가 지나면 삭제됩니다.</p>`)}<form id="upload-form">${errorBox}<div class="dialog-actions"><button type="button" class="secondary" data-action="close">취소</button><button type="submit" class="primary">사진 올리기</button></div></form>`);
});
document.addEventListener('change',async event=>{
 const input=event.target;if(!input.matches('[data-payment]'))return;
 input.disabled=true;
 try{await api('/api/admin/payments',{method:'PUT',body:{member_id:Number(input.dataset.payment),week:state.week,paid:input.checked}});await refresh();toast('저장 완료');}catch(error){input.checked=!input.checked;toast(error.message);}finally{input.disabled=false;}
});
dialog.addEventListener('close',()=>{if(photoObjectURL){URL.revokeObjectURL(photoObjectURL);photoObjectURL=null;}});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&state&&!dialog.open)refresh().catch(()=>{});});
(async()=>{try{await session();await refresh();}catch(error){$('#app').innerHTML=`<div class="shell"><main class="loading"><div class="error-panel">${esc(error.message)}<p class="help">페이지를 새로고침해서 다시 시도해 주세요.</p></div></main></div>`;}})();

function shareEditor(){
 requireShareAdmin();
 const chosen=state.members.filter(m=>selectedPeople.has(m.id));
 return `<button class="link-button" data-action="tab" data-tab="admin">‹ 운영</button>${weekbar()}<div class="share-picker">${state.members.map(m=>`<label><input type="checkbox" data-share-person="${m.id}" ${selectedPeople.has(m.id)?'checked':''}> ${esc(m.nickname)}</label>`).join('')}</div><div class="admin-tools"><button class="primary" data-action="export-image" ${chosen.length?'':'disabled'}>이미지 저장</button><button class="secondary" data-action="share-week" ${chosen.length?'':'disabled'}>공유 링크</button></div><div class="member-grid">${chosen.map(m=>memberCard(m)).join('')}</div>`;
}
document.addEventListener('change',event=>{const input=event.target;if(!input.matches('[data-share-person]')||!state.viewer?.is_admin||tab!=='share-edit')return;const id=Number(input.dataset.sharePerson);input.checked?selectedPeople.add(id):selectedPeople.delete(id);render();});
async function shareWeekLink(){
 requireShareAdmin();
 const url=new URL(location.origin);url.searchParams.set('week',state.week);url.searchParams.set('view','share');url.searchParams.set('members',state.members.filter(m=>selectedPeople.has(m.id)).map(m=>m.id).join(','));
 if(navigator.share){try{await navigator.share({title:'영써티 주간 인증',url:url.href});}catch(e){if(e.name!=='AbortError')throw e;}}
 else if(navigator.clipboard){await navigator.clipboard.writeText(url.href);toast('링크 복사 완료');}
 else showDialog('공유 링크',`<input aria-label="공유 링크" readonly value="${esc(url.href)}">`);
}
async function exportWeekImage(){
 requireShareAdmin();
 const members=state.members.filter(m=>selectedPeople.has(m.id));if(!members.length)return;
 await document.fonts.ready;
 const canvas=document.createElement('canvas');canvas.width=1000;canvas.height=160+members.length*180;
 const c=canvas.getContext('2d');c.fillStyle='#f7f9f7';c.fillRect(0,0,canvas.width,canvas.height);
 c.fillStyle='#20372e';c.font='bold 36px sans-serif';c.fillText('영써티',40,55);c.font='18px sans-serif';c.fillText('engthirty',165,55);c.font='24px sans-serif';c.fillText(`${state.week} ~ ${state.week_end}`,40,105);
 members.forEach((m,i)=>{const y=160+i*180;c.fillStyle='#20372e';c.font='bold 24px sans-serif';c.fillText(m.nickname,40,y,650);c.font='22px sans-serif';c.fillText(`${m.approved_count}/${m.target_days}일`,830,y);
 m.days.forEach((d,j)=>{const x=40+j*132;c.fillStyle=({approved:'#e8f3eb',pending:'#fff5df',missed:'#fbeeea',rejected:'#fbeeea',rest:'#edf1f6'})[d.status]||'#eef0ed';c.fillRect(x,y+18,120,85);c.fillStyle='#20372e';c.font='18px sans-serif';c.fillText(`${['월','화','수','목','금','토','일'][j]} ${Number(d.date.slice(-2))}`,x+12,y+46);c.fillText(statusInfo[d.status][1],x+12,y+80);});
 });
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.92));if(!blob)throw new Error('이미지 저장 실패');
 requireShareAdmin();
 const file=new File([blob],`engthirty-${state.week}.jpg`,{type:'image/jpeg'});
 const url=canvas.toDataURL('image/jpeg',.92), canShare=navigator.canShare?.({files:[file]})&&navigator.share;
 showDialog('주간 인증 이미지',`<img class="photo" src="${url}" alt="선택한 참여자의 주간 인증 현황">${helpToggle('저장 안내','<p>아이폰에서는 이미지를 길게 눌러 사진에 저장하거나 이미지 공유를 눌러 저장하세요.</p>')}<div class="dialog-actions"><a class="secondary image-download" href="${url}" download="${file.name}">JPG 저장</a>${canShare?'<button class="primary" id="share-image-file">이미지 공유</button>':''}</div>`);
 if(canShare)$('#share-image-file').onclick=async()=>{try{requireShareAdmin();await navigator.share({files:[file]});}catch(error){if(error.name!=='AbortError')formError(error.message);}};
}

function requireShareAdmin(){
 if(!state.viewer?.is_admin)throw new Error('운영자 인증이 필요합니다.');
}
function sharePage(){
 const chosen=state.members.filter(m=>selectedPeople.has(m.id));
 return `<div class="weekbar"><strong>${shortDate(state.week)} — ${shortDate(state.week_end)}</strong></div><div class="member-grid">${chosen.length?chosen.map(m=>memberCard(m)).join(''):'<div class="empty">공유된 인증 내역 없음</div>'}</div>`;
}
