(() => {
  const TOTAL_SECONDS = Math.max(60, Number(window.QUIZ_DURATION || 60) * 60);
  let order = [], choiceOrder = [], index = 0, answers = [], times = [], startedAt = null;
  let questionStarted = 0, totalStarted = 0, totalTimer = null, questionTimer = null, totalLeft = TOTAL_SECONDS;
  let submitting = false;
  let connectionPaused = false;
  let hasDisconnected = false;
  const SAVE_KEY = `tle_css_quiz_state_${Number(window.QUIZ_ID || 0)}`;
  const submissionToken = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('sub-' + Date.now() + '-' + Math.random().toString(36).slice(2));
  const $ = id => document.getElementById(id);
  const shuffle = arr => { const a=[...arr]; for(let j=a.length-1;j>0;j--){const k=Math.floor(Math.random()*(j+1));[a[j],a[k]]=[a[k],a[j]];} return a; };
  const questionMap = new Map(Q.map(q => [Number(q.id), q]));

  function init(){
    const restored = loadSavedState();
    if(!restored){
      order = shuffle(Q.map(q => Number(q.id)));
      choiceOrder = order.map(() => shuffle([0,1,2,3]));
      answers = Array(order.length).fill(null);
      times = Array(order.length).fill(0);
      startedAt = new Date().toISOString();
      totalLeft = TOTAL_SECONDS;
    }
    totalStarted = Date.now() - Math.max(0, TOTAL_SECONDS-totalLeft)*1000;
    renderNavigator();
    showQuestion();
    if(navigator.onLine){
      startTotalTimer();
    } else {
      pauseForConnection('offline');
    }
    saveState();
  }

  function saveState(){
    if(!order.length || submitting) return;
    try{
      localStorage.setItem(SAVE_KEY, JSON.stringify({
        quizId:Number(window.QUIZ_ID), order, choiceOrder, index, answers, times,
        startedAt, totalLeft, savedAt:Date.now()
      }));
    }catch(e){}
  }

  function loadSavedState(){
    try{
      const raw=localStorage.getItem(SAVE_KEY);
      if(!raw) return false;
      const state=JSON.parse(raw);
      if(Number(state.quizId)!==Number(window.QUIZ_ID) || !Array.isArray(state.order) || state.order.length!==Q.length) return false;
      order=state.order.map(Number);
      choiceOrder=state.choiceOrder;
      index=Math.min(Math.max(Number(state.index)||0,0),order.length-1);
      answers=Array.isArray(state.answers)?state.answers:Array(order.length).fill(null);
      times=Array.isArray(state.times)?state.times:Array(order.length).fill(0);
      startedAt=state.startedAt || new Date().toISOString();
      totalLeft=Math.max(0,Math.min(TOTAL_SECONDS,Number(state.totalLeft ?? TOTAL_SECONDS)));
      if(totalLeft<=0) return false;
      hasDisconnected=true;
      showConnectionBanner('restored');
      return true;
    }catch(e){ return false; }
  }

  function clearSavedState(){ try{localStorage.removeItem(SAVE_KEY);}catch(e){} }

  function showConnectionBanner(type){
    const banner=$('connectionBanner');
    if(!banner) return;
    const title=$('connectionTitle'), message=$('connectionMessage'), btn=$('connectionAction');
    banner.className='connection-banner show '+type;
    if(type==='offline'){
      title.textContent='Connection interrupted';
      message.textContent='You were disconnected due to a network interruption. Your progress has been saved. Click “Resume Test” to start your timer and complete your attempt.';
      btn.textContent='Resume Test';
    }else if(type==='restored'){
      title.textContent='Welcome back!';
      message.textContent=`Your connection has been restored. Click “Resume” to continue your quiz from where you left off. (Remaining time: ${Math.ceil(totalLeft/60)} minutes)`;
      btn.textContent='Resume';
    }else{
      title.textContent='Connection restored';
      message.textContent='Connection restored. Your timer will restart as soon as you click “Continue Quiz”.';
      btn.textContent='Continue Quiz';
    }
    btn.onclick=()=>{
      banner.classList.remove('show');
      connectionPaused=false;
      totalStarted=Date.now()-Math.max(0,TOTAL_SECONDS-totalLeft)*1000;
      startTotalTimer();
      questionStarted=Date.now()-Math.max(0,Number(times[index]||0))*1000;
      updateQuestionTime();
      questionTimer=setInterval(updateQuestionTime,250);
      saveState();
    };
  }

  function hideConnectionBanner(){ $('connectionBanner')?.classList.remove('show'); }

  function pauseForConnection(type='offline'){
    if(connectionPaused) return;
    connectionPaused=true;
    clearInterval(totalTimer); totalTimer=null;
    stopQuestionTimer();
    totalLeft=Math.max(0,TOTAL_SECONDS-Math.floor((Date.now()-totalStarted)/1000));
    saveState();
    showConnectionBanner(type);
  }

  function stopQuestionTimer(){ clearInterval(questionTimer); questionTimer=null; }

  function renderNavigator(){
    const nav = $('questionNav');
    if(!nav) return;
    nav.innerHTML = order.map((qid,i) => {
      const state = answers[i] === null ? 'unanswered' : (answers[i] === 'skip' ? 'skipped' : 'answered');
      return `<button type="button" class="qnav ${state} ${i===index?'current':''}" data-index="${i}" title="Question ${i+1}">Q${i+1}</button>`;
    }).join('');
    nav.querySelectorAll('.qnav').forEach(btn => btn.addEventListener('click', () => goTo(Number(btn.dataset.index))));
    updateAnswerStatus();
  }

  function updateAnswerStatus(){
    const unanswered = answers.filter(a => a === null).length;
    const skipped = answers.filter(a => a === 'skip').length;
    const remaining = unanswered + skipped;
    const status = $('answerStatus');
    if(status){
      if(remaining === 0) status.textContent = '✓ All questions answered. You may submit the quiz.';
      else status.textContent = `${remaining} question${remaining===1?'':'s'} still need${remaining===1?'s':''} an answer (${skipped} skipped).`;
      status.className = remaining === 0 ? 'answer-status complete' : 'answer-status pending';
    }
    const next = $('next');
    if(next){
      next.textContent = index === order.length-1 && remaining === 0 ? 'Submit Quiz ✓' : 'Next Question →';
    }
  }

  function showQuestion(){
    stopQuestionTimer();
    const q = questionMap.get(order[index]), map = choiceOrder[index];
    $('count').textContent = `Question ${index+1} of ${order.length}`;
    $('question').innerHTML = escapeHtml(q.q) + (q.image ? `<div class="question-image-wrap"><img class="question-image" src="${escapeHtml(q.image)}" alt="Question illustration" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="question-image-error" hidden>🖼️ Question image is unavailable. Please ask the teacher to re-upload the picture.</div></div>` : '');
    $('choices').innerHTML = map.map((oi,pos) => `<button class="choice" type="button" data-n="${oi}"><b>${String.fromCharCode(65+pos)}.</b> ${escapeHtml(q.choices[oi])}</button>`).join('');
    document.querySelectorAll('.choice').forEach(btn => btn.addEventListener('click', () => selectAnswer(Number(btn.dataset.n))));

    const currentAnswer = answers[index];
    if(currentAnswer !== null && currentAnswer !== 'skip'){
      document.querySelectorAll('.choice').forEach(btn => btn.classList.toggle('selected', Number(btn.dataset.n) === currentAnswer));
    }

    $('next').disabled = currentAnswer === null || currentAnswer === 'skip';
    $('skip').textContent = currentAnswer === 'skip' ? 'Skipped — Choose an Answer' : 'Skip for Now';
    $('skip').disabled = currentAnswer !== null && currentAnswer !== 'skip';
    questionStarted = Date.now();
    updateQuestionTime();
    questionTimer = setInterval(updateQuestionTime,250);
    renderNavigator();
  }

  function goTo(target){
    if(target < 0 || target >= order.length || target === index) return;
    index = target;
    showQuestion();
  }

  function updateQuestionTime(){
    const sec = Math.max(0,(Date.now()-questionStarted)/1000);
    if($('questionTime')) $('questionTime').textContent = `Question time: ${sec.toFixed(1)}s`;
  }

  function startTotalTimer(){
    if(connectionPaused || totalTimer) return;
    updateTotalTimer();
    totalTimer = setInterval(() => {
      totalLeft = Math.max(0,TOTAL_SECONDS-Math.floor((Date.now()-totalStarted)/1000));
      updateTotalTimer();
      saveState();
      if(totalLeft <= 0){
        clearInterval(totalTimer); totalTimer=null;
        stopQuestionTimer();
        clearSavedState();
        for(let i=0;i<answers.length;i++) if(answers[i] === null || answers[i] === 'skip'){ answers[i]='timed'; times[i]=0; }
        finish(true);
      }
    },1000);
  }

  function updateTotalTimer(){
    const m=Math.floor(totalLeft/60), s=totalLeft%60;
    $('timer').textContent=`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    $('timerLabel').textContent=`Total time remaining · ${Math.ceil(TOTAL_SECONDS/60)} minutes`;
    $('bar').style.width=`${Math.max(0,totalLeft)/TOTAL_SECONDS*100}%`;
    $('bar').className=totalLeft<=60?'danger':totalLeft<=300?'warning':'safe';
  }

  function escapeHtml(v){return String(v).replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));}

  function selectAnswer(originalIndex){
    times[index]=Math.max(0,(Date.now()-questionStarted)/1000);
    stopQuestionTimer();
    answers[index]=originalIndex;
    document.querySelectorAll('.choice').forEach(btn => btn.classList.toggle('selected',Number(btn.dataset.n)===originalIndex));
    $('next').disabled=false;
    $('skip').disabled=true;
    $('skip').textContent='Answered ✓';
    updateQuestionTime();
    renderNavigator();
    updateAnswerStatus();
    saveState();
  }

  function markSkipped(){
    times[index]=Math.max(0,(Date.now()-questionStarted)/1000);
    stopQuestionTimer();
    answers[index]='skip';
    renderNavigator();
    updateAnswerStatus();
    saveState();
    if(index < order.length-1){ index++; showQuestion(); saveState(); }
    else {
      const firstSkipped = answers.findIndex(a => a === 'skip' || a === null);
      if(firstSkipped >= 0){
        index = firstSkipped;
        showQuestion();
        showNeedAnswersMessage();
      }
    }
  }

  function showNeedAnswersMessage(){
    const skipped = answers.filter(a => a === 'skip').length;
    const unanswered = answers.filter(a => a === null).length;
    const total = skipped + unanswered;
    if(total > 0) alert(`You still have ${total} question${total===1?'':'s'} to answer.\n\nSkipped questions are not submitted. Please return to the highlighted questions and choose an answer before submitting.`);
  }

  function advance(){
    if(answers[index] === null || answers[index] === 'skip') return;
    stopQuestionTimer();
    if(index < order.length-1){ index++; showQuestion(); }
    else {
      const remaining = answers.findIndex(a => a === null || a === 'skip');
      if(remaining >= 0){ index=remaining; showQuestion(); showNeedAnswersMessage(); return; }
      finish(false);
    }
  }

  async function finish(autoTimed=false){
    stopQuestionTimer();
    if(submitting) return;
    if(!autoTimed){
      const remaining = answers.filter(a => a === null || a === 'skip').length;
      if(remaining > 0){ showNeedAnswersMessage(); return; }
    }
    submitting = true;
    $('next').disabled = true;
    $('skip').disabled = true;
    if($('next')) $('next').textContent = 'Saving Result…';
    clearInterval(totalTimer);
    if(autoTimed){
      for(let i=0;i<answers.length;i++) if(answers[i] === null || answers[i] === 'skip'){ answers[i]='timed'; times[i]=0; }
    }
    const payload={quiz_id:Number(window.QUIZ_ID),answers,question_order:order,choice_order:choiceOrder,times,started_at:startedAt,submission_token:submissionToken};
    try{
      const response=await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();
      if(!response.ok||!result.ok) throw new Error(result.error||'Unable to save your result.');
      clearSavedState();
      window.location.href='/history';
    }catch(error){
      submitting = false;
      saveState();
      connectionPaused=true;
      clearInterval(totalTimer); totalTimer=null;
      stopQuestionTimer();
      totalLeft=Math.max(0,TOTAL_SECONDS-Math.floor((Date.now()-totalStarted)/1000));
      showConnectionBanner(navigator.onLine ? 'restored' : 'offline');
      $('next').disabled = false;
      showQuestion();
      if($('next')) $('next').textContent='Next Question →';
    }
  }

  $('next').addEventListener('click',advance);
  $('skip').addEventListener('click',markSkipped);
  $('themeBtn')?.addEventListener('click',()=>document.body.classList.toggle('dark'));
  document.addEventListener('keydown',e=>{
    if(['1','2','3','4'].includes(e.key)) document.querySelectorAll('.choice')[Number(e.key)-1]?.click();
    if('abcd'.includes(e.key.toLowerCase())) document.querySelectorAll('.choice')['abcd'.indexOf(e.key.toLowerCase())]?.click();
    if(e.code==='Space'){e.preventDefault();if(!$('skip').disabled)$('skip').click();}
    if(e.key==='Enter'&&!$('next').disabled)$('next').click();
  });
  window.addEventListener('offline',()=>pauseForConnection('offline'));
  window.addEventListener('online',()=>{
    if(connectionPaused){
      showConnectionBanner(hasDisconnected ? 'restored' : 'continue');
    } else {
      showConnectionBanner('continue');
    }
    hasDisconnected=true;
  });
  document.addEventListener('visibilitychange',()=>{ if(document.hidden) saveState(); });
  window.addEventListener('beforeunload',saveState);
  init();
})();
