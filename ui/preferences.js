const KEY='hokieflow.preferences';
export function loadPreferences(storage){try{const p=JSON.parse(storage.getItem(KEY)||'{}');return {theme:p.theme==='dark'?'dark':'light'};}catch{return {theme:'light'};}}
export function savePreferences(storage,prefs){try{storage.setItem(KEY,JSON.stringify({theme:prefs.theme==='dark'?'dark':'light'}));}catch{/* Private browsing may disable persistence. */}}
