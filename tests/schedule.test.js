import test from 'node:test';
import assert from 'node:assert/strict';
import {upcoming,occurrences} from '../ui/schedule.js';
import {nearbyDining} from '../ui/dining.js';
const event=(id,start,end,extra={})=>({id,title:id,start,end,repeat:'none',...extra});
test('upcoming excludes finished entries and returns three chronologically',()=>{
 const rows=[event('past','2026-09-01T08:00:00Z','2026-09-01T09:00:00Z'),...['12','11','10','09'].map(h=>event(h,`2026-09-02T${h}:00:00Z`,`2026-09-02T${h}:30:00Z`))];
 assert.deepEqual(upcoming(rows,new Date('2026-09-02T08:00:00Z')).map(e=>e.id),['09','10','11']);
});
test('weekly recurrence stops at inclusive end date',()=>{
 const rows=[event('weekly','2026-09-01T12:00:00Z','2026-09-01T13:00:00Z',{repeat:'weekly',repeatUntil:'2026-09-15'})];
 assert.equal(occurrences(rows,new Date('2026-09-01T00:00:00Z'),new Date('2026-10-01T00:00:00Z')).length,3);
});
test('in-progress events remain upcoming while invalid intervals do not',()=>{
 const rows=[event('now','2026-09-01T08:00:00Z','2026-09-01T10:00:00Z'),event('bad','2026-09-01T12:00:00Z','2026-09-01T10:00:00Z')];
 assert.deepEqual(upcoming(rows,new Date('2026-09-01T09:00:00Z')).map(e=>e.id),['now']);
});
test('dining sorts valid locations by distance, missing coordinates last',()=>{
 const rows=nearbyDining([{name:'Far',lat:1,lon:1},{name:'Unknown'},{name:'Near',lat:0,lon:0}],{lat:0,lon:0});
 assert.deepEqual(rows.map(p=>p.name),['Near','Far','Unknown']);assert.equal(rows[0].distance,0);assert.equal(rows[2].distance,null);
});
