export function values(form){return Object.fromEntries(new FormData(form))}
export function feedback(text){let el=document.querySelector('#error')||document.querySelector('#claims-error');if(el)el.textContent=text||''}
