// Actual app DOM/network proof. Credentials and wire payloads stay in memory.
import {createHash} from 'node:crypto';
import {readFile,writeFile} from 'node:fs/promises';
let input='';
for await(const chunk of process.stdin) input+=chunk;
const config=JSON.parse(input);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const hash=value=>createHash('sha256').update(value).digest('hex');
const check=(value,label)=>{if(!value)throw new Error(label); console.log('PASS '+label);};
async function waitFor(ready,label){const end=Date.now()+15000;while(Date.now()<end){if(ready())return;await delay(200);}throw new Error('Timed out: '+label);}
async function downloaded(file){
  const end=Date.now()+15000;
  while(Date.now()<end){try{return await readFile(config.downloads+'/'+file.name);}catch(error){if(error.code!=='ENOENT')throw new Error('Actual browser download could not be read');}await delay(200);}
  throw new Error('Actual browser download did not finish');
}
const credentials=new Set();
function captureCredentials(tab){
  const requests=new Set(),tokens=new Set();
  tab.on('Network.requestWillBeSent',({requestId,request})=>{
    const auth=request.headers.Authorization??request.headers.authorization;
    if(auth){credentials.add(auth);credentials.add(auth.replace(/^Bearer /i,''));}
    if(!new URL(request.url).pathname.endsWith('/protocol/openid-connect/token'))return;
    requests.add(requestId);
    const form=new URLSearchParams(request.postData??'');
    for(const key of ['access_token','refresh_token','id_token'])if(form.get(key))credentials.add(form.get(key));
  });
  tab.on('Network.loadingFinished',async({requestId})=>{
    if(!requests.delete(requestId))return;
    const response=await tab.call('Network.getResponseBody',{requestId});
    const body=JSON.parse(response.base64Encoded?Buffer.from(response.body,'base64').toString():response.body);
    for(const key of ['access_token','refresh_token','id_token'])if(typeof body[key]==='string'&&body[key]){credentials.add(body[key]);tokens.add(key);}
  });
  return tokens;
}

class CDP {
  constructor(socket){this.socket=socket;this.id=0;this.pending=new Map();this.listeners=new Map();
    socket.onmessage=({data})=>{const item=JSON.parse(data);if(item.id){const request=this.pending.get(item.id);this.pending.delete(item.id);clearTimeout(request.timer);item.error?request.reject(new Error(item.error.message)):request.resolve(item.result);}
      else for(const listener of this.listeners.get(item.method)??[])Promise.resolve(listener(item.params)).catch(error=>{this.failure=error;});};
  }
  static async page(targetId){const pages=await(await fetch('http://127.0.0.1:'+config.debugPort+'/json/list')).json();
    const page=targetId?pages.find(page=>page.id===targetId):pages.find(page=>page.type==='page');
    if(!page)throw new Error('Actual browser page is missing');const socket=new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});return new CDP(socket);
  }
  call(method,params={}){return new Promise((resolve,reject)=>{const id=++this.id;const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);this.pending.set(id,{resolve,reject,timer});this.socket.send(JSON.stringify({id,method,params}));});}
  on(method,listener){this.listeners.set(method,[...(this.listeners.get(method)??[]),listener]);}
  async evaluate(expression){const result=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw new Error('Actual browser expression failed');return result.result.value;}
  async wait(expression,label,timeout=45000){const end=Date.now()+timeout;while(Date.now()<end){if(this.failure)throw this.failure;try{if(await this.evaluate(expression))return;}catch(error){if(error.message!=='Actual browser expression failed')throw error;}await delay(200);}throw new Error('Timed out: '+label);}
  async click(text,selector='button'){const clicked=await this.evaluate(`(()=>{const button=[...document.querySelectorAll(${JSON.stringify(selector)})].find(button=>(button.textContent.trim()===${JSON.stringify(text)} || button.getAttribute('aria-label')===${JSON.stringify(text)} || (${JSON.stringify(text)}==='Файлы' && button.textContent.trim().startsWith('Файлы ')))&&!button.disabled);if(!button)return false;button.click();return true;})()`);if(!clicked)throw new Error('Actual DOM action unavailable: '+text);}
  async field(selector,value){await this.evaluate(`(()=>{const field=document.querySelector(${JSON.stringify(selector)});const prototype=field instanceof HTMLSelectElement?HTMLSelectElement.prototype:field instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(prototype,'value').set.call(field,${JSON.stringify(String(value))});field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
  async files(paths){const document=await this.call('DOM.getDocument');const node=await this.call('DOM.querySelector',{nodeId:document.root.nodeId,selector:'input[type=file]'});await this.call('DOM.setFileInputFiles',{nodeId:node.nodeId,files:paths});}
  close(){this.socket.close();}
}

const page=await CDP.page();
let second;
const posts=[],requests=new Map(),history=[],tasks=[],receipts=[],outputRequests=[],cancelledDownloads=new Set(),untrustedRequests=[];
page.on('Network.requestWillBeSent',({request})=>{if(new URL(request.url).hostname==='ui-content.invalid')untrustedRequests.push(request.url);});
const apiResponses=[],dialogs=[];
let peerPostAttempts=0,peerDeleteAttempts=0;
let rejectNextAccessDelete=false,rejectNextPeerDelete=false;
const peerSecret='Browser-private-peer-secret-739',schedulePrompt='Browser manual cron in the original chat';
function observeOwnerAPI(tab){
  const pending=new Map();
  tab.on('Network.requestWillBeSent',({requestId,request})=>{
    const url=new URL(request.url);
    if(url.origin!==config.origin||!(/^\/api\/(tool-policies|schedules|remote-agents|external-access|chats\/[^/]+\/(files|title))(\/|$)/.test(url.pathname)||(request.method==='DELETE'&&/^\/api\/chats\/[^/]+$/.test(url.pathname))))return;
    if(url.pathname==='/api/remote-agents'&&request.method==='POST')peerPostAttempts++;
    if(request.method==='DELETE'&&/^\/api\/remote-agents\/[^/]+\/connection$/.test(url.pathname))peerDeleteAttempts++;
    pending.set(requestId,{path:url.pathname,query:url.search,method:request.method,request:request.postData?JSON.parse(request.postData):null,authorized:!!(request.headers.Authorization??request.headers.authorization)});
  });
  tab.on('Network.responseReceived',({requestId,response})=>{const request=pending.get(requestId);if(request)request.status=response.status;});
  tab.on('Network.loadingFinished',async({requestId})=>{
    const request=pending.get(requestId);if(!request)return;pending.delete(requestId);
    // Content downloads are binary and are measured separately below.
    if(request.path.endsWith('/content'))return;
    const response=await tab.call('Network.getResponseBody',{requestId});
    const body=JSON.parse(response.base64Encoded?Buffer.from(response.body,'base64').toString():response.body);
    apiResponses.push({...request,body});
  });
  tab.on('Page.javascriptDialogOpening',async({type,message})=>{
    if(type!=='confirm'||!['Удалить расписание?', 'Есть неотправленное сообщение', 'Результат удаления ещё не подтверждён.', 'Удалить учётку «','Удалить подключение «'].some(prefix=>message.startsWith(prefix)))throw new Error('Unexpected actual owner confirmation');
    const rejectAccess=rejectNextAccessDelete&&message.startsWith('Удалить учётку «');
    const rejectPeer=rejectNextPeerDelete&&message.startsWith('Удалить подключение «');
    const reject=rejectAccess||rejectPeer;
    if(rejectAccess)rejectNextAccessDelete=false;
    if(rejectPeer)rejectNextPeerDelete=false;
    dialogs.push(message);await tab.call('Page.handleJavaScriptDialog',{accept:!reject});
  });
}
async function ownerAction(tab,path,method,action){
  const before=apiResponses.length;await action();
  await waitFor(()=>apiResponses.slice(before).some(response=>response.path===path&&response.method===method),'actual owner '+method+' response');
  const response=apiResponses.slice(before).find(response=>response.path===path&&response.method===method);
  if(!response.authorized||response.status<200||response.status>=300)throw new Error('Actual owner action was not accepted: '+method+' '+path+' status='+response.status);
  return response;
}
let bearer,rootTask,lostReceipt,loseNext=false,dropped=0,pkce=false,pauseDownload=false,pausedDownload;
let breakStreams=false,breakTaskReads=false,failedStreams=0;
try {
  await page.call('Page.enable');await page.call('Runtime.enable');
  observeOwnerAPI(page);
  const firstTokens=captureCredentials(page);await page.call('Network.enable');
  page.on('Network.requestWillBeSent',({requestId,request})=>{
    const url=new URL(request.url);
    if(url.pathname.endsWith('/protocol/openid-connect/auth'))pkce ||= url.searchParams.get('code_challenge_method')==='S256'&&url.searchParams.get('response_type')==='code';
    if(url.origin!==config.origin)return;
    const auth=request.headers.Authorization??request.headers.authorization;if(auth)bearer=auth;
    if(url.pathname.startsWith('/api/chats/')&&url.pathname.includes('/tasks/')&&url.pathname.includes('/files/'))outputRequests.push({requestId,path:url.pathname,query:url.search,authorized:!!auth});
    if(url.pathname==='/a2a/owner/message:send'&&request.method==='POST'){
      const body=JSON.parse(request.postData);posts.push({digest:hash(request.postData),messageId:body.message.messageId,taskId:body.message.taskId,parts:body.message.parts});requests.set(requestId,{kind:'post'});
    } else if(url.pathname.endsWith('/history'))requests.set(requestId,{kind:'history'});
    else if(url.pathname.startsWith('/a2a/owner/tasks/')&&!url.pathname.includes(':'))requests.set(requestId,{kind:'task'});
  });
  page.on('Network.loadingFailed',({requestId,canceled,errorText})=>{if(canceled||errorText==='net::ERR_ABORTED')cancelledDownloads.add(requestId);});
  page.on('Network.loadingFinished',async({requestId})=>{const request=requests.get(requestId);if(!request)return;requests.delete(requestId);
    let response;
    try{response=await page.call('Network.getResponseBody',{requestId});}
    catch(error){if(request.kind!=='post'&&error.message==='No resource with given identifier found')return;throw error;}
    const body=JSON.parse(response.base64Encoded?Buffer.from(response.body,'base64').toString():response.body);
    if(request.kind==='post'&&body.task){receipts.push(body.task.metadata);rootTask??=body.task;}
    if(request.kind==='history'&&body.items)history.push(body.items);
    if(request.kind==='task'&&body.id)tasks.push(body);
  });
  page.on('Fetch.requestPaused',async event=>{
    const path=new URL(event.request.url).pathname;
    if((breakStreams&&path.endsWith(':subscribe'))||(breakTaskReads&&/^\/a2a\/owner\/tasks\/[^:]+$/.test(path))){
      if(path.endsWith(':subscribe'))failedStreams++;
      await page.call('Fetch.failRequest',{requestId:event.requestId,errorReason:'ConnectionClosed'});
    }else if(pauseDownload&&event.responseStatusCode===200){pauseDownload=false;pausedDownload=event;}
    else if(loseNext&&event.responseStatusCode===200){loseNext=false;dropped++;
      const response=await page.call('Fetch.getResponseBody',{requestId:event.requestId});const body=JSON.parse(response.base64Encoded?Buffer.from(response.body,'base64').toString():response.body);
      lostReceipt=body.task.metadata.accepted_file_receipt;
      await page.call('Fetch.failRequest',{requestId:event.requestId,errorReason:'ConnectionClosed'});
      await page.call('Fetch.disable');
    }else await page.call('Fetch.continueRequest',{requestId:event.requestId});
  });
  await page.call('Page.navigate',{url:config.origin+'/ui/'});
  await page.wait("!!document.querySelector('input[name=username]')",'real Keycloak login');
  await page.evaluate(`(()=>{document.querySelector('input[name=username]').value=${JSON.stringify(config.username)};document.querySelector('input[name=password]').value=${JSON.stringify(config.password)};document.querySelector('#kc-login').click();})()`);
  await page.wait("!!document.querySelector('.composer textarea')",'actual authenticated owner app');
  check(pkce&&!!bearer,'real Keycloak Authorization Code + PKCE S256 owner login');
  check(await page.evaluate("document.title==='Чат' && !document.querySelector('.brand,.brand-mark') && !document.querySelector('.chat-heading').textContent.includes('Core Agent')"),'neutral UI chrome and browser title contain no product brand');
  await page.files(config.files.slice(0,2));
  await page.wait("document.querySelectorAll('.composer-attachments li').length===2",'native selected file batch');
  check(await page.evaluate("document.querySelector('.attachment-summary').textContent.includes('25 Б')"),'native multiple-file selection shows aggregate decoded bytes');
  await page.click('Отправить ↑');
  await page.wait("document.querySelector('.thread')?.textContent.includes('report_2.txt')",'root safe file receipt');
  check(posts.length===1&&posts[0].parts.length===2&&posts[0].parts.every(part=>typeof part.raw==='string'&&part.filename==='report.txt'&&part.mediaType==='text/plain'&&!('text' in part)),'files-only official A2A 1.0 raw Parts reach actual backend');
  await page.wait("[...document.querySelectorAll('.interaction:not(.resolved)')].some(card=>card.querySelector('h3')?.textContent==='Разрешение на действие')",'actual owner tool approval wait');
  check(await page.evaluate("document.querySelector('.pending-interaction-bar')?.textContent.includes('Требуется ваше разрешение') && document.querySelector('.interaction .action-preview')?.textContent.includes('python3') && document.querySelector('.interaction')?.textContent.includes('Ответить до')"),'active approval explains saved command, deadline and one-call consequences');
  await page.click('Перейти к запросу');
  check(await page.evaluate("document.activeElement?.classList.contains('interaction')"),'approval jump moves keyboard focus to its actual request');
  await page.wait("document.querySelector('.chat-link strong')?.textContent && document.querySelector('.chat-link strong').textContent!=='Новый чат'",'canonical original attachment generates meaningful shared title');
  await page.evaluate("document.querySelector('.chat-menu').open=true");
  await page.click('Переименовать');
  await page.field('.chat-title-form input','Проверка файлов и диаграмм');
  const renamed=await ownerAction(page,'/api/chats/'+encodeURIComponent(rootTask.contextId)+'/title','PUT',()=>page.click('Сохранить название'));
  check(renamed.body.title==='Проверка файлов и диаграмм'&&renamed.body.title_revision===renamed.request.expected_revision+1,'actual owner rename persists company chat title with CAS');
  await page.wait("document.querySelector('.chat-heading h1')?.textContent==='Проверка файлов и диаграмм' && document.querySelector('.chat-link strong')?.textContent==='Проверка файлов и диаграмм'",'header and navigation use saved title instead of UUID');
  check(await page.evaluate("document.querySelectorAll('.chat-link time,.chat-link small:not(.chat-attention)').length===0"),'chat navigation shows meaningful titles without update dates or times');
  check(await page.evaluate("[...document.querySelectorAll('.chat-menu button')].find(button=>button.textContent==='Удалить чат')?.disabled && !document.querySelector('.connection-line [role=status]').textContent.includes('Соединение потеряно')"),'initial active chat has no false connection warning and cannot be deleted');
  breakStreams=true;
  await page.call('Fetch.enable',{patterns:[{urlPattern:config.origin+'/a2a/owner/tasks/*',requestStage:'Request'}]});
  const readsBefore=tasks.length;
  await page.call('Page.reload');
  await page.wait("!!document.querySelector('.chat-link')",'chat list after reconnect test reload');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await waitFor(()=>failedStreams>=2&&tasks.length>=readsBefore+2,'actual failed SSE with successful canonical reads');
  check(await page.evaluate("!document.querySelector('.connection-line [role=status]').textContent.includes('Соединение потеряно')"),'SSE outage with healthy canonical polling does not claim connection loss');
  breakTaskReads=true;
  await page.wait("document.querySelector('.connection-line [role=status]').textContent.includes('Соединение потеряно')",'actual canonical read outage is visible');
  breakTaskReads=false;breakStreams=false;
  await page.call('Fetch.disable');
  await page.wait("!document.querySelector('.connection-line [role=status]').textContent.includes('Соединение потеряно') && !!document.querySelector('.interaction:not(.resolved) button')",'canonical recovery clears actual outage without losing approval');
  check(true,'actual canonical read failure and recovery preserve pending chat work');
  await page.files([config.files[2]]);
  await page.wait("document.querySelectorAll('.composer-attachments li').length===1",'follow-up selected file');
  loseNext=true;
  await page.call('Fetch.enable',{patterns:[{urlPattern:config.origin+'/a2a/owner/message:send',requestStage:'Response'}]});
  await page.click('Отправить ↑');
  await page.wait("document.querySelector('.thread [role=alert]')?.textContent.includes('Повторите тот же запрос')",'uncertain accepted follow-up');
  check(dropped===1&&!!lostReceipt&&await page.evaluate("document.querySelector('.composer textarea').disabled&&document.querySelector('input[type=file]').disabled&&document.querySelector('.composer-attachments button').disabled"),'lost completed ACK preserves and freezes pending follow-up');

  const created=await page.call('Target.createTarget',{url:'about:blank'});second=await CDP.page(created.targetId);
  await second.call('Page.enable');await second.call('Runtime.enable');
  observeOwnerAPI(second);
  const secondTokens=captureCredentials(second);await second.call('Network.enable');
  let settingsWrites=0;
  second.on('Network.responseReceived',({response})=>{if(response.url===config.origin+'/api/settings'&&response.status===200)settingsWrites++;});
  await second.call('Page.navigate',{url:config.origin+'/ui/'});
  await second.wait("!!document.querySelector('.composer textarea')",'second real authenticated owner tab');
  await second.click('Настройки','.nav-item');
  await second.wait("!!document.querySelector('.form-grid input')",'actual company settings');
  async function setLimit(value){
    if(!await second.evaluate("[...document.querySelectorAll('.form-grid label')].some(label=>label.textContent.includes('Общий размер вложений, байт'))")){
      await second.click('Настройки','.nav-item');
      await second.wait("[...document.querySelectorAll('.form-grid label')].some(label=>label.textContent.includes('Общий размер вложений, байт'))",'actual company attachment settings');
    }
    const before=settingsWrites;
    await second.evaluate(`(()=>{const label=[...document.querySelectorAll('.form-grid label')].find(label=>label.textContent.includes('Общий размер вложений, байт'));const input=label.querySelector('input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(String(value))});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await delay(100);await second.evaluate("document.querySelector('form.form-sheet').requestSubmit()");
    await waitFor(()=>settingsWrites>before,'actual settings write');
    await second.wait("document.querySelector('[role=status]')?.textContent.includes('Настройки сохранены')",'real settings CAS');
  }
  await setLimit(1);
  await page.click('Повторить тот же запрос');
  await page.wait("document.querySelector('.thread')?.textContent.includes('report_3.txt')",'accepted follow-up safe receipt after retry');
  check(posts.length===3&&posts[1].digest===posts[2].digest&&posts[1].messageId===posts[2].messageId&&posts[1].taskId===rootTask.id,'identical pending payload deduplicates after real company limit reduction');
  check(receipts.at(-1).accepted_file_receipt.batch_id===lostReceipt.batch_id&&receipts.at(-1).accepted_file_receipt.entries[0].actual_name==='report_3.txt','repeated follow-up ACK reuses server-owned actual filename and batch');
  await setLimit(25000000);
  await second.click('Инструменты','.nav-item');
  await second.wait("document.querySelectorAll('form.policy:not([hidden])').length===7",'actual configured tool policy catalog');
  async function setPolicy(name,mode,exempt){
    const response=await ownerAction(second,'/api/tool-policies/'+name,'PUT',async()=>{
      await second.evaluate(`(()=>{const form=[...document.querySelectorAll('form.policy')].find(form=>form.dataset.toolName===${JSON.stringify(name)});const select=form.querySelector('select');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(mode)});select.dispatchEvent(new Event('change',{bubbles:true}));const checkbox=form.querySelector('input[type=checkbox]');if(checkbox.checked!==${!exempt})checkbox.click();})()`);
      await delay(100);await second.evaluate(`([...document.querySelectorAll('form.policy')].find(form=>form.dataset.toolName===${JSON.stringify(name)})).requestSubmit()`);
    });
    check(response.body.mode===mode&&response.body.guardrails_exempt===exempt&&response.request.mode===mode&&response.request.guardrails_exempt===exempt,'actual owner policy persists independent access and material exemption for '+name);
    await second.wait(`(()=>{const form=[...document.querySelectorAll('form.policy')].find(form=>form.dataset.toolName===${JSON.stringify(name)});return form.querySelector('select').value===${JSON.stringify(mode)}&&form.querySelector('input[type=checkbox]').checked===${!exempt}&&!form.querySelector('select').disabled&&form.querySelector('button').disabled;})()`,'policy refresh after actual CAS');
  }
  await setPolicy('core_cron_create','deny',true);
  await setPolicy('core_terminal_exec','require_hitl',true);
  await second.field('.tools-toolbar input[type=search]','core_terminal');
  check(await second.evaluate("document.querySelectorAll('form.policy:not([hidden])').length===1 && document.querySelector('form.policy:not([hidden])').dataset.toolName==='core_terminal_exec' && !document.querySelector('form.policy:not([hidden]) h3').textContent.includes('core_')"),'tool search finds technical names while readable title stays primary');
  await second.field('.tools-toolbar input[type=search]','');
  await second.field('.tools-toolbar label:nth-child(3) select','deny');
  check(await second.evaluate("document.querySelectorAll('form.policy:not([hidden])').length===1 && document.querySelector('form.policy:not([hidden])').dataset.toolName==='core_cron_create'"),'execution-mode filter shows actual denied rules');
  await second.field('.tools-toolbar label:nth-child(3) select','all');
  await second.field('.tools-toolbar label:nth-child(2) select','mcp');
  check(await second.evaluate("document.querySelectorAll('form.policy:not([hidden])').length===0"),'source filter distinguishes actual builtin origin');
  await second.field('.tools-toolbar label:nth-child(2) select','all');
  await second.evaluate("document.querySelector('.tools-toolbar input[type=checkbox]').click()");
  check(await second.evaluate("document.querySelectorAll('form.policy:not([hidden])').length===2 && [...document.querySelectorAll('form.policy:not([hidden]) input[type=checkbox]')].every(input=>!input.checked)"),'checks-off filter and positive checkbox preserve independent exemption semantics');
  await second.evaluate("document.querySelector('.tools-toolbar input[type=checkbox]').click()");

  await second.click('Расписания','.nav-item');
  await second.wait("!!document.querySelector('.schedules')&&!document.querySelector('.schedules [role=status]')",'actual schedules list');
  await second.click('Создать расписание ＋');
  await second.wait("!!document.querySelector('.schedules textarea')",'actual schedule creation form');
  check(await second.evaluate("document.querySelectorAll('.schedules .form-grid input')[1].value==='Europe/Moscow'"),'owner schedule starts with explicit Moscow timezone');
  await second.field('.schedules textarea',schedulePrompt);
  await second.field('.schedules .form-grid input','0 0 1 1 *');
  await second.field('.schedules select',rootTask.contextId);
  const createdSchedule=await ownerAction(second,'/api/schedules','POST',()=>second.click('Сохранить','.schedules form button'));
  let schedule=createdSchedule.body.schedule;
  check(schedule.context_id===rootTask.contextId&&schedule.expression==='0 0 1 1 *'&&schedule.timezone==='Europe/Moscow'&&schedule.active_task_id===rootTask.id,'UI creates same-chat schedule while cron model tool is denied');
  await second.wait("!!document.querySelector('.schedule')&&!document.querySelector('.schedules form')",'actual created schedule');
  check(await second.evaluate("[...document.querySelectorAll('.schedule button')].find(button=>button.textContent==='Запустить сейчас').disabled"),'schedule run-now is disabled while original chat waits for owner approval');
  await second.click('Изменить','.schedule button');
  await second.wait("!!document.querySelector('.schedules form')",'actual schedule edit');
  await second.field('.schedules .form-grid label:nth-child(3) input','Europe/Berlin');
  const editedSchedule=await ownerAction(second,'/api/schedules/'+schedule.id,'PUT',()=>second.click('Сохранить','.schedules form button'));
  schedule=editedSchedule.body.schedule;
  check(schedule.context_id===rootTask.contextId&&schedule.timezone==='Europe/Berlin'&&schedule.revision===createdSchedule.body.schedule.revision+1,'actual schedule edit persists Berlin timezone and revision');
  await second.wait("!document.querySelector('.schedules form')",'schedule edit accepted');

  await second.click('Агенты','.nav-item');
  await second.wait("[...document.querySelectorAll('button')].some(button=>button.textContent.trim()==='Добавить агента ＋')",'actual owner peer registry');
  await second.click('Добавить агента ＋');
  await second.wait("!!document.querySelector('.form-sheet input[pattern]')",'actual custom-header peer form');
  await second.call('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  check(await second.evaluate("(()=>{const name=document.querySelector('.form-sheet input[pattern]').getBoundingClientRect();const address=document.querySelector('.form-sheet input[type=url]').getBoundingClientRect();const help=document.getElementById('peer-name-help');return help.getBoundingClientRect().height>=2*parseFloat(getComputedStyle(help).fontSize)&&Math.abs(name.top-address.top)<1&&Math.abs(name.height-address.height)<1;})()"),'peer inputs keep aligned intrinsic height when name help wraps');
  await second.call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  check(await second.evaluate("(()=>{const name=document.querySelector('.form-sheet input[pattern]').getBoundingClientRect();const address=document.querySelector('.form-sheet input[type=url]').getBoundingClientRect();return address.top>=name.bottom&&Math.abs(name.height-address.height)<1&&document.documentElement.scrollWidth<=innerWidth;})()"),'mobile peer form stacks equal-height fields without overflow');
  await second.call('Emulation.clearDeviceMetricsOverride');
  await second.field('.form-sheet input[pattern]','browser-disabled_peer');
  await second.click('Добавить агента ＋');
  await second.field('.form-sheet input[type=url]','https://peer.invalid/a2a');
  await second.field('.form-sheet textarea','Disabled local browser proof peer');
  await second.field('.form-sheet .form-grid label:nth-child(4) input','X-Browser-Proof-Key');
  await second.field('.form-sheet select','replace');
  await second.wait("!!document.querySelector('.form-sheet input[type=password]')",'actual peer secret editor');
  await second.field('.form-sheet input[type=password]',peerSecret);
  const peerPostsBefore=peerPostAttempts;
  for(const invalidName of ['Агент погоды','agent with spaces','x'.repeat(129)]){
    await second.field('.form-sheet input[pattern]',invalidName);
    check(await second.evaluate("(()=>{const input=document.querySelector('.form-sheet input[pattern]');return !input.checkValidity()&&input.validity.patternMismatch&&input.maxLength===128&&document.getElementById(input.getAttribute('aria-describedby')).textContent.includes('Латинские буквы');})()"),'peer name validation explains and rejects unsupported names');
    await second.click('Сохранить подключение','.form-sheet button');
  }
  await second.field('.form-sheet input[pattern]','browser-disabled_peer');
  check(await second.evaluate("document.querySelector('.form-sheet input[pattern]').checkValidity()"),'peer name accepts an ASCII hyphen and underscore');
  const createdPeer=await ownerAction(second,'/api/remote-agents','POST',()=>second.click('Сохранить подключение','.form-sheet button'));
  check(peerPostAttempts===peerPostsBefore+1,'invalid peer names send no mutation before a valid name is saved');
  const peer=createdPeer.body;
  check(peer.header_name==='X-Browser-Proof-Key'&&peer.has_header_value===true&&peer.enabled===true&&!JSON.stringify(peer).includes(peerSecret)&&!('header_value' in peer),'custom-header secret is saved but omitted from actual owner metadata');
  await second.wait("!!document.querySelector('.peer')&&!document.querySelector('.form-sheet')",'actual saved peer');
  await second.click('Настроить','.peer button');
  await second.wait("!!document.querySelector('.form-sheet input[type=checkbox]')",'actual peer disable form');
  await second.click('Настроить','.peer button');
  await second.evaluate("document.querySelector('.form-sheet input[type=checkbox]').click()");
  const disabledPeer=await ownerAction(second,'/api/remote-agents/'+peer.id,'PUT',()=>second.click('Сохранить подключение','.form-sheet button'));
  check(disabledPeer.body.enabled===false&&disabledPeer.body.has_header_value===true&&disabledPeer.request.header_value===undefined&&disabledPeer.body.revision===peer.revision+1,'UI disable preserves private peer secret without resending it');
  await second.wait("!document.querySelector('.form-sheet')",'peer disabled before caller resumes');
  await second.call('Page.reload');
  await second.wait("!!document.querySelector('.composer textarea')",'actual authenticated peer reread after reload');
  const rereadPeer=await ownerAction(second,'/api/remote-agents','GET',()=>second.click('Агенты','.nav-item'));
  check(rereadPeer.body.agents.length===1&&rereadPeer.body.agents[0].enabled===false&&rereadPeer.body.agents[0].has_header_value===true&&!JSON.stringify(rereadPeer.body).includes(peerSecret),'actual private peer reread exposes configured flag and disabled state only');
  await second.click('Настроить','.peer button');
  await second.wait("!!document.querySelector('.form-sheet select')",'persisted peer editor');
  await second.field('.form-sheet select','replace');
  await second.wait("!!document.querySelector('.form-sheet input[type=password]')",'private secret replacement field');
  check(await second.evaluate("document.querySelector('.form-sheet input[type=password]').value===''&&document.querySelector('.form-sheet .form-grid label:nth-child(4) input').value==='X-Browser-Proof-Key'&&!document.querySelector('.form-sheet input[type=checkbox]').checked"),'persisted custom header rereads with empty secret field and disabled peer');
  await second.click('Закрыть','.form-sheet button');

  const peerDeletesBefore=peerDeleteAttempts;
  rejectNextPeerDelete=true;
  await second.click('Удалить','.peer button');
  await waitFor(()=>!rejectNextPeerDelete,'peer deletion confirmation cancelled');
  check(peerDeleteAttempts===peerDeletesBefore&&await second.evaluate("document.querySelectorAll('.peer').length===1"),'cancelled peer deletion sends no mutation');
  let holdPeerList=false,heldPeerList,losePeerDelete=false;
  second.on('Fetch.requestPaused',async event=>{
    if(!new URL(event.request.url).pathname.startsWith('/api/remote-agents'))return;
    if(losePeerDelete&&event.request.method==='DELETE'&&event.responseStatusCode===200){losePeerDelete=false;await second.call('Fetch.failRequest',{requestId:event.requestId,errorReason:'Failed'});return;}
    if(holdPeerList&&event.request.method==='GET'&&event.responseStatusCode===200){holdPeerList=false;heldPeerList=event;return;}
    await second.call('Fetch.continueRequest',{requestId:event.requestId});
  });
  await second.call('Fetch.enable',{patterns:[{urlPattern:config.origin+'/api/remote-agents*',requestStage:'Response'}]});
  await second.click('Добавить агента ＋');
  await second.field('.form-sheet input[pattern]','browser-delete-unknown');
  await second.field('.form-sheet input[type=url]','https://peer.invalid/a2a');
  await second.evaluate("document.querySelector('.form-sheet input[type=checkbox]').click()");
  holdPeerList=true;
  const extraPeer=await ownerAction(second,'/api/remote-agents','POST',()=>second.click('Сохранить подключение','.form-sheet button'));
  await waitFor(()=>!!heldPeerList,'actual pre-deletion peer listing response held');
  await second.click('Закрыть','.form-sheet button');
  await second.click('Добавить агента ＋');
  await second.field('.form-sheet input[pattern]','unsaved-new-editor');
  let peerReadsBefore=apiResponses.length;
  await second.call('Fetch.continueRequest',{requestId:heldPeerList.requestId});
  await waitFor(()=>apiResponses.slice(peerReadsBefore).some(response=>response.path==='/api/remote-agents'&&response.method==='GET'),'save reread released after opening another editor');
  check(await second.evaluate("document.querySelector('.form-sheet input[pattern]')?.value==='unsaved-new-editor'"),'late saved callback preserves the new editor and its input');
  await second.click('Закрыть','.form-sheet button');
  await second.evaluate("([...document.querySelectorAll('.peer')].find(article=>article.querySelector('h3').textContent==='browser-delete-unknown')).querySelector('button.secondary').click()");
  await second.wait("!!document.querySelector('.form-sheet textarea')",'other peer editor reopened');
  await second.field('.form-sheet textarea','Before deletion');
  heldPeerList=null;holdPeerList=true;
  await ownerAction(second,'/api/remote-agents/'+extraPeer.body.id,'PUT',()=>second.click('Сохранить подключение','.form-sheet button'));
  await waitFor(()=>!!heldPeerList,'second pre-deletion peer listing held');
  await second.click('Закрыть','.form-sheet button');
  const deletedPeer=await ownerAction(second,'/api/remote-agents/'+peer.id+'/connection','DELETE',()=>second.evaluate(`([...document.querySelectorAll('.peer')].find(article=>article.querySelector('h3').textContent===${JSON.stringify(peer.name)})).querySelector('button.danger').click()`));
  check(deletedPeer.body.id===peer.id&&!deletedPeer.body.enabled&&!deletedPeer.body.has_header_value&&deletedPeer.request.expected_revision===disabledPeer.body.revision,'confirmed peer deletion returns a scoped disabled receipt');
  await second.wait("document.querySelectorAll('.peer').length===1&&document.querySelector('.peer h3').textContent==='browser-delete-unknown'",'deleted peer absent from current shared list');
  peerReadsBefore=apiResponses.length;
  await second.call('Fetch.continueRequest',{requestId:heldPeerList.requestId});
  await waitFor(()=>apiResponses.slice(peerReadsBefore).some(response=>response.path==='/api/remote-agents'&&response.method==='GET'&&response.body.agents.some(agent=>agent.id===peer.id)),'actual stale peer listing released');
  check(await second.evaluate("document.querySelectorAll('.peer').length===1&&document.querySelector('.peer h3').textContent==='browser-delete-unknown'"),'late peer listing cannot restore a deleted connection');
  losePeerDelete=true;
  await second.click('Удалить','.peer button');
  await second.wait("document.querySelector('.page .error')?.textContent.includes('Не удалось подтвердить удаление подключения')&&document.querySelectorAll('.peer').length===0",'lost peer deletion receipt reconciled with shared list');
  check(peerDeleteAttempts===peerDeletesBefore+2&&!losePeerDelete&&extraPeer.body.id!==peer.id,'unconfirmed peer deletion rereads shared state without replay');
  await second.call('Fetch.disable');
  await second.click('Добавить агента ＋');
  await second.field('.form-sheet input[pattern]',peer.name);
  await second.field('.form-sheet input[type=url]','https://peer.invalid/a2a');
  await second.evaluate("document.querySelector('.form-sheet input[type=checkbox]').click()");
  const recreatedPeer=await ownerAction(second,'/api/remote-agents','POST',()=>second.click('Сохранить подключение','.form-sheet button'));
  check(recreatedPeer.body.id!==peer.id&&recreatedPeer.body.revision===1&&!recreatedPeer.body.has_header_value,'recreated peer name has a new identity without old credentials');
  await second.wait("!document.querySelector('.form-sheet')&&document.querySelectorAll('.peer').length===1",'new peer saved');
  await second.call('Page.reload');
  await second.wait("!!document.querySelector('.composer textarea')",'peer deletion survives browser reload');
  await second.click('Агенты','.nav-item');
  await second.wait(`document.querySelectorAll('.peer').length===1&&document.querySelector('.peer h3').textContent===${JSON.stringify(peer.name)}`,'only recreated peer remains after reload');

  await page.click('Файлы');
  await page.evaluate("document.querySelector('.file-cleanup').open=true");
  await page.wait("document.querySelectorAll('.workspace-file-name').length>=2",'active original chat workspace preview');
  await page.evaluate("document.querySelector('.workspace-file-choice input').click()");
  await page.wait("!!document.querySelector('.workspace-cleanup-actions button')",'active file selection');
  check(await page.evaluate("document.querySelector('#workspace-age').value===''&&[...document.querySelectorAll('.workspace-cleanup-actions button')].find(button=>button.textContent.trim()==='Удалить выбранные…').disabled"),'workspace defaults to all ages and disables selected deletion during active owner wait');
  await page.click('Снять выбор','.workspace-cleanup-actions button');
  await page.click('Закрыть','.workspace-files button');
  await page.evaluate("(()=>{const t=document.querySelector('.thread');t.scrollTop=0;t.dispatchEvent(new Event('scroll'));})()");
  await delay(300);
  let approvals=0;
  const deadline=Date.now()+60000;
  while(Date.now()<deadline){
    if(await page.evaluate("document.querySelector('.thread')?.textContent.includes('Native file reads completed.')"))break;
    if(tasks.at(-1)?.status.state==='TASK_STATE_FAILED')throw new Error('Actual backend Task failed during native file verification');
    const available=await page.evaluate("[...document.querySelectorAll('.interaction:not(.resolved) button')].some(button=>button.textContent.trim()==='Разрешить'&&!button.disabled)");
    if(available){if(approvals===3)await setLimit(1);await page.click('Разрешить','.interaction:not(.resolved) button');approvals++;await delay(500);}else await delay(200);
  }
  check(await page.evaluate("document.querySelector('.thread')?.textContent.includes('Native file reads completed.')"),'actual native terminal outcome');
  await page.wait("[...document.querySelectorAll('.markdown h2')].some(h=>h.textContent==='Formatted response') && [...document.querySelectorAll('.diagram img')].some(img=>img.complete&&img.naturalWidth>0)",'actual Markdown and Mermaid result');
  check(await page.evaluate("!!document.querySelector('.markdown strong') && !!document.querySelector('.markdown table') && document.querySelectorAll('.markdown ul li').length>=2 && !!document.querySelector('.markdown code.language-python')"),'Markdown headings lists tables and fenced code render');
  check(await page.evaluate("!!document.querySelector('.markdown code.language-python .hljs-built_in')"),'Python fenced code has native syntax highlighting');
  await page.wait("[...document.querySelectorAll('.diagram')].some(d=>d.textContent.includes('Не удалось построить диаграмму')&&d.querySelector('details')?.open)",'invalid Mermaid keeps source');
  check(await page.evaluate("!window.__markdownExecuted && !document.querySelector('.markdown script, .markdown iframe, .markdown a[href^=javascript], .markdown img:not([src^=\"blob:\"])')")&&untrustedRequests.length===0,'untrusted Markdown and Mermaid cannot execute or fetch external images');
  check(await page.evaluate("document.querySelector('.thread').scrollTop<8 && !!document.querySelector('.new-messages')"),'new runtime events preserve reading position and offer new-message jump');
  await page.click('Новые сообщения ↓');
  await page.wait("(()=>{const t=document.querySelector('.thread');return t.scrollHeight-t.scrollTop-t.clientHeight<100;})()",'explicit latest-message jump');
  check(await page.evaluate("(()=>{const keys=[...document.querySelectorAll('[data-action-key]')].map(card=>card.dataset.actionKey);return keys.length===4&&new Set(keys).size===4&&!!document.querySelector('.execution-group')&&!document.querySelector('.thread').textContent.includes('Вызов инструмента');})()"),'one correlated card per Task/call and consecutive successful execution group');
  await page.evaluate("document.querySelector('.execution-group').open=true;document.querySelector('.action-output').open=true");
  check(await page.evaluate("document.querySelector('.action-output pre')?.textContent.includes('browser-native-root-verified') && document.querySelector('.action-technical')?.textContent.includes('ID вызова')"),'real terminal output preserves readable newlines and technical data remains disclosed separately');
  async function readableAnswer(){
    check(await page.evaluate("(()=>{const steps=document.querySelector('.execution-steps');return steps.getBoundingClientRect().height<=Math.min(innerHeight*.48,480)+1&&getComputedStyle(steps).overflowY==='auto'&&steps.tabIndex===0;})()"),'expanded execution remains a bounded keyboard-accessible region');
    await page.evaluate("(()=>{const thread=document.querySelector('.thread');thread.scrollTop=thread.scrollHeight;})()");
    check(await page.evaluate("(()=>{const thread=document.querySelector('.thread'),answer=document.querySelector('.history-run .message:last-child'),t=thread.getBoundingClientRect(),a=answer.getBoundingClientRect(),composer=document.querySelector('.composer').getBoundingClientRect();return thread.scrollHeight-thread.scrollTop-thread.clientHeight<2&&a.bottom<=t.bottom+1&&t.bottom<=composer.top+1;})()"),'final answer stays reachable above composer after expanding execution');
  }
  await readableAnswer();
  await page.field('.composer textarea','Первая строка\nВторая строка\nТретья строка\nЧетвёртая строка');
  check(await page.evaluate("document.querySelector('.composer textarea').getBoundingClientRect().height>70"),'composer grows with actual multiline input');
  await page.field('.composer textarea','');
  check(await page.evaluate("document.querySelector('.composer textarea').getBoundingClientRect().height<90 && document.querySelector('.attachment-summary').textContent.includes('До 25 МБ суммарно') && !document.querySelector('.connection-line').textContent.includes('синхронизировано')"),'compact composer shows aggregate human-sized limit and quiet successful connection');
  await page.call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await readableAnswer();
  check(await page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('.thread').clientHeight>100"),'actual mobile viewport keeps bounded readable history without horizontal overflow');
  await page.evaluate("document.querySelector('.composer textarea').focus()");
  await page.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  await page.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  check(await page.evaluate("(()=>{const r=document.activeElement.getBoundingClientRect();return document.activeElement.textContent.includes('Прикрепить')&&r.top>=0&&r.bottom<=innerHeight;})()"),'keyboard focus stays visible above mobile composer boundary');
  await page.call('Emulation.clearDeviceMetricsOverride');
  check(await page.evaluate(`(()=>{
    const rgb=value=>value.match(/[0-9.]+/g).slice(0,3).map(Number);
    const luminance=value=>rgb(value).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const ratio=(a,b)=>{const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
    const text=getComputedStyle(document.querySelector('.attachment-summary'));
    const field=getComputedStyle(document.activeElement);
    const button=getComputedStyle(document.querySelector('.attach-button'));
    return parseFloat(field.outlineWidth)>=3 && ratio(text.color,'rgb(250,250,250)')>=4.5 && ratio(field.outlineColor,'rgb(250,250,250)')>=3 && ratio(button.borderColor,'rgb(250,250,250)')>=3;
  })()`),'actual muted text, focus outline and button boundary meet contrast requirements');

  const diagramURL=await page.evaluate("document.querySelector('.diagram img').src");
  await page.click('Обновить историю');
  await page.wait("!document.querySelector('.history-refresh:disabled')",'history refresh finished');
  check(await page.evaluate(`document.querySelector('.diagram img')?.src===${JSON.stringify(diagramURL)} && document.querySelector('.diagram img').naturalWidth>0`),'history refresh preserves the rendered Mermaid image');
  check(approvals===4,'actual owner decisions resume both native reads, snapshot selection and source deletion');
  check(await page.evaluate("![...document.querySelectorAll('.interaction.resolved')].some(card=>['Разрешение на действие','Проверка материала'].includes(card.querySelector('h3')?.textContent))"),'completed approval cards disappear while native decisions remain persisted');
  await page.call('Page.reload');
  await page.wait("!!document.querySelector('.chat-link')",'persisted company chat after browser reload');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await page.wait("document.querySelectorAll('.message .message-attachments:not(.response-files) li').length===3",'published attachment history after reload');
  const outcomes=history.at(-1).filter(item=>item.kind==='tool_result').map(item=>JSON.parse(item.text));
  check(['browser-native-root-verified','browser-native-followup-verified'].every(marker=>outcomes.some(result=>result.tool_call_id===marker&&result.status==='succeeded'&&result.output.exit_code===0&&result.output.stdout.trim()===marker))&&await page.evaluate("document.querySelector('.thread').textContent.includes('report_3.txt')"),'actual native file reads and persisted attachment history');
  check(outcomes.some(result=>result.tool_call_id==='browser-native-output-selected'&&result.status==='succeeded'&&result.output.files.length===2)&&outcomes.some(result=>result.tool_call_id==='browser-native-output-deleted'&&result.status==='succeeded'&&result.output.exit_code===0&&result.output.stdout.trim()==='browser-native-output-deleted'),'actual native files are selected then changed and deleted before final response');
  const entries=history.at(-1).flatMap(item=>item.attachments??[]);
  check(entries.length===3&&new Set(entries.map(entry=>entry.actual_name)).size===3,'published typed history has exact safe attachment names');
  const latest=tasks.at(-1);
  check(latest.metadata.file_receipt.entries.length===2&&!latest.metadata.accepted_file_receipt,'transient follow-up receipt never replaces persisted root metadata');
  await page.wait("document.querySelectorAll('.response-files li').length===2",'actual final response file controls');
  const output=history.at(-1).find(item=>item.kind==='result'&&item.status==='available').response_files;
  check(output.length===2&&output.map(file=>file.name).join(',')==='report-output.txt,empty.txt'&&output.every(file=>Object.keys(file).sort().join(',')==='file_id,media_type,name,sha256,size_bytes'),'ordered public response receipts render in matching final message');
  await page.call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:config.downloads});
  pauseDownload=true;
  await page.call('Fetch.enable',{patterns:[{urlPattern:config.origin+'/api/chats/*/tasks/*/files/*',requestStage:'Response'}]});
  await page.click('Скачать','.response-files button');
  await waitFor(()=>!!pausedDownload,'actual response download paused');
  check(await page.evaluate("(()=>{return [...document.querySelectorAll('.response-files li:first-child button')].every(button=>button.disabled)&&[...document.querySelectorAll('.response-files li:nth-child(2) button')].every(button=>!button.disabled);})()"),'only the clicked actual file download is disabled');
  await page.click('Новый чат');
  await page.wait("!!document.querySelector('.welcome')&&!document.querySelector('.response-files')",'actual chat switch during pending download');
  await page.call('Fetch.disable');
  await waitFor(()=>cancelledDownloads.has(pausedDownload.networkId),'download abort on actual chat switch');
  check(await readFile(config.downloads+'/'+output[0].name).then(()=>false,error=>error.code==='ENOENT'),'cancelled stale completion creates no download in the new chat');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await page.wait("document.querySelectorAll('.response-files li').length===2",'frozen final response after chat return');
  check(await page.evaluate("document.querySelector('.chat-heading h1')?.textContent==='Проверка файлов и диаграмм'"),'saved title survives real authenticated browser reload');
  await page.click('Открыть','.response-files li:first-child button');
  await page.wait(`document.querySelector('.thread .file-preview pre')?.textContent===${JSON.stringify("Native immutable output\n")}`,'authenticated immutable text file preview');
  await page.click('Закрыть','.thread .response-files li:first-child .file-preview button');
  await page.click('Файлы');
  check(await page.evaluate("[...document.querySelectorAll('.conversation-files h3')].some(h=>h.textContent.includes('Прикреплённые')) && [...document.querySelectorAll('.conversation-files h3')].some(h=>h.textContent.includes('Созданные агентом'))"),'files panel separates incoming materials and agent-selected output');
  await page.click('К исходному сообщению','.conversation-files button');
  check(await page.evaluate("document.activeElement?.dataset.historyId && !document.querySelector('.conversation-files-panel')"),'file source jump uncovers and focuses canonical history message');

  for(const file of output){
    const before=outputRequests.length;
    await page.click('Скачать','.response-files li:nth-child('+(output.indexOf(file)+1)+') button');
    const bytes=await downloaded(file);
    check(bytes.length===file.size_bytes&&hash(bytes)===file.sha256&&(file.name==='empty.txt'?bytes.length===0:bytes.equals(Buffer.from('Native immutable output\n'))),'actual UI download preserves immutable bytes after native source deletion and limit reduction for '+file.name);
    check(outputRequests.length===before+1&&outputRequests.at(-1).authorized&&!outputRequests.at(-1).query&&outputRequests.at(-1).path==='/api/chats/'+encodeURIComponent(rootTask.contextId)+'/tasks/'+encodeURIComponent(rootTask.id)+'/files/'+encodeURIComponent(file.file_id),'actual UI download uses current bearer and exact scoped file ID for '+file.name);
  }
  await page.click('Файлы');
  await page.evaluate("document.querySelector('.file-cleanup').open=true");
  await page.wait("document.querySelectorAll('.workspace-file-name').length===3",'actual authenticated workspace preview');
  for(const entry of entries){
    const path='/api/chats/'+encodeURIComponent(rootTask.contextId)+'/files/content?'+new URLSearchParams({path:entry.relative_path});
    const result=await page.evaluate(`(async()=>{const response=await fetch(${JSON.stringify(path)},{headers:{Authorization:${JSON.stringify(bearer)}},cache:'no-store',credentials:'omit'});const bytes=await response.arrayBuffer();const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(byte=>byte.toString(16).padStart(2,'0')).join('');return {status:response.status,size:bytes.byteLength,digest,nosniff:response.headers.get('x-content-type-options')};})()`);
    check(result.status===200&&result.size===entry.size_bytes&&result.digest===entry.sha256&&result.nosniff==='nosniff','authenticated browser download matches accepted size and SHA-256 for '+entry.actual_name);
    check(await page.evaluate(`(async()=>{const response=await fetch(${JSON.stringify(path)},{cache:'no-store',credentials:'omit'});return response.status===401;})()`),'download requires fresh server authorization for '+entry.actual_name);
  }
  await page.click('Закрыть','.workspace-files button');
  await page.files([config.files[2]]);
  await page.evaluate("(()=>{const field=document.querySelector('.composer textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'Keep this draft');field.dispatchEvent(new Event('input',{bubbles:true}));})()");
  await delay(100);const before=posts.length;await page.click('Отправить ↑');
  await page.wait("document.querySelector('.thread [role=alert]')?.textContent.includes('15 байт')",'actual current company limit before encoding');
  check(posts.length===before&&await page.evaluate("document.querySelector('.composer textarea').value==='Keep this draft'&&document.querySelectorAll('.composer-attachments li').length===1"),'current company limit rejects whole browser batch and preserves draft without POST');
  await second.click('Расписания','.nav-item');
  await second.wait("[...document.querySelectorAll('.schedule button')].some(button=>button.textContent==='Запустить сейчас'&&!button.disabled)",'same-chat schedule becomes runnable after original root completes');
  const manualRun=await ownerAction(second,'/api/schedules/'+schedule.id+'/run-now','POST',()=>second.click('Запустить сейчас','.schedule button'));
  check(manualRun.body.task.contextId===rootTask.contextId&&manualRun.body.task.id!==rootTask.id&&!manualRun.body.task.metadata?.error,'owner UI runs same-chat cron despite model tool deny');
  await second.wait("document.querySelector('.schedules [role=status]')?.textContent.includes('Задача принята')",'actual manual schedule admission');
  await second.click('Открыть чат','.schedule button');
  await second.wait("document.querySelector('.thread')?.textContent.includes('Native manual cron completed.')",'actual manual cron model outcome');
  check(await second.evaluate("document.querySelector('.thread').textContent.includes('Native file reads completed.')"),'manual cron reuses original chat and preserves prior native outcome');
  await second.click('Расписания','.nav-item');
  await second.wait("!!document.querySelector('.schedule')&&!document.querySelector('.schedules [role=status]')",'settled schedule after manual completion');
  const deletedSchedule=await ownerAction(second,'/api/schedules/'+schedule.id,'DELETE',()=>second.click('Удалить','.schedule button'));
  check(deletedSchedule.body.deleted===true&&deletedSchedule.body.schedule.id===schedule.id&&dialogs.some(message=>message.startsWith('Удалить расписание?')),'actual confirmed schedule deletion preserves accepted chat task');
  await second.wait("!document.querySelector('.schedule')&&document.querySelector('.schedules [role=status]')?.textContent.includes('Чат, файлы')",'actual schedule deletion result');

  // Navigate through the UI confirmation to discard the deliberately rejected draft.
  await page.click('Новый чат');
  await page.wait("!!document.querySelector('.welcome')",'confirmed draft discard before cleanup');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await page.wait("document.querySelector('.thread')?.textContent.includes('Native manual cron completed.')",'both native and manual roots in original persisted chat');
  await waitFor(()=>history.at(-1)?.some(item=>item.text==='Native manual cron completed.'),'actual persisted manual cron history');
  const preservedHistory=history.at(-1);
  const previewPath='/api/chats/'+encodeURIComponent(rootTask.contextId)+'/files';
  const initialPreview=await ownerAction(page,previewPath,'GET',async()=>{await page.click('Файлы');await page.evaluate("document.querySelector('.file-cleanup').open=true");});
  await page.wait("document.querySelectorAll('.workspace-file-name').length===3",'idle actual workspace before cleanup');
  check(!new URLSearchParams(initialPreview.query).has('older_than_days')&&initialPreview.body.active===false&&await page.evaluate("document.querySelector('#workspace-age').value===''") ,'idle workspace preview applies no default age filter');
  await page.field('#workspace-age','0');
  const agePreview=await ownerAction(page,previewPath,'GET',()=>page.click('Применить','.workspace-file-filters button'));
  check(new URLSearchParams(agePreview.query).get('older_than_days')==='0'&&agePreview.body.files.length===3&&agePreview.body.cleanup_block_reason===null,'actual explicit age filter previews the same eligible files');
  const chosen=entries[1];
  await page.evaluate(`(()=>{const choice=[...document.querySelectorAll('.workspace-file-choice input')].find(input=>input.getAttribute('aria-label')===${JSON.stringify('Выбрать файл '+chosen.relative_path)});choice.click();})()`);
  await page.wait("!!document.querySelector('.workspace-cleanup-actions .danger:not(:disabled)')",'idle chosen file deletion');
  await page.click('Удалить выбранные…','.workspace-cleanup-actions button');
  await page.wait("!!document.querySelector('.workspace-cleanup-confirm')",'actual selected-file confirmation');
  check(await page.evaluate(`document.querySelectorAll('.workspace-cleanup-confirm li').length===1&&document.querySelector('.workspace-cleanup-confirm .workspace-file-path').textContent===${JSON.stringify(chosen.relative_path)}`),'workspace confirmation lists exactly the chosen actual path');
  const cleanup=await ownerAction(page,previewPath+'/delete','POST',()=>page.click('Подтвердить удаление','.workspace-cleanup-confirm button'));
  check(cleanup.request.files.length===1&&cleanup.request.files[0].path===chosen.relative_path&&cleanup.body.state==='completed'&&cleanup.body.results.length===1&&cleanup.body.results[0].path===chosen.relative_path&&cleanup.body.results[0].status==='deleted'&&cleanup.body.totals.deleted===1&&cleanup.body.totals.skipped===0&&cleanup.body.totals.errors===0&&cleanup.body.totals.deleted_bytes===chosen.size_bytes,'confirmed workspace cleanup deletes only the selected actual file');
  await page.wait("document.querySelectorAll('.workspace-file-name').length===2&&document.querySelector('.workspace-cleanup-result')?.textContent.includes('Удалено: 1')",'actual cleanup receipt and refreshed remaining files');
  const remaining=apiResponses.findLast(response=>response.path===previewPath&&response.method==='GET').body.files;
  check(remaining.map(file=>file.path).sort().join(',')===entries.filter(entry=>entry!==chosen).map(entry=>entry.relative_path).sort().join(',')&&await page.evaluate(`document.querySelector('.workspace-cleanup-result').textContent.includes(${JSON.stringify(chosen.relative_path)})`),'cleanup UI shows chosen deletion result and keeps every unselected actual file');
  const deletedPath=previewPath+'/content?'+new URLSearchParams({path:chosen.relative_path});
  check(await page.evaluate(`(async()=>{const response=await fetch(${JSON.stringify(deletedPath)},{headers:{Authorization:${JSON.stringify(bearer)}},cache:'no-store',credentials:'omit'});return response.status===404;})()`),'deleted workspace path is actually absent at authenticated download');
  await page.call('Page.reload');
  await page.wait("!!document.querySelector('.chat-link')",'original chat retained after actual schedule and workspace deletion');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await page.wait("document.querySelector('.thread')?.textContent.includes('Native manual cron completed.')&&document.querySelectorAll('.message .message-attachments:not(.response-files) li').length===3&&document.querySelectorAll('.response-files li').length===2",'full immutable file history after cleanup reload');
  await waitFor(()=>history.at(-1)?.some(item=>item.text==='Native manual cron completed.'),'actual history response after cleanup reload');
  const retained=history.at(-1);
  check(retained.length===preservedHistory.length&&retained.every((item,index)=>JSON.stringify(item)===JSON.stringify(preservedHistory[index]))&&retained.some(item=>item.text?.startsWith('Native file reads completed.'))&&retained.some(item=>item.text==='Native manual cron completed.'),'browser reload preserves both completed roots and immutable file history after cleanup');
  await setLimit(25000000);
  await second.click('Инструменты','.nav-item');
  await second.wait("document.querySelectorAll('form.policy:not([hidden])').length===7",'owner-question policy catalog');
  await setPolicy('core_ask_owner','allow',true);
  await setPolicy('core_task_start','allow',true);
  await setPolicy('core_task_wait','allow',true);
  await setPolicy('core_response_files','allow',true);
  await setPolicy('core_terminal_exec','allow',true);
  await setPolicy('core_python_exec','allow',true);
  await page.field('.composer textarea','Browser guardrail review proof');
  await page.click('Отправить ↑');
  await page.wait("[...document.querySelectorAll('.interaction:not(.resolved) h3')].some(h=>h.textContent==='Проверка материала')",'actual suspicious input owner guardrail request');
  await page.click('Отклонить','.interaction:not(.resolved) button');
  await page.wait("!!document.querySelector('.interaction:not(.resolved) textarea')",'durable owner clarification after withheld input');
  check(await page.evaluate("![...document.querySelectorAll('.interaction h3')].some(h=>h.textContent==='Проверка материала') && document.querySelector('.pending-interaction-bar').textContent.includes('нужен ваш ответ')"),'resolved guardrail stays hidden while active owner question remains visible');
  await page.field('.interaction:not(.resolved) textarea','Заказ 42');
  await page.click('Отправить ответ','.interaction:not(.resolved) button');
  await page.wait("document.querySelector('.thread').textContent.includes('Owner clarification received;')",'native known-failure response after owner answer');
  check(await page.evaluate("[...document.querySelectorAll('.action-error')].some(card=>card.textContent.includes('кодом 3')) && [...document.querySelectorAll('.interaction.resolved')].some(card=>card.textContent.includes('Заказ 42'))"),'known command failure remains visible and answered owner question is retained');
  check(await page.evaluate("(()=>{const card=[...document.querySelectorAll('.action-error')].find(card=>card.textContent.includes('Ожидание задачи'));return card && card.textContent.includes('Ошибка') && !card.closest('.execution-group');})()"),'actual failed background task stays visible despite successful status read');
  await waitFor(()=>history.at(-1)?.some(item=>item.kind==='placeholder'&&item.status==='available'&&!item.review&&!item.outcome&&!item.attachments?.length&&!item.response_files?.length),'actual preserved internal history markers');
  const internalMarkers=history.at(-1).filter(item=>item.kind==='placeholder'&&item.status==='available'&&!item.review&&!item.outcome&&!item.attachments?.length&&!item.response_files?.length).map(item=>item.id);
  await page.wait(`(${JSON.stringify(internalMarkers)}).every(id=>!document.getElementById('history-'+id))`,'internal markers disappear only from presentation');
  check(await page.evaluate("[...document.querySelectorAll('.history-status')].some(node=>node.textContent.includes('Материал отклонён'))&&!document.querySelector('.thread').textContent.includes('Служебная запись агента')"),'stored runtime markers stay hidden while rejected input and task errors remain visible');
  const previewCards='.thread .history-run:last-child .response-files';
  await page.wait("document.querySelectorAll('.thread .history-run:last-child .response-files li').length===6",'native selected preview proof files');
  for(const position of [1,2]){
    await page.click('Открыть',previewCards+' li:nth-child('+position+') button');
    check(await page.evaluate("[...document.querySelectorAll('.file-preview')].some(preview=>preview.textContent.includes('Предпросмотр этого формата недоступен'))"),'unsupported active file format uses safe download fallback '+position);
    await page.click('Закрыть',previewCards+' li:nth-child('+position+') .file-preview button');
  }
  await page.click('Открыть',previewCards+' li:nth-child(3) button');
  await page.wait("document.querySelector('.thread .history-run:last-child .file-preview pre')?.textContent.includes('<script>window.__fileExecuted=true</script>')",'authenticated dangerous text is displayed literally');
  check(await page.evaluate("window.__fileExecuted!==true && !document.querySelector('.file-preview script,.file-preview img,.file-preview iframe')")&&untrustedRequests.length===0,'file preview neither executes supplied markup nor loads external content');
  await page.click('Закрыть',previewCards+' li:nth-child(3) .file-preview button');
  await page.click('Открыть',previewCards+' li:nth-child(4) button');
  await page.wait("document.querySelector('.file-preview .markdown h1')?.textContent==='File preview proof' && !!document.querySelector('.file-preview .markdown table') && !!document.querySelector('.file-preview .hljs-keyword') && document.querySelector('.file-preview .diagram img')?.naturalWidth>0",'actual Markdown file preview renders table Python and Mermaid');
  check(await page.evaluate("window.__fileExecuted!==true && !document.querySelector('.file-preview script,.file-preview iframe,.file-preview img:not([src^=\"blob:\"])')")&&untrustedRequests.length===0,'Markdown file preview preserves safe rendering without external fetches');
  await page.click('Закрыть',previewCards+' li:nth-child(4) .file-preview button');
  await page.click('Открыть',previewCards+' li:nth-child(5) button');
  await page.wait("!!document.querySelector('.file-preview .hljs-keyword')",'Python preview authenticated read is complete');
  check(await page.evaluate(`document.querySelector('.file-preview code')?.textContent===${JSON.stringify("value = 7\nif value:\n    print('python highlight verified')\n")} && !!document.querySelector('.file-preview .hljs-keyword') && getComputedStyle(document.querySelector('.file-preview pre')).whiteSpace==='pre'`),'Python file preview preserves exact indentation with syntax highlighting');
  check(await page.evaluate("!!document.querySelector('.action-card .action-preview .hljs-keyword')"),'Python action parameters have syntax highlighting');
  await page.click('Закрыть',previewCards+' li:nth-child(5) .file-preview button');
  await page.click('Открыть',previewCards+' li:nth-child(6) button');
  await page.wait("document.querySelector('.file-preview pre')?.textContent.includes('preview line')",'long native file preview');
  async function boundedPreview(){
    check(await page.evaluate("(()=>{const box=document.querySelector('.file-preview'),body=box.querySelector('.file-preview-content'),properties=box.parentElement.querySelector('.file-properties');const b=box.getBoundingClientRect();return b.height<=281&&body.scrollHeight>body.clientHeight&&['auto','scroll'].includes(getComputedStyle(body).overflowY)&&properties.getBoundingClientRect().top>=b.bottom-1;})()"),'long file preview scrolls inside its bounds without covering properties');
    await page.call('Page.bringToFront');
    await page.evaluate("document.querySelector('.file-preview-content').focus()");
    await page.wait("document.hasFocus() && document.activeElement===document.querySelector('.file-preview-content')",'long preview receives actual keyboard focus');
    const before=await page.evaluate("document.querySelector('.file-preview-content').scrollTop");
    await page.call('Input.dispatchKeyEvent',{type:'keyDown',key:'PageDown',code:'PageDown',windowsVirtualKeyCode:34});
    await page.call('Input.dispatchKeyEvent',{type:'keyUp',key:'PageDown',code:'PageDown',windowsVirtualKeyCode:34});
    await page.wait(`document.querySelector('.file-preview-content').scrollTop>${before}`,'keyboard scrolls actual long preview');
  }
  await boundedPreview();
  await page.call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await boundedPreview();
  await page.call('Emulation.clearDeviceMetricsOverride');
  await page.click('Закрыть',previewCards+' li:nth-child(6) .file-preview button');
  await second.click('Доступ к агенту','.nav-item');
  await second.wait("document.querySelector('h1')?.textContent==='Доступ к агенту' && document.querySelector('.page [role=status]')?.textContent.includes('Внешних учёток:')",'external access page uses current owner session');
  await second.click('Выдать доступ');
  await second.wait("document.querySelector('.access-dialog')?.open",'native accessible access modal');
  await second.field('.access-dialog input[maxlength="100"]','Browser external access');
  await second.field('.access-dialog input[type=number]','30');
  const issuedAccess=await ownerAction(second,'/api/external-access','POST',()=>second.click('Выдать доступ','.access-dialog button'));
  check(issuedAccess.status===201 && issuedAccess.body.expires_in===30*86400,'actual owner-session Keycloak issues a 30-day external token');
  let externalToken=issuedAccess.body.access_token;
  credentials.add(externalToken);
  await second.wait("!!document.querySelector('.access-token') && document.querySelector('.access-row h3')?.textContent==='Browser external access'",'token shown once with persisted account metadata');
  check(await second.evaluate(`document.querySelector('.access-token').value===${JSON.stringify(externalToken)}`),'one-time token matches real issuance');
  await second.call('Browser.grantPermissions',{origin:config.origin,permissions:['clipboardReadWrite','clipboardSanitizedWrite']});
  await second.call('Page.bringToFront');
  await second.click('Скопировать токен','.access-dialog button');
  await second.wait("document.querySelector('.access-dialog .access-actions button')?.textContent==='Скопировано'",'copy token feedback');
  await second.click('Готово','.access-dialog button');
  await second.wait("!document.querySelector('.access-dialog')",'closing modal removes credential from DOM');
  const accessList=await second.evaluate(`(async()=>{const response=await fetch('/api/external-access',{headers:{Authorization:${JSON.stringify(bearer)}},credentials:'omit',cache:'no-store'});return {body:await response.text(),cache:response.headers.get('cache-control')};})()`);
  check(accessList.cache==='no-store' && !accessList.body.includes(externalToken),'access listing contains no issued token');
  await second.click('Выдать новый токен');
  await second.wait("document.querySelector('.access-dialog')?.open && !document.querySelector('.access-token')",'reopening cannot recover previous token');
  await second.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await second.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await second.wait("!document.querySelector('.access-dialog')",'Escape discards access modal');
  let holdAccessList=false,heldAccessList,loseAccessDelete=false,nativeAccessDeletes=0;
  second.on('Fetch.requestPaused',async event=>{
    if(event.request.method==='DELETE'){
      nativeAccessDeletes++;
      if(loseAccessDelete){loseAccessDelete=false;await second.call('Fetch.failRequest',{requestId:event.requestId,errorReason:'Failed'});return;}
    }
    if(holdAccessList&&event.request.method==='GET'&&event.responseStatusCode===200){holdAccessList=false;heldAccessList=event;return;}
    await second.call('Fetch.continueRequest',{requestId:event.requestId});
  });
  await second.call('Fetch.enable',{patterns:[{urlPattern:config.origin+'/api/external-access*',requestStage:'Response'}]});
  await second.click('Выдать новый токен');
  holdAccessList=true;
  const renewedAccess=await ownerAction(second,'/api/external-access/'+encodeURIComponent(issuedAccess.body.account.id)+'/token','POST',()=>second.click('Выдать новый токен','.access-dialog button'));
  externalToken=renewedAccess.body.access_token;credentials.add(externalToken);
  await waitFor(()=>!!heldAccessList,'actual pre-deletion account listing response held');
  await second.click('Готово','.access-dialog button');
  const accessExternal=await second.evaluate(`(async()=>{const response=await fetch('/a2a/external/.well-known/agent-card.json',{headers:{Authorization:'Bearer '+${JSON.stringify(externalToken)},'A2A-Version':'1.0'},cache:'no-store',credentials:'omit'});return response.status;})()`);
  check(accessExternal===200,'issued credential authenticates external A2A entrance');
  const deleteAccessPath='/api/external-access/'+encodeURIComponent(issuedAccess.body.account.id)+'/account';
  const deletesBefore=apiResponses.filter(response=>response.path===deleteAccessPath&&response.method==='DELETE').length;
  rejectNextAccessDelete=true;
  await second.click('Удалить','.access-row button');
  await second.wait("document.querySelector('.access-row h3')?.textContent==='Browser external access' && !document.querySelector('.access-row button').disabled",'cancelled deletion keeps external account');
  check(!rejectNextAccessDelete&&apiResponses.filter(response=>response.path===deleteAccessPath&&response.method==='DELETE').length===deletesBefore,'cancelled external account deletion sends no mutation');
  const deletedAccess=await ownerAction(second,deleteAccessPath,'DELETE',()=>second.click('Удалить','.access-row button'));
  check(deletedAccess.body.deleted===true&&deletedAccess.body.account_id===issuedAccess.body.account.id&&dialogs.at(-1).includes('новая учётка не получит к ним доступ'),'confirmed external account deletion returns native receipt');
  await second.wait("!document.querySelector('.access-row') && document.querySelector('.page [role=status]')?.textContent==='Внешних учёток: 0'",'deleted external account disappears from list and count');
  const accessReadsBefore=apiResponses.filter(response=>response.path==='/api/external-access'&&response.method==='GET').length;
  await second.call('Fetch.continueRequest',{requestId:heldAccessList.requestId});
  await waitFor(()=>apiResponses.filter(response=>response.path==='/api/external-access'&&response.method==='GET').length>accessReadsBefore,'actual stale listing delivered after deletion');
  check(await second.evaluate("!document.querySelector('.access-row') && document.querySelector('.page [role=status]')?.textContent==='Внешних учёток: 0'"),'late account listing cannot restore a deleted row or count');
  const deletedExternal=await second.evaluate(`(async()=>{const response=await fetch('/a2a/external/.well-known/agent-card.json',{headers:{Authorization:'Bearer '+${JSON.stringify(externalToken)},'A2A-Version':'1.0'},cache:'no-store',credentials:'omit'});return response.status;})()`);
  check(deletedExternal===401,'deleted native account token no longer authenticates external A2A');
  await second.click('Выдать доступ');
  await second.field('.access-dialog input[maxlength="100"]','Lost deletion receipt');
  const uncertainAccess=await ownerAction(second,'/api/external-access','POST',()=>second.click('Выдать доступ','.access-dialog button'));
  credentials.add(uncertainAccess.body.access_token);
  await second.wait("document.querySelector('.access-row h3')?.textContent==='Lost deletion receipt'",'actual account for lost deletion response');
  await second.click('Готово','.access-dialog button');
  const deletesBeforeLoss=nativeAccessDeletes;loseAccessDelete=true;
  await second.click('Удалить','.access-row button');
  await second.wait("document.querySelector('.page .error')?.textContent.includes('Не удалось подтвердить удаление учётки') && !document.querySelector('.access-row') && document.querySelector('.page [role=status]')?.textContent==='Внешних учёток: 0'",'uncertain deletion rereads actual Keycloak list');
  check(!loseAccessDelete&&nativeAccessDeletes===deletesBeforeLoss+1,'unconfirmed deletion rereads canonical account list without replay');
  await second.call('Fetch.disable');
  check(firstTokens.size===3&&secondTokens.size===3,'actual access, refresh and ID tokens captured for both owner tabs');
  const privateValues=[...credentials,peerSecret,'Native immutable output\n',...posts.flatMap(post=>post.parts.map(part=>part.raw).filter(Boolean)),...tasks.flatMap(task=>(task.artifacts??[]).flatMap(artifact=>(artifact.parts??[]).map(part=>part.raw).filter(Boolean)))];
  const storageChecks=await Promise.all([page,second].map(tab=>tab.evaluate(`(()=>{const privateValues=${JSON.stringify(privateValues)};return [localStorage,sessionStorage].every(store=>Array.from({length:store.length},(_,index)=>store.getItem(store.key(index))).every(value=>privateValues.every(secret=>!value.includes(secret))));})()`)));
  check(storageChecks.every(Boolean),'both owner tabs store no observed auth tokens, peer secrets or raw file bytes in local/session storage');
  await page.call('Page.bringToFront');
  const screenshot=await page.call('Page.captureScreenshot',{format:'png'});await writeFile(config.evidence+'/actual-browser.png',Buffer.from(screenshot.data,'base64'));
  const finalTask=tasks.at(-1);
  await page.evaluate("document.querySelector('.chat-menu').open=true");
  await page.click('Удалить чат');
  await page.wait("document.querySelector('.chat-delete-dialog')?.open",'native accessible delete confirmation');
  await page.click('Отмена','.chat-delete-dialog button');
  await page.wait("!document.querySelector('.chat-delete-dialog').open && document.activeElement===document.querySelector('.chat-menu > summary')",'cancel returns focus to visible chat menu');
  check(await page.evaluate("!!document.querySelector('.chat-link')"),'cancelled chat deletion keeps shared chat');
  await page.evaluate("document.querySelector('.chat-menu').open=true");
  await page.click('Удалить чат');
  await page.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await page.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await page.wait("!document.querySelector('.chat-delete-dialog').open && document.activeElement===document.querySelector('.chat-menu > summary')",'Escape returns focus without deleting chat');
  await page.evaluate("document.querySelector('.chat-menu').open=true");
  await page.click('Удалить чат');
  const archivePath='/api/chats/'+encodeURIComponent(rootTask.contextId);
  const archive=await ownerAction(page,archivePath,'DELETE',()=>page.click('Подтвердить удаление чата','.chat-delete-dialog button'));
  check(archive.body.archived===true&&archive.body.context_id===rootTask.contextId,'actual confirmed chat deletion returns durable archive receipt');
  await page.wait("document.querySelectorAll('.chat-link').length===0 && !document.querySelector('.thread .history-run')",'deleted chat closes into empty composer');
  await second.call('Page.reload');
  await second.wait("!!document.querySelector('.composer textarea') && document.querySelectorAll('.chat-link').length===0",'deleted shared chat is absent for second owner tab');
  const archivedReads=await page.evaluate(`(async()=>{const headers={Authorization:${JSON.stringify(bearer)},'A2A-Version':'1.0'},paths=${JSON.stringify(['/a2a/owner/tasks/'+finalTask.id,'/api/chats/'+rootTask.contextId+'/history?limit=50',outputRequests[0].path+outputRequests[0].query])};const results=[];for(const path of paths){const response=await fetch(path,{headers,cache:'no-store',credentials:'omit'});const error=response.status===200?undefined:(await response.json()).error?.code;results.push({status:response.status,error});}return results;})()`);
  check(archivedReads.every(result=>result.status===200),'archived chat retains authenticated canonical Task history and issued file access'+(archivedReads.some(result=>result.status!==200)?': '+JSON.stringify(archivedReads):''));
  await second.click('Расписания','.nav-item');
  await second.wait("!!document.querySelector('.schedules')&&!document.querySelector('.schedules [role=status]')",'schedule screen after shared archive');
  await second.click('Создать расписание ＋');
  await second.field('.schedules textarea','Empty scheduled chat archive proof');
  await second.field('.schedules .form-grid input','0 0 1 1 *');
  const emptySchedule=await ownerAction(second,'/api/schedules','POST',()=>second.click('Сохранить','.schedules form button'));
  await second.wait("!!document.querySelector('.chat-link')",'actual empty scheduled chat appears');
  await second.evaluate("document.querySelector('.chat-link').click()");
  await second.wait("!!document.querySelector('.composer textarea') && !!document.querySelector('.chat-menu')",'empty chat with no root Task');
  await second.evaluate("document.querySelector('.chat-menu').open=true");
  await second.click('Удалить чат');
  await ownerAction(second,'/api/chats/'+encodeURIComponent(emptySchedule.body.schedule.context_id),'DELETE',()=>second.click('Подтвердить удаление чата','.chat-delete-dialog button'));
  const archivedSchedules=await ownerAction(second,'/api/schedules','GET',()=>second.click('Расписания','.nav-item'));
  check(archivedSchedules.body.schedules.find(row=>row.id===emptySchedule.body.schedule.id)?.enabled===false,'deleting an empty scheduled chat atomically disables its schedule');
  await page.call('Page.bringToFront');
  await page.field('.composer textarea','Native public reply streaming proof');
  await page.click('Отправить ↑');
  await page.wait("document.querySelector('.live-answer .prose')?.textContent===\"# Native streamed reply\\n\\n```python\\nprint('live\"",'actual provider prefix arrives before complete response');
  const providerBlocked=`(async()=>{const response=await fetch('/api/browser-reply-fixture',{headers:{Authorization:${JSON.stringify(bearer)}},cache:'no-store',credentials:'omit'});const state=await response.json();return response.status===200&&state.started&&state.waiting&&!state.finished;})()`;
  check(await page.evaluate(providerBlocked),'public reply is visible while the actual provider is blocked');
  check(await page.evaluate("!document.querySelector('.live-answer code,.live-answer .diagram')&&!document.querySelector('.thread').textContent.includes('private-native-reasoning')"),'incomplete streamed Markdown stays safe plaintext and excludes reasoning');
  await page.call('Page.reload');
  await page.wait("!!document.querySelector('.chat-link')",'streaming chat retained after reload');
  await page.evaluate("document.querySelector('.chat-link').click()");
  await page.wait("document.querySelector('.chat-link')?.getAttribute('aria-current')==='page' && ['Запрос принят','Готовит ответ'].includes(document.querySelector('.task-status')?.textContent)",'reopened chat restores canonical state');
  check(await page.evaluate(providerBlocked)&&await page.evaluate("!document.querySelector('.live-answer')&&!document.querySelector('.thread').textContent.includes('Native streamed reply')"),'page reload uses canonical history without restoring the transient prefix');
  check(await page.evaluate(`(async()=>{const response=await fetch('/api/browser-reply-fixture',{method:'POST',headers:{Authorization:${JSON.stringify(bearer)}},credentials:'omit'});return response.status===200;})()`),'authenticated native fixture releases the actual provider');
  await page.wait("document.querySelector('.thread .markdown h1')?.textContent==='Native streamed reply'&&!document.querySelector('.live-answer')",'stored final reply replaces provisional text');
  check(await page.evaluate("document.querySelectorAll('.thread .markdown h1').length===1&&document.querySelector('.thread .markdown .hljs-built_in')?.textContent==='print'&&!document.querySelector('.thread').textContent.includes('private-native-reasoning')"),'canonical final reply replaces preview once with safe Markdown and Python');
  const revokedBearer=bearer;
  const exitRequests=[];
  page.on('Network.requestWillBeSent',({request})=>{
    const url=new URL(request.url);
    exitRequests.push({path:url.pathname,logoutHint:url.pathname.endsWith('/logout')&&url.searchParams.has('id_token_hint')});
  });
  await page.click('Выйти');
  await page.wait("location.pathname==='/ui/'&&new URLSearchParams(location.search).get('logged_out')==='1'&&document.querySelector('h1')?.textContent==='Вы вышли из аккаунта'",'intentional logged-out screen');
  check(exitRequests.some(request=>request.logoutHint),'real logout preserves ID token hint until Keycloak logout URL is built');
  check(await page.evaluate(`(async()=>{const response=await fetch('/api/identity',{headers:{Authorization:${JSON.stringify(revokedBearer)}},cache:'no-store',credentials:'omit'});return response.status===401;})()`),'Keycloak logout revokes the old owner token for a new HTTP request');
  exitRequests.length=0;
  await page.call('Page.reload');
  await page.wait("document.querySelector('h1')?.textContent==='Вы вышли из аккаунта' && !document.querySelector('.brand,.brand-mark')",'logged-out screen after reload');
  await delay(500);
  check(!exitRequests.some(request=>request.path.startsWith('/api/')||request.path.endsWith('/protocol/openid-connect/auth'))&&await page.evaluate("!document.querySelector('.sidebar,.composer')"),'logged-out reload exposes no private UI and performs no automatic login or private requests');
  await page.click('Войти');
  await page.wait("!!document.querySelector('input[name=username]')",'fresh login form after completed SSO logout');
  check(exitRequests.some(request=>request.path.endsWith('/protocol/openid-connect/auth')),'explicit sign-in after logout opens real Keycloak login');
} catch(error){
  const screenshot=await page.call('Page.captureScreenshot',{format:'png'}).catch(()=>null);if(screenshot)await writeFile(config.evidence+'/failed-browser.png',Buffer.from(screenshot.data,'base64'));
  const ui=await page.evaluate("({focused:document.hasFocus(),active:document.activeElement?.className,preview:[...document.querySelectorAll('.file-preview-content')].map(p=>({scrollTop:p.scrollTop,scrollHeight:p.scrollHeight,clientHeight:p.clientHeight,rect:p.getBoundingClientRect().toJSON()})),thread:(()=>{const p=document.querySelector('.thread');return p&&{scrollTop:p.scrollTop,scrollHeight:p.scrollHeight,clientHeight:p.clientHeight};})(),actions:[...document.querySelectorAll('.action-card')].map(p=>({title:p.querySelector('.action-heading')?.textContent,reason:[...p.querySelectorAll(':scope > p')].map(p=>p.textContent)})),status:document.querySelector('.task-status')?.textContent})").catch(()=>null);if(ui)await writeFile(config.evidence+'/failed-state.json',JSON.stringify(ui,null,2));
  console.error(error.message);process.exitCode=1;
} finally {second?.close();page.close();}
