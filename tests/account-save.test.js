import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../ui/app.js',import.meta.url),'utf8');
function setup(){
 const c={state:{user:{id:'one',name:'Student'},accountData:{plans:[],events:[]},accountVersion:0,accountSaving:false,accountStorageError:''},document:{body:{classList:{remove(){}}}}};
 c.accountRequest=async(path,body,method)=>{c.request={path,body,method};return {ok:true,...body,version:body.version+1};};
 vm.createContext(c);
 vm.runInContext(source.slice(source.indexOf('function applyAccount(result)'),source.indexOf('\nasync function loadAccount()')),c);
 vm.runInContext(source.slice(source.indexOf('async function saveAccount(data)'),source.indexOf('\nfunction showAccountError')),c);
 return c;
}
test('save retains identity even when backend omits user and advances the version for the next save',async()=>{
 const c=setup();await c.saveAccount({plans:[],events:[{id:'event-1'}]});
 assert.equal(c.state.user.id,'one');assert.equal(c.state.accountVersion,1);
 assert.equal(c.state.accountData.events[0].id,'event-1');
 await c.saveAccount(c.state.accountData);assert.equal(c.request.body.version,1);
 assert.equal(c.request.method,'PUT');assert.equal(c.state.accountSaving,false);
});
test('failed initial read blocks saving an empty account over existing cloud data',async()=>{
 const c=setup();c.applyAccount({user:{id:'one'},storageError:'Please retry loading your schedule.'});
 await assert.rejects(()=>c.saveAccount({events:[]}),/retry loading/);
 assert.equal(c.request,undefined);
 c.applyAccount({user:{id:'one'},data:{events:[{id:'existing'}],plans:[]},version:4});
 await c.saveAccount(c.state.accountData);assert.equal(c.request.body.version,4);
});
test('failed save preserves local identity and data and releases pending state',async()=>{
 const c=setup();c.accountRequest=async()=>{throw Error('Please sign in again.');};
 await assert.rejects(()=>c.saveAccount({events:[{id:'unsaved'}]}),/sign in/);
 assert.equal(c.state.user.id,'one');assert.equal(c.state.accountData.events.length,0);
 assert.equal(c.state.accountSaving,false);
});
test('explicit logout clears identity and saved state',()=>{
 const c=setup();c.applyAccount({user:null});assert.equal(c.state.user,null);
 assert.equal(c.state.accountData.events.length,0);
});
