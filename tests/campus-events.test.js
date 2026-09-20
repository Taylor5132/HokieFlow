import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../ui/app.js',import.meta.url),'utf8');
function setup(user=true,end='2026-09-22T11:00:00-04:00'){
 const c={state:{user:user?{id:'test'}:null,accountData:{events:[],plans:[{id:'keep'}]}},campusEvents:[{id:'evt_test',title:'Campus event',location:'Squires',start:'2026-09-22T10:00:00-04:00',end}],addingCampusEvent:null,campusEventError:'',render(){},announce(){},authScreen(mode){c.login=mode;},eventEditor(id,draft){c.draft=draft;},events(){return c.state.accountData.events;},async saveAccount(data){c.saved=data;c.state.accountData=data;}};
 vm.createContext(c);vm.runInContext(source.slice(source.indexOf('async function addCampusEvent(id)')),c);return c;
}
test('plus saves a schedule event and preserves account data; repeated plus does not duplicate',async()=>{const c=setup();await c.addCampusEvent('evt_test');await c.addCampusEvent('evt_test');assert.equal(c.saved.events.length,1);assert.equal(c.saved.events[0].kind,'event');assert.equal(c.saved.plans[0].id,'keep');assert.equal(c.saved.events[0].start,'2026-09-22T10:00:00-04:00');});
test('signed-out plus opens login without saving',async()=>{const c=setup(false);await c.addCampusEvent('evt_test');assert.equal(c.login,'login');assert.equal(c.saved,undefined);});
test('unknown end opens editor instead of saving a guessed duration',async()=>{const c=setup(true,null);await c.addCampusEvent('evt_test');assert.equal(c.draft.end,null);assert.equal(c.saved,undefined);});
test('failed account save is shown and releases pending state',async()=>{const c=setup();c.saveAccount=async()=>{throw Error('Save failed');};await c.addCampusEvent('evt_test');assert.equal(c.campusEventError,'Save failed');assert.equal(c.addingCampusEvent,null);assert.equal(c.state.accountData.events.length,0);});
test('event page does not repeat the account banner but keeps a separate events error',()=>{
 const c={state:{accountError:'Schedule unavailable'},campusEventState:'ok',campusEventError:'Schedule unavailable',campusEvents:[],campusEventNotices:[],heading:()=>'',escape:value=>value};
 vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function campusEventsPage()'),source.indexOf('async function addCampusEvent(id)')),c);
 assert.equal(c.campusEventsPage().includes('role="alert"'),false);
 c.campusEventError='Events could not load';assert.equal(c.campusEventsPage().includes('Events could not load'),true);
});
