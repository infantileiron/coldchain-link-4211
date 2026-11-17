/** @typedef {{id:number,code:string,route:string,zone:string,driver:string}} Shipment */
/** @typedef {{id:number,state:'queued'|'running'|'done'|'failed',result:object}} Study */
/** @typedef {{id:number,route:string,effective_month:string,deductible_cents:number,payout_bp:number,cap_cents:number}} Contract */
/** @typedef {{id:number,route:string,month:string,state:'pending'|'settling'|'settled',payable_cents:number,adjustment_cents:number}} Settlement */
