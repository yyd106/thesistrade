import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this.events={};this.hidden=false;this.disabled=false;this.textContent='';}
  append(...nodes){this.children.push(...nodes);for(const node of nodes)node.parent=this;}
  addEventListener(name,handler){(this.events[name]??=[]).push(handler);}
  setAttribute(name,value){this.attrs[name]=String(value);}
  click(){downloads.push({href:this.href,filename:this.download});}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);this.removed=true;}
}
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);};
const downloads=[],objects=[],revoked=[],timeouts=[],timers=[],redirects=[],body=new Element('body');
node('strategy-download').hidden=true;
const context=vm.createContext({
  document:{body,addEventListener(){},createElement:tag=>new Element(tag),getElementById:node},
  fetch(){throw Error('No real network in strategy download tests');},
  location:{pathname:'/',assign:path=>redirects.push(path)},
  AbortSignal:{timeout:ms=>{timeouts.push(ms);return new AbortController().signal;}},
  URLSearchParams,Blob,Uint8Array,
  URL:{createObjectURL:blob=>{objects.push(blob);return 'blob:strategy-'+objects.length;},revokeObjectURL:url=>revoked.push(url)},
  setTimeout:(callback,ms)=>{timers.push({callback,ms});return timers.length;},
});
const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
vm.runInContext(source,context,{filename:'app.js'});
const get=name=>vm.runInContext(name,context);
const call=()=>get('downloadStrategyArchive')();
const feedback=()=>node('strategy-download-feedback');
const button=node('strategy-download-button');
const zipBytes=new Uint8Array([0x50,0x4b,3,4,0,0,0,0]);
const reply=(status,type,content=zipBytes,disposition='attachment; filename="strategy-history.zip"')=>({
  ok:status>=200&&status<300,status,
  headers:new Headers({'Content-Type':type,'Content-Disposition':disposition}),
  blob:async()=>new Blob([content],{type}),
  json:async()=>typeof content==='string'?JSON.parse(content):content,
});
let calls=[];
const transport=handler=>{context.testTransport=async(path,options)=>{calls.push({path,options});return handler(path,options);};vm.runInContext('api.transport=testTransport',context);};
const assertIdle=()=>{
  assert.equal(button.disabled,false);assert.equal(button.textContent,'下载近两周策略');
  assert.equal(button.attrs['aria-busy'],'false');assert.equal(get('strategyDownloadBusy'),false);
};

// No initial role means no visible control. Guests cannot invoke a download even
// if the handler is called directly, and reinitialization never doubles listeners.
get('initStrategyDownload')();assert.equal(node('strategy-download').hidden,true);
vm.runInContext("currentUser={role:'GUEST'}",context);get('initStrategyDownload')();
assert.equal(node('strategy-download').hidden,true);
await call();assert.equal(calls.length,0);
vm.runInContext("currentUser={role:'ADMIN'}",context);get('initStrategyDownload')();get('initStrategyDownload')();
assert.equal(node('strategy-download').hidden,false);
assert.equal(button.events.click.length,1);

// A real asynchronous download request keeps the action locked until both
// archive generation and response body reading finish.
let finishRequest,finishBody;
const expectedName='研究与交易策略-20261009-20261022.zip';
transport(()=>new Promise(resolve=>{finishRequest=resolve;}));
const first=call();await call();
assert.equal(calls.length,1);assert.equal(button.disabled,true);
assert.equal(button.attrs['aria-busy'],'true');assert.match(feedback().textContent,/正在整理/);
assert.equal(calls[0].path,'/api/strategy-export');
assert.equal(calls[0].options.method,'GET');
assert.equal(calls[0].options.credentials,'same-origin');
assert.equal(calls[0].options.cache,'no-store');
assert.equal(calls[0].options.headers.Accept,'application/zip');
assert.equal(timeouts.at(-1),300000,'Archive requests do not use the normal 20 second JSON deadline');
const response=reply(200,'application/zip',zipBytes,"attachment; filename=fallback.zip; filename*=UTF-8''"+encodeURIComponent(expectedName));
response.blob=()=>new Promise(resolve=>{finishBody=resolve;});
finishRequest(response);await new Promise(resolve=>setImmediate(resolve));
await call();assert.equal(calls.length,1);assert.equal(button.disabled,true);
finishBody(new Blob([zipBytes],{type:'application/zip'}));await first;
assertIdle();assert.equal(downloads.length,1);assert.equal(downloads[0].filename,expectedName);
assert.equal(objects.length,1);assert.equal(objects[0].size,zipBytes.length);
assert.equal(body.children.length,0,'Temporary download link is removed');
assert.equal(revoked.length,0,'Object URL remains valid long enough for browser download startup');
assert.equal(timers.at(-1).ms,60000);timers.at(-1).callback();
assert.deepEqual(revoked,['blob:strategy-1']);assert.match(feedback().textContent,/已开始下载/);

// Responses containing JSON errors, login pages or mislabeled content must not
// become files with a misleading .zip suffix.
for(const [status,type,content,message] of [
  [403,'application/json',{error:'仅管理员可以下载策略'},/仅管理员/],
  [503,'application/json',{error:'策略正在整理，请稍后再试'},/请稍后再试/],
  [200,'application/json',{error:'本次没有可下载的资料'},/没有可下载/],
  [200,'text/html','<html>sign in</html>',/暂未准备好/],
  [500,'text/plain','database internal exception',/暂未准备好/],
  [200,'application/zip','{"error":"not an archive"}',/文件不完整/],
  [200,'application/zip','',/文件不完整/],
]){
  transport(()=>reply(status,type,content));await call();
  assertIdle();assert.match(feedback().textContent,message);assert.match(feedback().className,/error/);
  assert.equal(downloads.length,1);assert.equal(objects.length,1);
}

// Authentication expires through the same login route as the rest of the app.
transport(()=>reply(401,'application/json',{error:'expired'}));await call();
assertIdle();assert.deepEqual(redirects,['/login?next=%2F']);
assert.match(feedback().textContent,/登录已失效/);assert.equal(downloads.length,1);

for(const error of [new TypeError('connection lost'),Object.assign(new Error('slow'),{name:'TimeoutError'})]){
  transport(()=>{throw error;});await call();assertIdle();
  assert.match(feedback().textContent,error.name==='TimeoutError'?/用时较长/:/检查连接/);
  assert.equal(downloads.length,1);
}
transport(()=>({...reply(200,'application/zip'),blob:async()=>{throw new TypeError('incomplete response body');}}));
await call();assertIdle();assert.equal(downloads.length,1);assert.match(feedback().className,/error/);

// Retry after failure succeeds; no error markup or remote filename becomes DOM.
transport(()=>reply(200,'application/zip',zipBytes,'attachment; filename="../../history.zip"'));
await call();assertIdle();assert.equal(downloads.length,2);assert.equal(downloads[1].filename,'history.zip');
assert.doesNotMatch(feedback().className,/error/);
assert.equal(get('strategyArchiveFilename')('attachment; filename="unexpected.html"'),'研究与交易策略-近两周.zip');
assert.equal(get('strategyArchiveFilename')("attachment; filename=fallback.zip; filename*=UTF-8''%broken"),'fallback.zip');
vm.runInContext("currentUser={role:'GUEST'}",context);get('initStrategyDownload')();
assert.equal(node('strategy-download').hidden,true);const previousCalls=calls.length;
await call();assert.equal(calls.length,previousCalls);

const html=await readFile(new URL('../ashare/web/index.html',import.meta.url),'utf8');
assert.match(html,/<section[^>]*id="strategy-download"[^>]*class="[^"]*admin-only[^"]*"[^>]*hidden/);
assert.match(html,/id="strategy-download-button"[^>]*type="button"[^>]*>下载近两周策略<\/button>/);
assert.match(html,/含今天.*14 个北京时间自然日.*研究、交易策略及变化原因.*截至文件生成时点/);
assert.match(html,/id="strategy-download-feedback"[^>]*role="status"[^>]*aria-live="polite"[^>]*hidden/);
const css=await readFile(new URL('../ashare/web/app.css',import.meta.url),'utf8');
assert.match(css,/\.admin-only\{display:none!important\}/);
assert.match(css,/body\[data-role="ADMIN"\] \.admin-only\[hidden\]\{display:none!important\}/);
assert.match(source,/initBoardTabs\(\);initStrategyDownload\(\)/,'Successful session setup enables the control');
console.log('Strategy download UI: admin visibility, long request/body deadline, duplicate lock, safe archive response, filename, login expiry, failure recovery and browser download passed.');
