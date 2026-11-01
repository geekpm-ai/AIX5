from aix5_ai.web_ui import INDEX


def test_onvif_flow_dialog_exposes_all_user_visible_stages_and_close_action():
    """The ONVIF add flow must explain long-running work instead of a small inline note."""
    assert 'id="onvifFlowDialog"' in INDEX
    assert 'aria-label="取消并关闭"' in INDEX
    for stage in ("scan", "auth", "profiles", "save"):
        assert f'data-onvif-flow-stage="{stage}"' in INDEX
    assert "id=\"onvifFlowElapsed\"" in INDEX
    assert "id=\"onvifFlowPercent\"" in INDEX
    assert "id=\"onvifFlowStatus\"" in INDEX


def test_onvif_flow_cancels_requests_and_ignores_stale_results():
    """Closing the progress window must abort the active fetch and guard late responses."""
    assert "controller:new AbortController()" in INDEX
    assert "signal:token.controller.signal" in INDEX
    assert "token.controller.abort()" in INDEX
    assert "onvifFlowIsCurrent(token)" in INDEX
    assert "if(token.kind==='scan'){onvifScanVersion++;" in INDEX


def test_linkage_page_uses_onvif_configuration_only():
    """The UI guides Home Assistant through ONVIF as the sole integration path."""
    start = INDEX.index('<section id="linkage"')
    end = INDEX.index("</section>", start) + len("</section>")
    linkage = INDEX[start:end]

    assert "ONVIF 联动" in linkage
    assert "onvifGatewayUrl" in linkage
    assert "onvifStatusDetail" in linkage
    assert "loadOnvifStatus()" in linkage
    assert "Home Assistant" in linkage
    assert "saveSettings()" not in linkage


def test_linkage_status_refresh_reads_onvif_event_state():
    """The linkage status action should query the ONVIF gateway diagnostics endpoint."""
    assert "async function loadOnvifStatus()" in INDEX
    assert "api('/api/onvif/events')" in INDEX
    assert "PullPoint 订阅" in INDEX


def test_home_assistant_linkage_uses_onvif_only():
    """The user-facing integration entry point is ONVIF."""
    assert 'ONVIF 联动' in INDEX
    assert 'loadOnvifStatus()' in INDEX
    assert 'function saveSettings' not in INDEX


def test_preview_uses_status_dot_and_codec_metadata_without_resource_dock():
    """Preview cards stay unobstructed while retaining accessible status information."""
    assert 'id="resourceDock"' not in INDEX
    assert "previewCodec(camera)" in INDEX
    assert "status-dot ${c.status==='running'?'ok':'bad'}" in INDEX
    assert "SYSTEM LOAD" not in INDEX


def test_events_use_thumbnail_grid_cards():
    """Event history is image-led and keeps filter/time/device metadata."""
    assert 'id="eventRows" class="event-grid"' in INDEX
    assert "event-card" in INDEX
    assert "event-thumb" in INDEX
    assert "eventImageUrl(event)" in INDEX
    assert "formatEventTimestamp(event)" in INDEX
    assert 'class="event-image-link"' in INDEX
    assert 'aria-controls="eventImageDialog"' in INDEX
    assert 'id="eventExportJson"' not in INDEX
    assert 'download="aix5-events.csv"' in INDEX
    assert "查看原图" not in INDEX
    assert "已记录" not in INDEX


def test_rule_editor_supports_target_absence_durations():
    """Rules can alert after a selected target has been absent continuously."""
    assert 'id="ruleAbsenceSeconds"' in INDEX
    for value in ("60", "300", "600", "1800", "3600"):
        assert f'value="{value}"' in INDEX
    assert "absence_seconds" in INDEX
    assert "持续缺失" in INDEX


def test_preview_audio_playback_recovery_and_stale_peer_callbacks():
    """Run the browser control functions against media/peer lifecycle doubles."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node.js is needed for the browser audio lifecycle test")
    audio = INDEX[INDEX.index("const audioEnabled=") : INDEX.index("let audioPollBusy=")]
    start = INDEX[
        INDEX.index("async function startWebrtc(") : INDEX.index("async function restartWebrtc(")
    ]
    harness = r'''
import assert from 'node:assert/strict';
const videos = new Map(), rows = new Map(), rtcPeers = {}, rtcVersions = {}, cameras = [];
let now = 20000;
Date.now = () => now;
function control(){return {attributes:{},style:{},classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v}}}
function addCamera(id){
  const camera={id,status:'running',audio_present:true,audio_status:'running',audio_level:40,audio_codec:'aac'};
  cameras.push(camera);
  const controls=Object.fromEntries(['.audio-toggle','.audio-meter','.audio-meter-fill','.audio-level-text'].map(k=>[k,control()]));
  rows.set(id,{querySelector:k=>controls[k]});
  const video={muted:true,paused:true,srcObject:{getAudioTracks:()=>[{readyState:'live'}]},async play(){this.paused=false}};
  videos.set(id,video);rtcPeers[id]={connectionState:'connected',previewStartedAt:0};
  return {camera,video,button:controls['.audio-toggle']};
}
const document={querySelector(selector){const id=selector.match(/="([^"]+)"/)[1];return selector.startsWith('[data-webrtc-video')?videos.get(id):rows.get(id)}};
function bindDetectionChannel(){}
function resizeOverlay(){}
function previewMeta(){return ''}
class MediaStream {
  tracks=[];
  addTrack(track){this.tracks.push(track)}
  getAudioTracks(){return this.tracks.filter(track=>track.kind==='audio')}
}
const createdPeers=[];
class RTCPeerConnection {
  constructor(){createdPeers.push(this)}
  connectionState='new';
  addTransceiver(){}
  createDataChannel(){return {label:'detections'}}
  async createOffer(){return {sdp:'offer',type:'offer'}}
  async setLocalDescription(offer){this.localDescription=offer}
  async setRemoteDescription(){}
  close(){this.connectionState='closed'}
}
async function api(){return {sdp:'answer',type:'answer'}}
'''
    checks = r'''
const a=addCamera('a'),b=addCamera('b');
refreshAudioControls(a.camera);
assert.equal(a.button.attributes['aria-pressed'],'false');
await toggleCameraAudio('a');
assert.equal(a.video.muted,false);
assert.equal(a.button.attributes['aria-pressed'],'true');
assert.equal(b.video.muted,true);
await toggleCameraAudio('a');
let attempts=0;
a.video.play=async function(){attempts++;if(attempts===1)throw {name:'NotAllowedError'};this.paused=false};
await toggleCameraAudio('a');
assert.equal(attempts,2,'blocked audible play retries muted');
assert.equal(a.video.muted,true);
assert.equal(a.button.attributes['aria-pressed'],'false');
assert.equal(audioEnabled.has('a'),false);
assert.equal(b.video.muted,true);

// A rejected play promise belonging to an old player cannot mute its replacement.
let rejectOld;
a.video.play=()=>new Promise((resolve,reject)=>{rejectOld=reject});
audioEnabled.add('a');
const oldPlay=playCameraVideo('a',a.video,rtcPeers.a);
const replacement={...a.video,muted:false};
videos.set('a',replacement);rtcPeers.a={connectionState:'connected',previewStartedAt:now};
rejectOld({name:'NotAllowedError'});await oldPlay;
assert.equal(replacement.muted,false);
assert.equal(audioEnabled.has('a'),true);

const originalStart=startWebrtc, missing=addCamera('missing');
missing.video.srcObject={getAudioTracks:()=>[]};
let reconnects=[],finishReconnect;
startWebrtc=(id,mode)=>{reconnects.push(id);return new Promise(resolve=>{finishReconnect=resolve})};
refreshAudioControls(missing.camera);
assert.equal(missing.button.disabled,true);
refreshAudioControls(missing.camera);
assert.deepEqual(reconnects,['missing'],'meter polling cannot overlap reconnects');
finishReconnect();await Promise.resolve();now+=1000;
refreshAudioControls(missing.camera);
assert.equal(reconnects.length,1,'reconnect attempts have a cooldown');
now+=10000;refreshAudioControls(missing.camera);
assert.deepEqual(reconnects,['missing','missing']);
finishReconnect();await Promise.resolve();
missing.camera.audio_present=false;now+=10000;refreshAudioControls(missing.camera);
assert.equal(reconnects.length,2,'absent source does not trigger reconnects');
startWebrtc=originalStart;

const live=addCamera('live');delete rtcPeers.live;
await startWebrtc('live');
const oldPeer=rtcPeers.live,oldStream=live.video.srcObject;
const newStream=new MediaStream(),newVideo={...live.video,srcObject:newStream};
videos.set('live',newVideo);rtcPeers.live={connectionState:'connected',previewStartedAt:now};
oldPeer.ontrack({track:{kind:'audio',readyState:'live'}});
assert.equal(newStream.tracks.length,0,'stale ontrack cannot change the new media stream');
assert.equal(oldStream.tracks.length,0);

// Two reconnect requests waiting for the old peer leave only the latest request active.
const racing=addCamera('racing');let finishClose;
const closing=new Promise(resolve=>{finishClose=resolve});rtcPeers.racing.close=()=>closing;
const beforeRace=createdPeers.length;
const one=startWebrtc('racing'),two=startWebrtc('racing');finishClose();
await Promise.all([one,two]);
assert.equal(rtcVersions.racing,2);
assert.equal(createdPeers.length-beforeRace,1,'concurrent reconnects create only one new peer');
assert.equal(rtcPeers.racing.connectionState,'new');
console.log('browser audio lifecycle checks passed');
'''
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=harness + audio + start + checks,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_event_date_timeline_pagination_and_stale_query_behavior():
    """Exercise event navigation, timezone mapping and races in real JavaScript."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node.js is needed for event timeline navigation checks")
    script = INDEX[
        INDEX.index("const COMMON_TIMEZONES=") : INDEX.index("let onvifStatusLoading=false;")
    ]
    harness = r'''
import assert from 'node:assert/strict';
const nodes = new Map(), calls = [], cameras = [{id:'cam-a',name:'入口'}];
let now=Date.parse('2026-10-30T16:30:00Z');Date.now=()=>now;
const setInterval=()=>0;
function makeNode(){return {
  value:'',textContent:'',innerHTML:'',style:{},dataset:{},disabled:false,required:false,href:'',attributes:{},listeners:{},
  classList:{values:new Set(),add(...items){items.forEach(item=>this.values.add(item))},remove(...items){items.forEach(item=>this.values.delete(item))},contains(item){return this.values.has(item)},toggle(item,force){if(force===undefined)force=!this.values.has(item);force?this.values.add(item):this.values.delete(item)}},
  setAttribute(k,v){this.attributes[k]=String(v)},getAttribute(k){return this.attributes[k]},
  replaceChildren(...items){this.children=items;this.value=''},add(item){this.children.push(item)},
  addEventListener(name,fn){this.listeners[name]=fn},scrollIntoView(){this.scrolled=true},focus(){},
  getBoundingClientRect(){return {top:0,height:500}},setPointerCapture(){},hasPointerCapture(){return false},releasePointerCapture(){}
}}
const $=selector=>{if(!nodes.has(selector))nodes.set(selector,makeNode());return nodes.get(selector)};
const document={querySelector:$};
const window={matchMedia:()=>({matches:true})};
class Option{constructor(label,value){this.label=label;this.value=value}}
const onvifEscape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const formatStorageBytes=n=>String(n)+' B';
let apiImpl;
async function api(url,options){calls.push(url);return apiImpl(url,options)}
function timelineFor(date){
  const start=Date.parse(date+'T00:00:00+08:00')/1000,end=start+86400;
  return {date,timezone:'Asia/Shanghai',start,end,now:now/1000,total:125,buckets:Array.from({length:288},(_,i)=>({start:start+i*300,end:start+(i+1)*300,count:i===2?12:0}))};
}
function pageData(cursor){const offset=cursor==='page-2'?60:cursor==='page-3'?120:0,size=Math.min(60,125-offset);return {total:125,has_more:offset+size<125,next_cursor:offset+size<125?'page-'+(offset===0?2:3):null,items:Array.from({length:size},(_,i)=>({id:'e-'+(offset+i),created:now/1000-offset-i,camera_name:'入口',camera_id:'cam-a',event_type:'target_detected',target_type:'person',confidence:.8}))}}
apiImpl=async url=>{
 const parsed=new URL(url,'http://test');
 if(parsed.pathname==='/api/settings')return {timezone:'Asia/Shanghai'};
 if(parsed.pathname==='/api/events/page')return pageData(parsed.searchParams.get('cursor'));
 if(parsed.pathname==='/api/events/timeline')return timelineFor(parsed.searchParams.get('date'));
 if(parsed.pathname==='/api/events/summary')return {total:2543,oldest:now/1000-86400,newest:now/1000,retention_days:7,max_event_storage_mb:2048,event_bytes:140000000};
 throw Error('Unexpected request '+url);
};
'''
    checks = r'''
await loadEvents();
assert.equal($('#eventDate').value,'2026-10-31','default date follows configured timezone, not host TZ');
let first=new URL(calls.find(url=>url.startsWith('/api/events/page?')),'http://test');
assert.equal(first.searchParams.get('start'),'2026-10-31T00:00:00');
assert.equal(first.searchParams.get('end'),'2026-11-01T00:00:00');
assert.equal(eventRecords.length,60);assert.equal(eventTotal,125);
assert.match($('#eventStorageSummary').textContent,/2,543/);
assert.equal($('#eventNextPage').disabled,false);
await changeEventPage(1);
assert.equal(eventPageIndex,1);assert.equal(eventRecords[0].id,'e-60');
await changeEventPage(1);
assert.equal(eventRecords.length,5);assert.equal(eventRecords[0].id,'e-120');assert.equal($('#eventNextPage').disabled,true);
await changeEventPage(-1);assert.equal(eventRecords[0].id,'e-60');
$('#eventCam').value='cam-a';syncEventCameraOptions();assert.equal($('#eventCam').value,'cam-a');
$('#eventType').value='target_absent';
await eventFiltersChanged('category');assert.equal(eventPageIndex,0);
let query=eventFilterQuery();assert.equal(query.get('event_type'),'target_absent');assert.equal(query.get('camera_id'),'cam-a');
$('#eventStartTime').value='14:20';$('#eventEndTime').value='13:00';
assert.throws(()=>eventFilterQuery(),/结束时间/);
$('#eventStartTime').value='00:10';$('#eventEndTime').value='00:30';
query=eventFilterQuery();assert.equal(query.get('start'),'2026-10-31T00:10:00');
updateEventExports(query);assert.equal(new URL($('#eventExportCsv').href,'http://test').searchParams.get('start'),'2026-10-31T00:10:00');
assert.equal(new URL($('#eventExportCsv').href,'http://test').searchParams.has('cursor'),false);
assert.equal(new URL($('#eventExportCsv').href,'http://test').searchParams.get('format'),'csv');

// Hover only inspects the full day's histogram; no request should be sent.
eventTimelineData=timelineFor('2026-10-31');eventTimelineReadAt=now;
const beforeHover=calls.length;showEventTimelineHover(2);assert.equal(calls.length,beforeHover);
assert.match($('#eventTimelineHover').textContent,/12 条事件/);
const range=eventTimelineRangeFor(2,4);assert.equal(range.end-range.start,900);assert.equal(range.count,12);
await selectEventTimeline(2,4);
query=eventFilterQuery();assert.equal(query.get('start'),'2026-10-30T16:10:00.000Z');assert.equal(query.get('end'),'2026-10-30T16:25:00.000Z');
assert.equal(eventPageIndex,0);assert.equal($('#eventResultsHeading').scrolled,true);
await showFullDayEvents();assert.equal(eventTimelineRange,null);assert.equal($('#eventStartTime').value,'');
await showAllEventDates();query=eventFilterQuery();assert.equal(query.has('start'),false);assert.equal(query.has('end'),false);assert.equal(eventPageIndex,0);

// Two repeated local hours on DST fall-back must remain distinct buckets.
applyEventTimezone('America/New_York',false);eventAllDates=false;$('#eventDate').value='2026-11-01';
const start=Date.parse('2026-11-01T04:00:00Z')/1000,end=Date.parse('2026-11-02T05:00:00Z')/1000;
eventTimelineData={date:'2026-11-01',timezone:'America/New_York',start,end,now:end+3600,total:2,buckets:Array.from({length:300},(_,i)=>({start:start+i*300,end:start+(i+1)*300,count:0}))};eventTimelineReadAt=now;
assert.equal(eventTimelineBounds().end-eventTimelineBounds().start,25*3600);
assert.equal(eventClock(start+3600),eventClock(start+7200));
eventTimelineRange=eventTimelineRangeFor(23,24);$('#eventStartTime').value='01:55';$('#eventEndTime').value='01:05';
query=eventFilterQuery();assert.equal(query.get('start'),'2026-11-01T05:55:00.000Z');assert.equal(query.get('end'),'2026-11-01T06:05:00.000Z');

// A slow older response cannot replace the newly selected date/page.
applyEventTimezone('Asia/Shanghai',false);eventTimelineRange=null;$('#eventStartTime').value='';$('#eventEndTime').value='';
const normalApi=apiImpl;let releaseOld,oldReached;
const oldReady=new Promise(resolve=>{oldReached=resolve});
apiImpl=async (url,options)=>{
 const parsed=new URL(url,'http://test');
 if(parsed.pathname==='/api/events/page'&&parsed.searchParams.get('start')?.startsWith('2026-10-29')){oldReached();return new Promise(resolve=>{releaseOld=resolve})}
 return normalApi(url,options);
};
$('#eventDate').value='2026-10-29';const old=loadEvents();await oldReady;
$('#eventDate').value='2026-10-30';await loadEvents();
releaseOld({items:[{id:'stale'}],total:1,has_more:false,next_cursor:null});await old;
assert.notEqual(eventRecords[0]?.id,'stale');assert.match(eventFilterKey,/2026-10-30/);
assert.equal(eventBusy,false);
// Keyboard navigation previews without fetching; Enter applies one interval.
const timeTrack=$('#eventTimelineTrack'),beforeKey=calls.length;
timeTrack.listeners.keydown({key:'ArrowDown',preventDefault(){}});
assert.equal(calls.length,beforeKey);assert.equal(eventTimelineKeyboard,1);
const originalSelect=selectEventTimeline;let selectedRange;
selectEventTimeline=(first,last)=>{selectedRange=[first,last]};
timeTrack.listeners.keydown({key:'Enter',preventDefault(){}});assert.deepEqual(selectedRange,[1,1]);
// Pointer drag selects a time range only on release, never on every move.
timeTrack.listeners.pointerdown({button:0,clientY:100,pointerId:1,preventDefault(){}});
timeTrack.listeners.pointermove({clientY:200,pointerId:1});assert.equal(calls.length,beforeKey);
timeTrack.listeners.pointerup({clientY:200,pointerId:1});assert.ok(selectedRange[1]>selectedRange[0]);
selectEventTimeline=originalSelect;
// Failed requests clear stale page navigation and expose a recoverable message.
apiImpl=async (url,options)=>{if(url.startsWith('/api/events/page'))throw Error('test query failed');return normalApi(url,options)};
await loadEvents();assert.equal(eventRecords,null);assert.equal($('#eventPreviousPage').disabled,true);assert.equal($('#eventNextPage').disabled,true);assert.match($('#eventFilterMessage').textContent,/test query failed/);
console.log('event timeline, date filters, cursor pages, DST and stale-query checks passed');
'''
    result = subprocess.run(
        [node, "--input-type=module", "-"],
        input=harness + script + checks,
        text=True,
        capture_output=True,
        timeout=20,
        env={**__import__("os").environ, "TZ": "UTC"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
