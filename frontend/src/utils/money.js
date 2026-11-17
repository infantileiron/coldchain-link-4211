/** 金额一律以“分”存储与传输，展示时换算成元并保留两位。 */
export const yuan=cents=>cents==null?'—':(cents/100).toFixed(2);
export const bp=value=>value==null?'—':(value/100).toFixed(2)+'%';
export const monthOf=()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`};
