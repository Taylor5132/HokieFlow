import test from 'node:test';
import assert from 'node:assert/strict';
import {importMerge} from '../ui/class-import.js';
const item=(id,start,end,extra={})=>({id,title:id,start,end,repeat:'none',...extra});
test('reimport skips identical meeting IDs and preserves saved events',()=>{const a=item('a','2026-09-21T10:00:00-04:00','2026-09-21T11:00:00-04:00');const r=importMerge([a],[a]);assert.equal(r.added.length,0);assert.equal(r.events.length,1);});
test('overlap detects existing weekly meetings, but not touching boundaries',()=>{const a=item('a','2026-09-14T10:00:00-04:00','2026-09-14T11:00:00-04:00',{repeat:'weekly',repeatUntil:'2026-12-09'});assert.equal(importMerge([a],[item('b','2026-09-21T10:30:00-04:00','2026-09-21T11:30:00-04:00')]).conflicts.length,1);assert.equal(importMerge([a],[item('b','2026-09-21T11:00:00-04:00','2026-09-21T12:00:00-04:00')]).conflicts.length,0);});
