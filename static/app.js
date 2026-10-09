let stream=null, recorder=null, chunks=[], sid=null, score=0, cameraOn=false, radarOn=false, radarSocket=null, eventsLocal=[], lastFace=Date.now(), lastMulti=0, lastHand=0, lastBody=0, lastGaze=0, lastHeadTurn=0, lastObject=0, lastObstruction=0, visionReady=false, objectModel=null, objectBusy=false, userRole='STUDENT', cliCwd='/', liveFrameTimer=null, liveFrameCanvas=document.createElement('canvas'), liveFrameCtx=liveFrameCanvas.getContext('2d'), radarRelayTimer=null, visionBusy=false, visionTimer=null, objectTimer=null, objectCanvas=document.createElement('canvas'), objectCtx=objectCanvas.getContext('2d')
const $=id=>document.getElementById(id); const video=$('video'),canvas=$('overlay'),ctx=canvas.getContext('2d');
const weights={FACE_MISSING:10,MULTIPLE_PERSONS:30,CAMERA_OBSTRUCTED:25,UNEXPECTED_MOVEMENT:10,HAND_DETECTED:5,PERSON_LEFT_FRAME:20};
let cfg={warning_threshold:31,suspicious_threshold:61,critical_threshold:81,score_decay:2,recording_enabled:1,buzzer_enabled:1,landmarks_enabled:1,radar_enabled:1};

(async()=>{try{const m=await (await fetch('/api/me')).json();userRole=m.role||'STUDENT';$('userName').textContent=(userRole==='STUDENT'?'RAIMA':(m.username||'USER')).toUpperCase();document.querySelectorAll('.admin-only').forEach(x=>x.style.display=['ADMIN','SUPER_ADMIN','SUPERVISOR'].includes(userRole)?'':'none');if(['ADMIN','SUPER_ADMIN'].includes(userRole))await loadAdminSettings();await refreshCameras();await loadDevices();}catch(e){toast('Dashboard initialization failed')}})();
function togglePanel(id){const x=$(id);if(x)x.classList.toggle('open')}
function setProjectAccent(){document.documentElement.style.setProperty('--accent','#49ff88');document.documentElement.style.setProperty('--button-color','#49ff88');document.documentElement.style.setProperty('--accent-bg','#062d12')}
setProjectAccent();
async function logout(){await fetch('/api/logout',{method:'POST'});location.href='/'}

async function refreshCameras(){const select=$('cameraSelect');if(!select)return;select.innerHTML='<option>Detecting cameras…</option>';if(!navigator.mediaDevices?.enumerateDevices){select.innerHTML='<option>Camera API unavailable</option>';return}try{const devices=await navigator.mediaDevices.enumerateDevices(),cams=devices.filter(d=>d.kind==='videoinput');select.innerHTML=cams.length?cams.map((d,i)=>`<option value="${escapeHtml(d.deviceId)}">${escapeHtml(d.label||`Camera ${i+1}`)}</option>`).join(''):'<option value="">No camera detected</option>'}catch(e){select.innerHTML='<option>Unable to enumerate cameras</option>'}}
async function toggleCamera(){if(cameraOn){await stopCamera();return}await startCamera()}
async function ensureStudentSession(){if(sid)return true;try{const r=await fetch('/api/sessions',{method:'POST'}),j=await r.json();if(!r.ok)throw new Error(j.error||'Could not start session');sid=j.session_id;$('sSession').textContent=sid.slice(0,8)+'…';return true}catch(e){toast(e.message||'Could not start monitoring session');return false}}
async function startCamera(){
  if(window.__cameraStarting)return;
  window.__cameraStarting=true;
  try{
    if(!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia!=='function') throw new Error('Camera API is not supported by this browser');
    if(stream) await stopCamera(false);
    const select=$('cameraSelect');
    const deviceId=select?.value||'';
    const hd={width:{ideal:1280},height:{ideal:720},frameRate:{ideal:30,max:30}};
    const attempts=[];
    if(deviceId && deviceId!=='No camera detected' && deviceId!=='Detecting cameras…' && deviceId!=='Unable to enumerate cameras') attempts.push({video:{...hd,deviceId:{exact:deviceId}},audio:false});
    attempts.push({video:hd,audio:false},{video:true,audio:false});
    let lastErr=null;
    for(const constraints of attempts){
      try{
        stream=await Promise.race([navigator.mediaDevices.getUserMedia(constraints),new Promise((_,reject)=>setTimeout(()=>reject(new Error('Camera start timed out')),15000))]);
        if(stream?.getVideoTracks?.().length)break;
      }catch(e){lastErr=e;stream=null}
    }
    if(!stream) throw lastErr||new Error('Camera could not be opened');
    video.srcObject=stream; video.muted=true; video.playsInline=true;
    const track=stream.getVideoTracks()[0];
    if(track)track.onended=()=>{
      if(!cameraOn)return;
      cameraOn=false; setBreaker('camBtn',false); $('camState').textContent='OFFLINE'; $('sCam').textContent='OFFLINE'; $('detState').textContent='OFF'; $('camMsg').style.display='grid';
      stopLiveCameraRelay(); if(visionTimer){clearInterval(visionTimer);visionTimer=null} if(objectTimer){clearInterval(objectTimer);objectTimer=null}
    };
    await new Promise((resolve,reject)=>{
      if(video.readyState>=2)return resolve();
      const timer=setTimeout(()=>reject(new Error('Camera video did not become ready')),10000);
      video.addEventListener('loadeddata',()=>{clearTimeout(timer);resolve()},{once:true});
    });
    await video.play();
    cameraOn=true; setBreaker('camBtn',true); $('camState').textContent='ONLINE'; $('sCam').textContent='ONLINE'; $('detState').textContent='ACTIVE'; $('camMsg').style.display='none';
    try{await refreshCameras()}catch(_){}
    // Camera must remain ON even if the session API temporarily fails.
    if(!await ensureStudentSession()){
      toast('Camera is ON, but monitoring session could not be started.');
    }
    if(cfg.recording_enabled && sid){
      try{
        if(!window.MediaRecorder)throw new Error('MediaRecorder is not supported');
        const type=MediaRecorder.isTypeSupported('video/webm;codecs=vp9')?'video/webm;codecs=vp9':'video/webm';
        recorder=new MediaRecorder(stream,{mimeType:type}); recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)}; recorder.start(1000); $('rec').textContent='REC ●'; $('sRec').textContent='ACTIVE';
      }catch(e){toast('Camera is ON; recording unavailable: '+e.message)}
    }
    addNotification('INFO','Camera started','Real browser camera permission granted. The consent-based live student camera is now shared with the authorized admin console.');
    if(sid)startLiveCameraRelay();
    startVision();
  }catch(e){
    toast(e?.name==='NotAllowedError'?'Camera permission was denied. Allow camera access in Chrome and try again.':(e.message||'Camera could not be started'));
    setBreaker('camBtn',false);
    try{await stopCamera(false)}catch(_){}
  }finally{window.__cameraStarting=false}
}

function setBreaker(id,on){const b=$(id);if(!b)return;b.classList.toggle('on',on);b.querySelector('b').textContent=on?'ON':'OFF'}
async function stopCamera(resetSession=true){
  stopLiveCameraRelay();
  if(visionTimer){clearInterval(visionTimer);visionTimer=null}
  if(objectTimer){clearInterval(objectTimer);objectTimer=null}
  if(recorder&&recorder.state!=='inactive'){try{recorder.stop()}catch(_){}}
  recorder=null;
  if(stream){stream.getTracks().forEach(t=>{try{t.onended=null;t.stop()}catch(_){}});stream=null}
  try{video.pause();video.srcObject=null}catch(_){}
  cameraOn=false;setBreaker('camBtn',false);$('camState').textContent='OFFLINE';$('sCam').textContent='OFFLINE';$('detState').textContent='OFF';$('camMsg').style.display='grid';$('rec').textContent='REC OFF';$('sRec').textContent='INACTIVE';
  if(resetSession&&sid)toast('Camera stopped. Use END SESSION / FINALIZE to save evidence.');
}
function startLiveCameraRelay(){
  stopLiveCameraRelay();
  if(userRole!=='STUDENT'||!sid)return;
  liveFrameCanvas.width=360; liveFrameCanvas.height=203;
  liveFrameTimer=setInterval(async()=>{
    if(!cameraOn||!sid||!video.videoWidth)return;
    try{
      liveFrameCtx.drawImage(video,0,0,liveFrameCanvas.width,liveFrameCanvas.height);
      const blob=await new Promise(r=>liveFrameCanvas.toBlob(r,'image/jpeg',.58));
      if(!blob)return;
      const fd=new FormData(); fd.append('session_id',sid); fd.append('frame',blob,'live.jpg'); fd.append('score',String(Math.round(score)));
      await fetch('/api/live-camera/frame',{method:'POST',body:fd,cache:'no-store',credentials:'include'});
    }catch(_){ }
  },100);
}
function stopLiveCameraRelay(){
  if(liveFrameTimer){clearInterval(liveFrameTimer);liveFrameTimer=null;}
  if(userRole==='STUDENT')fetch('/api/live-camera/stop',{method:'POST',keepalive:true}).catch(()=>{});
}

async function endSession(){if(!sid){toast('No active session.');return}try{if(recorder&&recorder.state!=='inactive'){await new Promise(resolve=>{recorder.onstop=resolve;recorder.stop()})}if(chunks.length){const fd=new FormData();fd.append('video',new Blob(chunks,{type:'video/webm'}),'recording.webm');const r=await fetch('/api/sessions/'+sid+'/recording',{method:'POST',body:fd});if(!r.ok)throw new Error('Recording upload failed')}const er=await fetch('/api/sessions/'+sid+'/events?max_score='+encodeURIComponent(Math.round(score)),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(eventsLocal)});if(!er.ok)throw new Error('Event save failed');const r=await fetch('/api/sessions/'+sid+'/finish',{method:'POST'}),j=await r.json();if(!r.ok)throw new Error(j.error||'Finalization failed');addNotification('INFO','Session finalized','Recording, events, report and metadata were saved.');toast('Session completed. Evidence ZIP: '+j.zip);stopCamera(false);sid=null;$('sSession').textContent='NONE';eventsLocal=[];chunks=[];score=0;updateScore()}catch(e){toast('Finalization error: '+e.message)}}
window.addEventListener('beforeunload',e=>{if(userRole==='STUDENT'){navigator.sendBeacon('/api/live-camera/stop');navigator.sendBeacon('/api/live-radar/stop');}if(sid){e.preventDefault();e.returnValue='Session evidence is not finalized.'}});

async function waitForVisionLibraries(timeout=20000){
  const started=Date.now();
  while(Date.now()-started<timeout){
    if(typeof FaceMesh!=='undefined'&&typeof Hands!=='undefined'&&typeof Pose!=='undefined')return true;
    await new Promise(r=>setTimeout(r,200));
  }
  return false;
}
async function startVision(){
  if(visionReady){if(!visionTimer)visionTimer=setInterval(processVisionFrame,120);if(!objectTimer)loadObjectModel();return}
  if(!(await waitForVisionLibraries())){toast('Camera detection libraries are still loading. Please keep the camera ON for a moment and try again.');return}
  const loc=f=>`https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh/${f}`,hl=f=>`https://cdn.jsdelivr.net/npm/@mediapipe/hands/${f}`,pl=f=>`https://cdn.jsdelivr.net/npm/@mediapipe/pose/${f}`;
  window.faceMesh=new FaceMesh({locateFile:loc});faceMesh.setOptions({maxNumFaces:3,refineLandmarks:true,minDetectionConfidence:.5,minTrackingConfidence:.5});
  window.handsModel=new Hands({locateFile:hl});handsModel.setOptions({maxNumHands:2,minDetectionConfidence:.5,minTrackingConfidence:.5});
  window.poseModel=new Pose({locateFile:pl});poseModel.setOptions({modelComplexity:0,minDetectionConfidence:.5,minTrackingConfidence:.5});
  faceMesh.onResults(onFace);handsModel.onResults(onHands);poseModel.onResults(onPose);visionReady=true;
  if(!visionTimer)visionTimer=setInterval(processVisionFrame,120);
  loadObjectModel();
}
async function loadObjectModel(){
  if(objectModel||objectBusy||objectTimer)return;
  const started=Date.now();
  while(!window.cocoSsd&&Date.now()-started<20000)await new Promise(r=>setTimeout(r,200));
  if(!window.cocoSsd)return;
  try{objectBusy=true;objectModel=await cocoSsd.load({base:'lite_mobilenet_v2'});if(cameraOn&&!objectTimer)objectTimer=setInterval(processObjectFrame,700)}catch(_){objectModel=null}finally{objectBusy=false}
}
async function processVisionFrame(){if(!cameraOn||visionBusy||!video.videoWidth)return;visionBusy=true;canvas.width=video.videoWidth||640;canvas.height=video.videoHeight||480;try{await faceMesh.send({image:video});await handsModel.send({image:video});await poseModel.send({image:video});if(Date.now()-lastObstruction>3000){const px=document.createElement('canvas');px.width=32;px.height=18;const pc=px.getContext('2d',{willReadFrequently:true});pc.drawImage(video,0,0,32,18);const d=pc.getImageData(0,0,32,18).data;let sum=0;for(let i=0;i<d.length;i+=4)sum+=(d[i]+d[i+1]+d[i+2])/3;const avg=sum/(d.length/4);if(avg<4){addEvent('CAMERA_OBSTRUCTED','HIGH',.95,'Camera image is almost completely blocked or covered');lastObstruction=Date.now()}}}catch(e){}finally{visionBusy=false}}
async function processFrame(){return processVisionFrame()}
function clear(){ctx.clearRect(0,0,canvas.width,canvas.height)}
function onFace(r){clear();const n=r.multiFaceLandmarks?.length||0;$('faces').textContent=n;$('persons').textContent=n;if(n===0){if(Date.now()-lastFace>1500&&Date.now()-lastObstruction>3000){addEvent('CAMERA_OBSTRUCTED','HIGH',.92,'Face is missing or the camera appears blocked/covered');lastObstruction=Date.now()}}else{lastFace=Date.now();detectFaceAttention(r.multiFaceLandmarks[0])}if(n>1&&Date.now()-lastMulti>2500){addEvent('MULTIPLE_PERSONS','HIGH',.99,`${n} faces visible in frame`);lastMulti=Date.now()}if(cfg.landmarks_enabled&&r.multiFaceLandmarks)r.multiFaceLandmarks.forEach(drawFaceOverlay)}
function detectFaceAttention(l){if(!l||l.length<478)return;const nose=l[1],left=l[33],right=l[263];const span=Math.max(.05,Math.abs(right.x-left.x));const head=(nose.x-((left.x+right.x)/2))/span;const irisL=[468,469,470,471,472].map(i=>l[i]).filter(Boolean),irisR=[473,474,475,476,477].map(i=>l[i]).filter(Boolean);const avg=arr=>arr.reduce((a,p)=>a+p.x,0)/Math.max(1,arr.length);let gazeExtreme=false;if(irisL.length&&irisR.length){const lx=(avg(irisL)-l[33].x)/Math.max(.04,Math.abs(l[133].x-l[33].x));const rx=(avg(irisR)-l[362].x)/Math.max(.04,Math.abs(l[263].x-l[362].x));gazeExtreme=lx<.28||lx>.72||rx<.28||rx>.72}if(Math.abs(head)>.18&&Date.now()-lastHeadTurn>3000){addEvent('HEAD_TURNED','MEDIUM',.88,'Student turned head significantly to the side');lastHeadTurn=Date.now()}if(gazeExtreme&&Date.now()-lastGaze>3000){addEvent('SIDE_GAZE','MEDIUM',.86,'Student eyes are looking noticeably to the side');lastGaze=Date.now()}}
function drawFaceOverlay(l){const connections=window.FACEMESH_TESSELATION||[];ctx.lineWidth=1;ctx.strokeStyle=getComputedStyle(document.body).getPropertyValue('--accent').trim()||'#54e69a';ctx.globalAlpha=.65;for(const pair of connections){const a=l[pair[0]],b=l[pair[1]];if(!a||!b)continue;ctx.beginPath();ctx.moveTo(a.x*canvas.width,a.y*canvas.height);ctx.lineTo(b.x*canvas.width,b.y*canvas.height);ctx.stroke()}ctx.globalAlpha=1;const key=[10,152,234,454,33,263,61,291,13,14];ctx.fillStyle='#fff';for(const i of key){const p=l[i];if(p){ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,2,0,Math.PI*2);ctx.fill()}}}
async function processObjectFrame(){if(!cameraOn||objectBusy||!objectModel||!video.videoWidth)return;objectBusy=true;try{objectCanvas.width=320;objectCanvas.height=180;objectCtx.drawImage(video,0,0,320,180);const preds=await objectModel.detect(objectCanvas);const persons=preds.filter(p=>p.class==='person'&&p.score>.62);$('persons').textContent=Math.max(Number($('faces').textContent)||0,persons.length);if(persons.length>1&&Date.now()-lastMulti>2500){addEvent('MULTIPLE_PERSONS','HIGH',.98,`${persons.length} people detected in the camera frame`);lastMulti=Date.now()}const suspiciousObjects=preds.filter(p=>['cell phone','laptop','book','remote','bottle'].includes(p.class)&&p.score>.62);if(suspiciousObjects.length&&Date.now()-lastObject>4000){addEvent('OBJECT_DETECTED','HIGH',.94,`Suspicious object detected: ${suspiciousObjects[0].class}`);lastObject=Date.now()}}catch(_){}finally{objectBusy=false}}
function onHands(r){const n=r.multiHandLandmarks?.length||0;$('hands').textContent=n;if(n&&Date.now()-lastHand>5000){addEvent('HAND_DETECTED','LOW',.9,`${n} hand(s) detected`);lastHand=Date.now()}if(cfg.landmarks_enabled&&r.multiHandLandmarks)r.multiHandLandmarks.forEach(l=>drawPoints(l,'#7da7ff',1))}
function onPose(r){const present=!!r.poseLandmarks;$('bodyState').textContent=present?'DETECTED':'NOT DETECTED';if(present&&Date.now()-lastBody>8000){addEvent('BODY_DETECTED','LOW',.9,'Body landmarks detected');lastBody=Date.now()}if(cfg.landmarks_enabled&&r.poseLandmarks)drawPoints(r.poseLandmarks,'#ff9fcb',1)}
function drawPoints(list,color,size){ctx.fillStyle=color;for(const p of list){ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,size+1,0,Math.PI*2);ctx.fill()}}
function addEvent(type,severity,confidence,description){if(!sid)return;const pts={LOW:weights.HAND_DETECTED||5,MEDIUM:weights.FACE_MISSING||10,HIGH:weights.MULTIPLE_PERSONS||30,CRITICAL:40}[severity]||5;score=Math.max(0,Math.min(100,score+pts));eventsLocal.push({type,severity,confidence,score_change:pts,description,created_at:new Date().toISOString()});if(eventsLocal.length>500)eventsLocal.shift();const line=document.createElement('div');line.className='event '+(severity==='HIGH'||severity==='CRITICAL'?'high':'');line.textContent=`${new Date().toLocaleTimeString()} · ${type} · ${description}`;$('events').prepend(line);const t=document.createElement('div');t.className='timeline-item';t.innerHTML=`<b>${new Date().toLocaleTimeString()}</b><span>${escapeHtml(type)}</span><i>${severity} +${pts}</i><small>${escapeHtml(description)}</small>`;$('timeline').prepend(t);addNotification(severity,type,description);updateScore()}
function updateScore(){const p=Math.round(score);$('meterFill').style.width=p+'%';$('scoreText').textContent=p+'%';let state='● NORMAL',cls='normal';if(p>=cfg.critical_threshold){state='● CRITICAL';cls='critical'}else if(p>=cfg.suspicious_threshold){state='● SUSPICIOUS ACTIVITY';cls='dangerText'}else if(p>=cfg.warning_threshold){state='● WARNING';cls='warning'}$('alertState').textContent=state;$('alertState').className=cls;if(p>=cfg.suspicious_threshold&&cfg.buzzer_enabled)beep()}
setInterval(()=>{if(sid&&score>0){score=Math.max(0,score-Math.max(0,Number(cfg.score_decay)||2));updateScore()}},1000);
function beep(){if(window.__lastBeep&&Date.now()-window.__lastBeep<2500)return;window.__lastBeep=Date.now();try{const a=new AudioContext(),o=a.createOscillator(),g=a.createGain();o.frequency.value=740;g.gain.value=.035;o.connect(g);g.connect(a.destination);o.start();setTimeout(()=>{o.stop();a.close()},220)}catch(e){}}
function addNotification(level,title,body){const n=document.createElement('div');n.className='notice';n.innerHTML=`<b>${escapeHtml(level)} · ${escapeHtml(title)}</b><span>${escapeHtml(body)}</span>`;$('notices').prepend(n);const count=document.querySelectorAll('#notices .notice').length;$('noticeCount').textContent=count}
function toast(t){const x=$('toast');x.textContent=t;x.classList.add('show');setTimeout(()=>x.classList.remove('show'),3500)}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}

async function toggleRadar(){
  if(!cfg.radar_enabled){toast('Radar is disabled in settings.');return}
  radarOn=!radarOn;
  setBreaker('radarBtn',radarOn);
  $('radarState').textContent=radarOn?'CONNECTING':'OFFLINE';$('sRadar').textContent=radarOn?'CONNECTING':'OFFLINE';
  $('radarEmpty').textContent=radarOn?'WAITING FOR RADAR TARGETS':'RADAR OFF';$('sweep').style.display=radarOn?'block':'none';
  if(radarOn){
    if(userRole==='STUDENT'){
      const ok=await ensureStudentSession();
      if(!ok){radarOn=false;setBreaker('radarBtn',false);return}
      startRadarHeartbeat();
    }
    connectRadar();
  }else{
    stopRadarHeartbeat();
    if(radarSocket)radarSocket.close();$('targets').innerHTML='';
    if(userRole==='STUDENT')fetch('/api/live-radar/stop',{method:'POST',keepalive:true}).catch(()=>{});
  }
}
async function relayRadar(targets){if(!sid||userRole!=='STUDENT')return;try{await fetch('/api/live-radar/frame',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,targets})})}catch(_){} }
function startRadarHeartbeat(){stopRadarHeartbeat();if(userRole!=='STUDENT')return;const beat=()=>{if(!radarOn||!sid)return;fetch('/api/live-radar/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid}),cache:'no-store'}).catch(()=>{})};beat();radarRelayTimer=setInterval(beat,1000)}
function stopRadarHeartbeat(){if(radarRelayTimer){clearInterval(radarRelayTimer);radarRelayTimer=null}}
function renderTargets(arr){$('targets').innerHTML='';$('radarEmpty').style.display=arr.length?'none':'grid';arr.forEach((t,i)=>{const d=document.createElement('span');d.className='target';const a=(Number(t.angle)||0)*Math.PI/180,r=Math.min(44,Math.max(6,(Number(t.distance)||0)*10));d.style.left=(50+Math.sin(a)*r)+'%';d.style.top=(50-Math.cos(a)*r)+'%';d.title=`Target ${t.id||i+1} · ${t.distance}m`;$('targets').appendChild(d)})}

function openBluetooth(){togglePanel('bluetoothPanel');if($('bluetoothPanel').classList.contains('open'))checkLocalRadio('bluetooth')}
function openWifi(){togglePanel('wifiPanel');if($('wifiPanel').classList.contains('open'))checkLocalRadio('wifi')}
async function checkLocalRadio(kind){const endpoint=kind==='bluetooth'?'/radio/bluetooth/status':'/radio/wifi/status';try{const r=await fetch('http://127.0.0.1:8765'+endpoint);const j=await r.json();$(kind==='bluetooth'?'btRadio':'wifiRadio').textContent=j.available?(j.powered?'ON':'OFF'):'UNAVAILABLE'}catch(e){$(kind==='bluetooth'?'btRadio':'wifiRadio').textContent='LOCAL AGENT OFFLINE'}}
async function scanBluetooth(){const state=$('btScanState'),list=$('btList');state.textContent='SCANNING FOR AVAILABLE DEVICES…';list.innerHTML='';try{const r=await fetch('http://127.0.0.1:8765/bluetooth/scan');const j=await r.json();if(!r.ok)throw new Error(j.error||'Bluetooth scan failed');state.textContent=`SCAN COMPLETE · ${j.devices.length} DEVICE(S)`;list.innerHTML=j.devices.length?j.devices.map(d=>`<div class="scan-item"><b>${escapeHtml(d.name||'Unknown Bluetooth Device')}</b><span>${escapeHtml(d.address||d.id||'')} · ${escapeHtml(d.rssi==null?'Signal unavailable':`RSSI ${d.rssi}`)}</span></div>`).join(''):'<div class="scan-item"><b>No devices found</b><span>No nearby Bluetooth devices were returned by the real adapter.</span></div>'}catch(e){state.textContent='SCAN UNAVAILABLE';toast(e.message+' — start hardware/local_agent.py and turn Bluetooth ON.')}}

async function scanWifi(){const state=$('wifiScanState'),list=$('wifiList');state.textContent='SCANNING AVAILABLE NETWORKS / DEVICES…';list.innerHTML='';try{const r=await fetch('http://127.0.0.1:8765/wifi/scan');const j=await r.json();if(!r.ok)throw new Error(j.error||'Wi-Fi scan failed');state.textContent=`SCAN COMPLETE · ${j.networks.length} NETWORK(S)`;list.innerHTML=j.networks.length?j.networks.map(n=>`<div class="scan-item"><b>${escapeHtml(n.ssid||'(Hidden network)')}</b><span>Signal ${escapeHtml(String(n.signal??'?'))}% · ${escapeHtml(n.auth||'Unknown')} · ${escapeHtml(n.bssid||'')}</span>${n.ssid?`<button class="control small" onclick="connectWifi(${JSON.stringify(n.ssid)})">CONNECT</button>`:''}</div>`).join(''):'<div class="scan-item"><b>No Wi-Fi networks found</b><span>The real adapter returned no networks.</span></div>'}catch(e){state.textContent='SCAN UNAVAILABLE';toast(e.message+' — start hardware/local_agent.py.')}}
async function connectWifi(ssid){const password=prompt(`Enter Wi-Fi password for ${ssid}`);if(password===null)return;const state=$('wifiScanState');state.textContent=`CONNECTING TO ${ssid}…`;try{const r=await fetch('http://127.0.0.1:8765/wifi/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password})});const j=await r.json();if(!r.ok)throw new Error(j.error||'Wi-Fi connection failed');state.textContent=`CONNECTED · ${ssid}`;toast(`Wi-Fi connected: ${ssid}`);await scanWifi()}catch(e){state.textContent='CONNECTION FAILED';toast(e.message)}}

async function loadDevices(){const r=await fetch('/api/devices');if(!r.ok)return;const j=await r.json();$('deviceList').innerHTML=j.devices.length?j.devices.map(d=>`<div class="device"><b>${escapeHtml(d.name)}</b><br>${escapeHtml(d.device_type)} · ${escapeHtml(d.transport)} · ${escapeHtml(d.status)}<br>${escapeHtml(d.ip||'')} · last seen ${escapeHtml(d.last_seen||'')}</div>`).join(''):'No registered devices yet. Use the real Bluetooth/Wi-Fi scanners or radar agent.'}
async function loadAdminSettings(){const r=await fetch('/api/settings');if(!r.ok)return;cfg=Object.assign(cfg,(await r.json()).settings);Object.assign(weights,{FACE_MISSING:cfg.face_missing_weight,MULTIPLE_PERSONS:cfg.multiple_person_weight,HAND_DETECTED:cfg.hand_detection_weight})}

async function submitStudentCliInput(input){
  const command=input.value.trim(); if(!command)return; input.value='';
  try{
    const r=await fetch('/api/cli',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({command,cwd:cliCwd})});
    const j=await r.json();
    $('cliOut').textContent+=`sad@monitor:${cliCwd==='/'?'~':cliCwd}$ ${command}\n${j.output||j.error||''}\n\n`;
    if(j.cwd){cliCwd=j.cwd;$('cliPath').textContent=cliCwd==='/'?'~':cliCwd}
    if(j.clear)$('cliOut').textContent='';
    if(j.download)window.open(j.download,'_blank','noopener');
    $('cliOut').scrollTop=$('cliOut').scrollHeight;
  }catch(err){$('cliOut').textContent+=`CLI ERROR: ${err.message}\n\n`;}
  input.focus();
}
$('cliInput')?.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();submitStudentCliInput(e.target)}});


document.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b)return;
  b.classList.remove('click-flash'); void b.offsetWidth; b.classList.add('click-flash');
});
$('terminal')?.addEventListener('click',()=>setTimeout(()=>$('cliInput')?.focus(),0));


// Matrix-rain background. Visual only; it does not collect or transmit data.
(function initMatrixRain(){
  const c=document.getElementById('matrixRain'); if(!c)return;
  const ctx=c.getContext('2d'); let w=0,h=0,cols=0,drops=[];
  const chars='01ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%<>[]{}';
  function resize(){w=c.width=window.innerWidth;h=c.height=window.innerHeight;cols=Math.floor(w/16);drops=Array.from({length:cols},()=>Math.random()*-80);}
  function draw(){ctx.fillStyle='rgba(16,0,3,.12)';ctx.fillRect(0,0,w,h);ctx.font='13px Consolas,monospace';ctx.fillStyle='rgba(255,40,65,.78)';for(let i=0;i<drops.length;i++){const ch=chars[Math.floor(Math.random()*chars.length)];ctx.fillText(ch,i*16,drops[i]*16);if(drops[i]*16>h&&Math.random()>.975)drops[i]=Math.random()*-20;drops[i]+=1;}requestAnimationFrame(draw)}
  resize();window.addEventListener('resize',resize);draw();
})();
