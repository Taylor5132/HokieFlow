import test from 'node:test';
import assert from 'node:assert/strict';
import {importMerge} from '../ui/class-import.js';
const item=(id,start,end,extra={})=>({id,title:id,start,end,repeat:'none',...extra});
test('reimport skips identical meeting IDs and preserves saved events',()=>{const a=item('a','2026-09-21T10:00:00-04:00','2026-09-21T11:00:00-04:00');const r=importMerge([a],[a]);assert.equal(r.added.length,0);assert.equal(r.events.length,1);});
test('overlap detects existing weekly meetings, but not touching boundaries',()=>{const a=item('a','2026-09-14T10:00:00-04:00','2026-09-14T11:00:00-04:00',{repeat:'weekly',repeatUntil:'2026-12-09'});assert.equal(importMerge([a],[item('b','2026-09-21T10:30:00-04:00','2026-09-21T11:30:00-04:00')]).conflicts.length,1);assert.equal(importMerge([a],[item('b','2026-09-21T11:00:00-04:00','2026-09-21T12:00:00-04:00')]).conflicts.length,0);});

// One-click "Add to schedule": the row button used to open the review step.
// These pin the decisions that step used to make by hand.
import {classAddPlan} from '../ui/class-import.js';

const meeting=(id,title,start,end)=>({id,title,start,end,repeat:'none'});
const preview=(events,extra={})=>({events,warnings:[],errors:[],...extra});

test('adding a section saves its meetings and reports how many',()=>{
  const r=classAddPlan([],preview([meeting('cs3114-a','CS 3114','2026-09-21T12:30:00-04:00','2026-09-21T13:45:00-04:00')]));
  assert.equal(r.ok,true);
  assert.equal(r.count,1);
  assert.equal(r.events.length,1);
});

test('adding the same section twice does not duplicate it',()=>{
  const one=meeting('cs3114-a','CS 3114','2026-09-21T12:30:00-04:00','2026-09-21T13:45:00-04:00');
  const r=classAddPlan([one],preview([one]));
  assert.equal(r.ok,false);
  assert.match(r.error,/already in your schedule/);
  assert.equal(r.events.length,1,'existing schedule is left alone');
});

test('a preview error means nothing is saved',()=>{
  const existing=[meeting('keep','Existing','2026-09-21T09:00:00-04:00','2026-09-21T10:00:00-04:00')];
  const r=classAddPlan(existing,preview([meeting('new','New','2026-09-21T11:00:00-04:00','2026-09-21T12:00:00-04:00')],{errors:['Unsupported calendar']}));
  assert.equal(r.ok,false);
  assert.equal(r.events,existing);
  assert.equal(r.error,'Unsupported calendar');
});

test('an overlap is reported as a clash, not as a note',()=>{
  const weekly=meeting('old','CS 2505','2026-09-14T10:00:00-04:00','2026-09-14T11:00:00-04:00');
  weekly.repeat='weekly';weekly.repeatUntil='2026-12-09';
  const r=classAddPlan([weekly],preview([meeting('new','CS 3114','2026-09-21T10:30:00-04:00','2026-09-21T11:30:00-04:00')]));
  assert.equal(r.ok,true);
  assert.equal(r.conflicts.length,1);
  assert.match(r.conflicts[0],/overlaps/);
  assert.equal(r.warnings.length,0,'an overlap is not a provenance note');
});

test('inference notices are kept apart from clashes',()=>{
  const r=classAddPlan([],preview([meeting('new','CS 3114','2026-09-22T12:30:00-04:00','2026-09-22T13:45:00-04:00')],{warnings:['Weekly dates are inferred from the published term.']}));
  assert.equal(r.ok,true);
  assert.equal(r.conflicts.length,0);
  assert.equal(r.warnings.length,1);
});

test('the 200-entry cap still refuses the add',()=>{
  const full=Array.from({length:200},(_,i)=>meeting('e'+i,'Event '+i,'2026-09-21T08:00:00-04:00','2026-09-21T08:30:00-04:00'));
  const r=classAddPlan(full,preview([meeting('new','CS 3114','2026-09-22T12:30:00-04:00','2026-09-22T13:45:00-04:00')]));
  assert.equal(r.ok,false);
  assert.match(r.error,/200 entries/);
  assert.equal(r.events.length,200);
});
