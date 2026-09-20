import test from 'node:test';
import assert from 'node:assert/strict';
import {loadPreferences,savePreferences,dueReminders} from '../ui/preferences.js';
test('preferences tolerate unavailable storage and persist supported options',()=>{
 assert.deepEqual(loadPreferences(undefined),{theme:'light',notifications:false});
 let value='not json';const store={getItem:()=>value,setItem:(_,v)=>value=v};assert.equal(loadPreferences(store).theme,'light');
 savePreferences(store,{theme:'dark',notifications:true});assert.deepEqual(loadPreferences(store),{theme:'dark',notifications:true});
});
test('reminders include only events starting within ten minutes',()=>{
 const now=new Date('2026-09-19T12:00:00Z');const event=(id,start)=>({id,title:id,start,end:'2026-09-19T13:00:00Z'});
 const result=dueReminders([event('past','2026-09-19T11:55:00Z'),event('soon','2026-09-19T12:05:00Z'),event('later','2026-09-19T12:11:00Z')],now);
 assert.deepEqual(result.map(r=>r.title),['soon']);assert.equal(result[0].minutes,5);
});
