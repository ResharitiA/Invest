const style = document.createElement('style');
style.textContent = `
.helper{position:fixed;right:20px;bottom:16px;z-index:20}
.helper-toggle{background:#182943;border:1px solid #60a5fa;border-radius:24px;color:white;cursor:pointer;width:150px;height:190px;padding:0}
.helper-toggle canvas{width:150px;height:155px;display:block}
.helper-chat{width:min(390px,calc(100vw - 32px));background:#111b2d;border:1px solid #3b82f6;border-radius:16px;padding:18px;margin-bottom:10px;box-shadow:0 12px 40px #0008}
.helper-chat[hidden]{display:none}.helper-chat header{display:flex;justify-content:space-between}
.helper-output{white-space:pre-wrap;max-height:310px;overflow:auto;overflow-wrap:anywhere;font-size:14px;line-height:1.5;margin:12px 0}
.helper-message{background:#182943;border-radius:10px;padding:10px;margin:10px 0}.helper-message strong{display:block;color:#93c5fd;margin-bottom:6px}.helper-question{background:#223b57;margin-left:24px}
.helper-chat input,.helper-chat select{width:100%;background:#0b1220;color:#e8eef8;border:1px solid #526783;padding:10px;border-radius:7px;margin:6px 0}
.helper-chat button{cursor:pointer}.helper-chat .btn{margin-top:6px}
`;
document.head.append(style);
const root = document.createElement('aside');
root.className = 'helper';
root.innerHTML = '<div class="helper-chat" hidden><header><b>Скрепка · помощник Garden</b><button type="button" aria-label="Закрыть">×</button></header><p class="muted">Объясню находки и помогу выбрать, что исправить первым.</p><select aria-label="Запуск аудита"></select><div class="helper-output" role="status" aria-live="polite"></div><form><input aria-label="Вопрос скрепке" maxlength="2000" placeholder="Что исправить в первую очередь?" required><button class="btn">Спросить</button></form><small class="muted">AI может ошибаться. Проверяйте рекомендации по коду.</small></div><button class="helper-toggle" type="button" aria-label="Открыть помощника Скрепку" aria-expanded="false"><canvas></canvas><span>Спросить Скрепку</span></button>';
document.body.append(root);
const panel=root.querySelector('.helper-chat'), toggle=root.querySelector('.helper-toggle');
const output=root.querySelector('.helper-output'), select=root.querySelector('select');
let runs=[],busy=false,canAsk=false;
const notice=document.createElement('div');notice.className='muted';notice.setAttribute('role','status');output.after(notice);
output.setAttribute('role','log');output.setAttribute('aria-label','История переписки');
function format(a){
 const findings=runs.find(r=>r.id===select.value)?.findings||[];
 const label=f=>(f.path||'Файл не указан')+(f.line?':'+f.line:'')+' ('+f.category+')';
 const readable=value=>{let text=String(value||'');for(const f of findings)text=text.split(f.id).join(label(f));return text};
 return readable(a.summary)+(a.risk_level?'\n\nРиск: '+a.risk_level:'')+((a.recommendations||[]).length?'\n\n'+a.recommendations.map(r=>{
  const finding=findings.find(f=>f.id===r.finding_id);
  return (finding?label(finding):'Находка отсутствует в выбранном запуске')+'\n'+readable(r.explanation)+'\nРекомендация: '+readable(r.fix);
 }).join('\n\n'):'');
}
function appendMessage(label,text,user=false){
 const message=document.createElement('div');message.className='helper-message'+(user?' helper-question':'');
 const title=document.createElement('strong');title.textContent=label;
 const body=document.createElement('div');body.textContent=text;message.append(title,body);output.append(message);
 output.scrollTop=output.scrollHeight;return message;
}
async function refresh(){
 if(busy)return;
 try{
  const selected=select.value;
  const [status,list]=await Promise.all([api('/api/assistant'),api('/api/runs')]);
  runs=list.filter(r=>r.status!=='running');
  select.replaceChildren(...runs.map(r=>{const o=document.createElement('option');o.value=r.id;o.textContent=new Date(r.started*1000).toLocaleString()+' · '+r.name;return o}));
  if(runs.some(r=>r.id===selected))select.value=selected;
  canAsk=Boolean(status.configured&&runs.length);
  root.querySelector('form button').disabled=!canAsk;
  notice.textContent=!runs.length?'Сначала запустите аудит.':!status.configured?'AI не настроен. История доступна; для новых вопросов задайте LLM_API_KEY и LLM_MODEL на сервере.':'';
  showSaved();
 }catch(e){notice.textContent='Не удалось загрузить помощника: '+e.message}
}
function showSaved(){
 const r=runs.find(r=>r.id===select.value);output.replaceChildren();
 for(const answer of Object.values(r?.ai||{}))appendMessage('Скрепка · анализ',format(answer));
 for(const entry of r?.assistant||[]){
  appendMessage('Вы',entry.question,true);
  appendMessage('Скрепка',format(entry.answer));
 }
 if(!output.childElementCount)output.textContent=r?'Задайте вопрос о выбранном запуске.':'Нет переписки.';
}
toggle.onclick=()=>{panel.hidden=!panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)refresh()};
root.querySelector('header button').onclick=()=>{panel.hidden=true;toggle.setAttribute('aria-expanded','false')};
select.onchange=()=>{notice.textContent='';showSaved()};
root.querySelector('form').onsubmit=async e=>{
 e.preventDefault();if(busy||!canAsk)return;
 const input=root.querySelector('input'),button=root.querySelector('form button');
 const question=input.value.trim(),run=runs.find(r=>r.id===select.value);
 if(!question||!run)return;
 input.value='';
 busy=true;button.disabled=true;select.disabled=true;input.disabled=true;
 showSaved();appendMessage('Вы',question,true);notice.textContent='Разбираю результаты…';
 try{
  const answer=await api('/api/assistant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:run.id,question})});
  (run.assistant??=[]).push({question,answer});
  showSaved();input.value='';notice.textContent='';
 }catch(e){notice.textContent='Не удалось получить ответ: '+e.message+'. Отправленный вопрос виден в переписке.'}
 finally{busy=false;button.disabled=!canAsk;select.disabled=false;input.disabled=false;if(!panel.hidden)input.focus()}
};
async function model(){
 try{
  const THREE=await import('https://esm.sh/three@0.170.0');
  const {OBJLoader}=await import('https://esm.sh/three@0.170.0/examples/jsm/loaders/OBJLoader.js');
  const {MTLLoader}=await import('https://esm.sh/three@0.170.0/examples/jsm/loaders/MTLLoader.js');
  const renderer=new THREE.WebGLRenderer({canvas:root.querySelector('canvas'),alpha:true,antialias:true});
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(150,155,false);
  const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(35,150/155,.01,100);
  scene.add(new THREE.HemisphereLight(0xffffff,0x486080,3));
  const light=new THREE.DirectionalLight(0xffffff,4);light.position.set(3,4,5);scene.add(light);
  const materials=await new MTLLoader().loadAsync('/assets/Helper1.mtl');materials.preload();
  const object=await new OBJLoader().setMaterials(materials).loadAsync('/assets/Helper1.obj');
  const box=new THREE.Box3().setFromObject(object),size=box.getSize(new THREE.Vector3()),center=box.getCenter(new THREE.Vector3());
  const scale=2/Math.max(size.x,size.y,size.z);
  object.position.copy(center).multiplyScalar(-scale);object.scale.setScalar(scale);
  const pivot=new THREE.Group();pivot.add(object);scene.add(pivot);camera.position.set(0,0,4.5);
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  renderer.setAnimationLoop(t=>{if(!reduced.matches){pivot.rotation.y=Math.sin(t*.0005)*.16;pivot.position.y=Math.sin(t*.002)*.04}renderer.render(scene,camera)});
 }catch(e){root.querySelector('canvas').hidden=true;toggle.style.height='60px';toggle.title='3D недоступно; чат продолжает работать';console.warn('Helper1:',e)}
}
model();
