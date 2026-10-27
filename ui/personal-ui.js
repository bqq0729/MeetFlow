/* UI adapter for the documented backend endpoints. No model processing happens here. */
window.personalCodexOk=false;
window.personalClaudePending=false;
window.personalCodexPending=false;
window.personalSelectedEngine='claude';
window.personalEngineInitialized=false;
window.personalDocumentTypes=['minutes'];
window.personalSwitching=false;
const personalDisconnecting=new Set();
window.personalEnforceSingleEngine=function(engine,state){
  if(!window.personalEngineInitialized || !state.ok || engine===window.personalSelectedEngine || personalDisconnecting.has(engine))return;
  personalDisconnecting.add(engine);
  const request=engine==='codex'?api.logout_codex():api.logout();
  request.then(result=>{if(result.error)toast('이전 연결 해제 필요',result.error,{error:true});}).finally(()=>personalDisconnecting.delete(engine));
};
window.personalEngineName=()=>document.getElementById('m-engine')?.value==='codex'?'Codex':'Claude Code';
window.personalModel=()=>document.getElementById('m-engine').value==='codex'?document.getElementById('m-codex-model').value.trim():document.getElementById('m-model').value;
window.personalUpdateEngineStatus=function(){
  const codex=document.getElementById('m-engine')?.value==='codex';
  const ok=codex?window.personalCodexOk:claudeOk;
  const state=document.getElementById('selected-engine-state');
  if(state){state.textContent=ok?'연결됨':'연결 필요';state.classList.toggle('connected',ok);}
  document.getElementById('ai-side-state').textContent=`${personalEngineName()} · ${ok?'연결됨':'연결 필요'}`;
};
window.personalLoginEngine=()=>document.getElementById('m-engine').value==='codex'?api.login_codex():document.getElementById('claude-login').click();
window.personalShowCodex=function(d){
  window.personalCodexPending=!!d.pending;
  const detail=document.getElementById('codex-detail'), button=document.getElementById('codex-login'),device=document.getElementById('codex-device');
  document.getElementById('codex-dot').className='dot '+(d.pending?'wait':d.ok?'ok':'bad');
  button.disabled=!!d.pending;
  if(d.pending){
    detail.textContent=d.login_url?'로그인 페이지에서 아래 코드로 인증해 주세요.':'로그인을 준비하고 있어요…';
    button.textContent='연결 중…';
    device.hidden=!(d.login_url&&d.login_code);
    if(!device.hidden){
      document.getElementById('codex-device-code').textContent=d.login_code;
      const url=new URL(d.login_url);
      if(url.protocol==='https:'&&url.hostname==='auth.openai.com') document.getElementById('codex-device-link').href=url.href;
    }
  }else{
    device.hidden=true;
    window.personalCodexOk=!!d.ok;
    detail.textContent=d.ok?'계정이 연결됐어요. 문서 만들기에서 Codex를 선택하세요.':d.missing?'Codex CLI를 설치한 뒤 연결해 주세요.':'연결을 누르면 계정 로그인 안내가 표시됩니다.';
    button.textContent=d.ok?'로그아웃':'Codex 연결';
  }
  personalUpdateEngineStatus();
  personalEnforceSingleEngine('codex',d);
};
window.personalEnsureEngine=async function(){
  if(window.personalSwitching){toast('AI 도구 변경 중','연결 정리가 끝난 뒤 다시 눌러 주세요.');return false;}
  const codex=document.getElementById('m-engine').value==='codex';
  if(codex){const state=await api.check_codex();if(state.error){await alertBox('서버 확인 필요',state.error);return false;}personalShowCodex(state);}
  if(codex?window.personalCodexOk:claudeOk)return true;
  const yes=await modal(`${personalEngineName()} 연결 필요`,'문서를 정리하려면 선택한 AI 도구를 연결해 주세요. 음성 변환은 계정 없이 사용할 수 있어요.',[{label:'나중에',value:false},{label:'연결 관리로',value:true,primary:true}]);
  if(yes)go('connections');
  return false;
};
window.personalSyncEngine=function(){
  const codex=document.getElementById('m-engine').value==='codex';
  document.getElementById('claude-model-field').hidden=codex;
  document.getElementById('codex-model-field').hidden=!codex;
  document.getElementById('claude-account-card').hidden=codex;
  document.getElementById('codex-account-card').hidden=!codex;
  document.getElementById('connections-engine').value=codex?'codex':'claude';
  document.getElementById('codex-sharing-note').hidden=!codex;
  personalUpdateEngineStatus();
};
window.personalSetEngine=async function(next){
  const previous=window.personalSelectedEngine;
  if(next===previous){personalSyncEngine();return;}
  if(window.personalClaudePending || window.personalCodexPending){
    document.getElementById('m-engine').value=previous;
    document.getElementById('connections-engine').value=previous;
    toast('로그인이 진행 중이에요','인증을 마친 뒤 AI 도구를 변경해 주세요.');return;
  }
  if(document.getElementById('t-run').disabled){
    document.getElementById('m-engine').value=previous;
    document.getElementById('connections-engine').value=previous;
    toast('작업이 진행 중이에요','현재 작업이 끝난 뒤 AI 도구를 변경해 주세요.');return;
  }
  window.personalSwitching=true;
  try{
    const oldOk=previous==='codex'?window.personalCodexOk:claudeOk;
    if(oldOk){
      const result=await(previous==='codex'?api.logout_codex():api.logout());
      if(result.error)throw new Error(result.error);
      if(previous==='codex')window.personalCodexOk=false;else claudeOk=false;
    }
    window.personalSelectedEngine=next;
    document.getElementById('m-engine').value=next;
    personalSyncEngine();
    syncMoreSum();
    await api.save_settings({minutes_engine:next});
    if(next==='codex'){const result=await api.check_codex();if(!result.error)personalShowCodex(result);}
    else await api.check_claude();
  }catch(error){
    window.personalSelectedEngine=previous;
    document.getElementById('m-engine').value=previous;
    personalSyncEngine();
    await alertBox('AI 도구 변경',error.message);
  }finally{window.personalSwitching=false;}
};
window.personalInitEngines=function(init){
  const settings=init.settings||{};
  window.personalDocumentTypes=Array.isArray(init.document_types)?init.document_types:['minutes'];
  const engines=Array.isArray(init.engines)?init.engines:['claude','codex'];
  const select=document.getElementById('m-engine');
  select.innerHTML=engines.filter(e=>['claude','codex'].includes(e)).map(e=>`<option value="${e}">${e==='codex'?'Codex · OpenAI':'Claude Code'}</option>`).join('');
  select.value=engines.includes(settings.minutes_engine)?settings.minutes_engine:'claude';
  window.personalSelectedEngine=select.value;
  window.personalEngineInitialized=true;
  personalEnforceSingleEngine('claude',{ok:claudeOk});
  if(select.value==='codex')document.getElementById('m-codex-model').value=settings.minutes_model||'';
  personalSyncEngine();
  api.check_codex().then(state=>{if(!state.error)personalShowCodex(state);else document.getElementById('codex-detail').textContent='새 연결 기능을 사용하려면 personal 서버를 다시 시작해 주세요.';}).catch(()=>{});
};
(() => {
  const labels = {guide:'나의 작업실', make:'음성 변환', publish:'업무 연동', compare:'비교분석', stats:'활동과 사용량', connections:'연결 관리'};
  let mode = 'transcript';
  const selections = {transcript:'', minutes:''};
  const page = document.getElementById('page-make');
  const input = document.getElementById('t-files');
  const minutesOn = document.getElementById('m-on');
  const runButton = document.getElementById('t-run');
  const formats = document.querySelector('.chips').closest('.field');
  const filePicker = document.querySelector('[data-target="t-files"]');
  const transcriptOptions = ['t-lang','t-model','t-speakers','t-diarize'].map(id=>document.getElementById(id).closest('.opt-cell'));
  minutesOn.closest('.opt-cell').classList.add('legacy-mode-switch');
  function updateMode() {
    const minutes = mode === 'minutes';
    page.dataset.mode = mode;
    personalUpdateGuide(minutes);
    minutesOn.checked = minutes;
    formats.hidden = !minutes;
    document.getElementById('t-model-note').hidden = minutes;
    transcriptOptions.forEach(el=>el.hidden = minutes);
    document.querySelectorAll('#t-more .template-field,#t-more .document-fields').forEach(el=>el.hidden = !minutes);
    filePicker.dataset.pick = minutes ? 'txt' : 'media';
    document.querySelector('label[for="t-files"]').textContent = minutes ? '대본 파일' : '음성 · 영상 파일';
    input.placeholder = minutes ? '정리할 대본(.txt)을 선택하세요' : '저장된 음성·영상 파일을 업로드하세요';
    page.querySelector('h1').textContent = minutes ? '대본을 문서로 정리해요.' : '음성 파일을 텍스트로 변환해요.';
    page.querySelector('.page-head p').textContent = minutes ? '완성된 대본을 AI로 정리하고 원하는 문서로 저장하세요.' : '외부에서 녹음한 파일을 올려 텍스트로 바꾸고, 화자별 발언을 확인하세요.';
    page.querySelector('.eyebrow').textContent = minutes ? 'TURN WORDS INTO CLARITY' : 'UPLOAD AUDIO, GET TEXT';
    page.querySelector('.editor-index').textContent = minutes ? '02 / MINUTES' : '01 / TRANSCRIPT';
    page.querySelector('.upload-banner h2').textContent = minutes ? '대본과 문서 종류를 선택하세요.' : '변환할 음성·영상 파일을 올려주세요.';
    page.querySelector('.upload-banner p').textContent = minutes ? '음성 변환에서 저장한 .txt 파일도 사용할 수 있어요.' : '녹음은 휴대폰·다른 프로그램에서 진행하세요. 여기서는 파일만 변환합니다.';
    page.querySelector('.upload-banner>span:last-child').textContent = minutes ? 'TRANSCRIPT → DOCUMENT' : 'AUDIO → TRANSCRIPT';
    document.querySelectorAll('.minutes-document-field').forEach(el=>el.hidden=!minutes);
    page.querySelector('.settings-caption small').textContent = minutes ? '문서 정보 · 양식 · AI 모델' : '언어 · 인식 품질 · 화자 구분';
    document.querySelectorAll('[data-personal-mode]').forEach(el=> {
      const selected=el.dataset.personalMode===mode;
      if(el.classList.contains('mode-tab')) {el.classList.toggle('active',selected);el.setAttribute('aria-pressed',String(selected));}
      if(el.closest('.nav')) {el.classList.toggle('active',page.classList.contains('active')&&selected);if(page.classList.contains('active')&&selected)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');}
    });
    if(!runButton.disabled) syncRunLabel();
    syncMoreSum();
    syncPage();
  }
  window.personalMode = function(next, useCurrent=false) {
    if(runButton.disabled && next!==mode) {toast('작업이 진행 중이에요','현재 작업이 끝난 뒤 다른 화면으로 이동할 수 있어요.');return;}
    selections[mode]=input.value;
    const changed=next!==mode;
    mode=next;
    if(changed) {
      input.value=useCurrent && curTxt ? curTxt : selections[mode] || (mode==='minutes' && curTxt ? curTxt : '');
      renderPills();
    }
    go('make');
    updateMode();
  };
  document.querySelectorAll('[data-personal-mode]').forEach(el=>el.onclick=()=>window.personalMode(el.dataset.personalMode));
  const originalRun=runButton.onclick;
  runButton.onclick=async function() {
    minutesOn.checked=mode==='minutes';
    const files=pickedFiles();
    if(mode==='transcript' && files.some(isTxt)) return alertBox('저장된 음성·영상 파일을 선택해 주세요','대본(.txt)을 정리하려면 문서 만들기로 이동하세요.');
    if(mode==='minutes' && files.some(f=>!isTxt(f))) return alertBox('대본 파일을 선택해 주세요','먼저 음성 변환에서 저장된 음성 파일을 텍스트로 바꿔 주세요.');
    const kind=document.getElementById('document-type').value;
    if(mode==='minutes' && !window.personalDocumentTypes.includes(kind)) return alertBox('문서 종류 준비 중','이 종류의 문서 정리는 아직 준비 중이에요. 현재는 회의록으로 만들 수 있어요.');
    return originalRun.call(this);
  };
  const date = document.getElementById('home-date');
  if (date) date.textContent = new Intl.DateTimeFormat('ko-KR', {month:'long', day:'numeric', weekday:'long'}).format(new Date());
  function syncPage() {
    const active = document.querySelector('.page.active');
    if (!active) return;
    const label = active.id==='page-make' && mode==='minutes' ? '문서 만들기' : labels[active.id.replace('page-', '')] || '나의 작업실';
    document.getElementById('workspace-current').textContent = label;
    document.title = `${label} · Whisper Personal`;
    if(active.id==='page-make') document.querySelectorAll('.nav [data-personal-mode]').forEach(el=>el.classList.toggle('active',el.dataset.personalMode===mode));
    document.getElementById('nav-compare')?.classList.toggle('active',active.id==='page-compare');
    updateContext();
  }
  const observer = new MutationObserver(syncPage);
  document.querySelectorAll('.page').forEach(page => observer.observe(page, {attributes:true, attributeFilter:['class']}));
  // Existing initialization populates settings asynchronously. Keep the mode's action label explicit.
  const actionObserver=new MutationObserver(()=> {
    if(runButton.disabled) return;
    const wanted=mode==='minutes'?'문서 만들기':'음성 변환';
    const label=document.getElementById('t-run-label');
    if(label && label.textContent.trim()!==wanted) label.textContent=wanted;
  });
  actionObserver.observe(runButton,{childList:true,subtree:true,characterData:true});
  if(!runButton.disabled) document.getElementById('t-run-label').textContent='음성 변환';
  const help={guide:'외부에서 녹음한 파일을 음성 변환에 업로드한 뒤, 문서 만들기에서 필요한 형태로 정리하세요.',transcript:'이 페이지에서는 녹음하지 않습니다. 휴대폰·다른 프로그램에서 녹음한 음성·영상 파일을 업로드하세요. 언어·품질·화자 구분은 자세한 설정에서 바꿀 수 있어요. 완료 후 대본(.txt)과 자막(.srt)을 저장합니다.',minutes:'대본(.txt)과 문서 종류를 선택하고 PDF·한글·워드 등의 저장 형식을 고르세요. 선택한 AI 도구의 계정 연결이 필요합니다.',publish:'저장한 문서를 선택하고 연결된 도구로 공유하세요. 등록할 내용과 담당자·기한을 먼저 확인해 주세요.',compare:'이전 개인 테스트의 관찰을 조건별로 살펴보세요. 측정 조건이 다를 수 있으며, 정답 원문 기준의 정확도 순위는 아닙니다.',stats:'실제로 완료한 작업의 시간과 사용량을 살펴볼 수 있어요.',connections:'문서를 정리할 AI 도구 하나를 선택하고 연결하세요. 도구를 변경하면 이전 도구의 연결은 해제합니다.'};
  function updateContext() {
    const active=document.querySelector('.page.active');
    if(!active) return;
    const id=active.id.replace('page-','');
    const context=id==='make'?mode:id;
    const count=input.value.split(';').filter(s=>s.trim()).length;
    const status=document.getElementById('workspace-context');
    if(status) status.textContent=id==='make'?(count?`${mode==='minutes'?'대본':'음성·영상 파일'} ${count}개 선택`:'파일 선택 전') : {guide:'대본 → 문서 → 공유',publish:'내용 확인 후 공유',compare:'개인 테스트 기록',stats:'작업 시간 · 사용량',connections:'계정 · 외부 도구'}[id]||'';
    const panel=document.getElementById('workspace-help-text');
    if(panel) panel.textContent=help[context]||'';
    if(id==='make' && !runButton.disabled) {
      const statusText=document.querySelector('#t-status .text');
      if(statusText && !document.querySelector('#t-next.show') && !document.querySelector('#t-bar.running')) statusText.textContent=mode==='minutes'?'대본과 문서 종류를 선택하고 시작하세요.':'업로드한 파일을 텍스트(.txt)와 자막(.srt)으로 저장합니다.';
    }
  }
  document.getElementById('nav-compare').onclick=()=>go('compare');
  document.getElementById('workspace-help-toggle').onclick=()=>{
    const panel=document.getElementById('workspace-help');
    panel.hidden=!panel.hidden;
    document.getElementById('workspace-help-toggle').setAttribute('aria-expanded',String(!panel.hidden));
  };
  ['input','change'].forEach(event=>input.addEventListener(event,updateContext));
  const kinds={minutes:'회의 목적·주요 논의·결정·담당자와 할 일을 정리합니다.',interview:'질문과 답변, 핵심 발언과 인사이트를 정리합니다.',lecture:'핵심 개념·설명·예시·복습할 내용을 정리합니다.',report:'배경·진행 내용·주요 결과·다음 계획을 정리합니다.',summary:'긴 대본에서 핵심 주제와 요점만 간결하게 추립니다.',custom:'아래 추가 요청에 원하는 구성과 말투를 적어주세요.'};
  const kind=document.getElementById('document-type');
  kind.onchange=()=>{document.getElementById('document-type-hint').textContent=kinds[kind.value]+(window.personalDocumentTypes.includes(kind.value)?'':' 이 문서 종류는 준비 중입니다.');syncMoreSum();};
  kind.onchange();
  document.getElementById('m-engine').onchange=()=>personalSetEngine(document.getElementById('m-engine').value);
  document.getElementById('connections-engine').onchange=()=>personalSetEngine(document.getElementById('connections-engine').value);
  document.querySelectorAll('.chip input').forEach(input=>input.addEventListener('change',syncMoreSum));
  document.getElementById('codex-login').onclick=async()=>{
    if(window.personalCodexOk){
      const yes=await modal('Codex 로그아웃','이 개인 작업실의 Codex 연결을 해제할까요?',[{label:'취소',value:false},{label:'로그아웃',value:true,primary:true}]);
      if(yes){const state=await api.logout_codex();if(state.error)alertBox('연결 해제',state.error);else personalShowCodex(state);}
    }else{personalShowCodex({pending:true});const state=await api.login_codex();if(state?.error){personalShowCodex({ok:false});alertBox('Codex 연결',state.error);}}
  };
  document.getElementById('codex-refresh').onclick=async()=>{const state=await api.check_codex();if(state.error)alertBox('연결 확인',state.error);else personalShowCodex(state);};
  document.querySelectorAll('[data-compare]').forEach(button=>button.onclick=()=>{
    document.querySelectorAll('[data-compare]').forEach(b=>{const chosen=b===button;b.classList.toggle('active',chosen);b.setAttribute('aria-pressed',String(chosen));});
    document.querySelectorAll('.comparison-panel').forEach(panel=>panel.hidden=panel.id!==`compare-panel-${button.dataset.compare}`);
  });
  syncPage();
  updateMode();
  updateContext();
})();

function personalUpdateGuide(minutes){
 const set=(id,value)=>{const e=document.getElementById(id);if(e)e.textContent=value;};
 set('work-guide-title',minutes?'문서 정리 안내':'파일 변환 안내');
 set('work-guide-intro',minutes?'변환된 대본을 선택한 AI 도구로 정리합니다.':'녹음은 휴대폰이나 다른 프로그램에서 따로 진행하세요.');
 set('work-step-1',minutes?'대본 파일 선택':'저장된 파일 업로드');
 set('work-step-note-1',minutes?'정리할 .txt 파일을 준비합니다.':'음성·영상 파일을 선택합니다.');
 set('work-step-2',minutes?'문서 종류와 AI 선택':'변환 옵션 확인');
 set('work-step-note-2',minutes?'사용할 AI 도구 하나를 연결합니다.':'언어·품질·화자 구분을 설정합니다.');
 set('work-step-3',minutes?'문서 생성과 검토':'텍스트로 변환');
 set('work-step-note-3',minutes?'제목·이름·결정 내용을 확인합니다.':'내용과 화자를 확인하고 저장합니다.');
 set('work-guide-output',minutes?'PDF · 한글(HWPX) · Word · Markdown':'대본 .txt · 자막 .srt');
 set('work-guide-note',minutes?'문서 정리 시 대본이 선택한 AI 서비스로 전달됩니다.':'음성 변환은 AI 계정 연결 없이 사용할 수 있습니다.');
}
(() => {
 const sizes=window.matchMedia('(min-width:1200px)');
 const sync=()=>{const guide=document.getElementById('work-guide');if(guide)guide.open=sizes.matches;};
 sizes.addEventListener('change',sync);sync();
 document.querySelectorAll('[data-cost-view]').forEach(button=>button.onclick=()=>{
  document.querySelectorAll('[data-cost-view]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});
  document.querySelectorAll('[data-cost-panel]').forEach(panel=>panel.hidden=panel.dataset.costPanel!==button.dataset.costView);
 });
})();
