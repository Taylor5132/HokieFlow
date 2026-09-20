import test from 'node:test';
import assert from 'node:assert/strict';
import {loadPreferences,savePreferences} from '../ui/preferences.js';
test('preferences tolerate unavailable storage and persist supported options',()=>{
 assert.deepEqual(loadPreferences(undefined),{theme:'light'});
 let value='not json';const store={getItem:()=>value,setItem:(_,v)=>value=v};assert.equal(loadPreferences(store).theme,'light');
 savePreferences(store,{theme:'dark'});assert.deepEqual(loadPreferences(store),{theme:'dark'});
});
