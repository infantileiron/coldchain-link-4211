import {state} from '../stores/session.js';
import {esc,num} from '../utils/html.js';
import {yuan,bp} from '../utils/money.js';
import {badge} from '../components/common/status.js';
import {field} from '../components/common/fields.js';
import {CLAIM_STATE_LABELS,ADJUSTMENT_STATE_LABELS} from '../constants/enums.js';

const pct=v=>v==null?'—':v.toFixed(2)+'%';
const stateBadge=s=>badge(CLAIM_STATE_LABELS[s]||s);

function monthPicker(){
 return `<label>结算月<select id=claim-month>${state.claims.months.map(m=>`<option value="${esc(m)}" ${m===state.claims.month?'selected':''}>${esc(m)}</option>`).join('')||'<option>暂无月份</option>'}</select></label>`;
}

function board(){
 let c=state.claims;if(!c)return '<p class=notice>暂无结算数据。先在样本质检中登记货值与结算月。</p>';
 let routes=c.routes.filter(r=>r.sample_count||r.settlement||r.adjustments_pending_cents||r.adjustments_deferred_cents);
 if(!routes.length)return '<p class=notice>该月没有需要核算的线路。</p>';
 return `<div class=scroll><table><thead><tr><th>线路</th><th>样本</th><th>损失合计</th><th>免赔额</th><th>赔付比例</th><th>封顶</th><th>合同应付</th><th>待结转差额</th><th>应付/预计赔款</th><th>状态</th><th></th></tr></thead><tbody>${routes.map(r=>{
  // 已结账月份展示冻结口径，未结账展示当前重算预览
  let k=r.settlement||r.computed;
  let trusted=r.settlement?'已结冻结':'核算预览';
  let act=r.settlement?`<button data-claim=${r.settlement.id}>查看结算单</button>`:
    (r.blockers.length?`<span class=tag>受阻</span>`:
      (state.user.role==='admin'?`<button class=primary data-settle-route="${esc(r.route)}">结账</button>`:'<span class=tag>待结账</span>'));
  let pending=r.settlement?0:r.adjustments_pending_cents;
  let deferred=r.settlement?0:r.adjustments_deferred_cents;
  let pendingCell=yuan(pending)+(deferred?` <span class=tag title="负差额只抵到 0，剩余顺延到下一个开放月份">顺延 ${yuan(deferred)}</span>`:'');
  let total=r.settlement?r.settlement.payable_cents:r.payable_preview_cents;
  return `<tr title="${trusted}"><td>${esc(r.route)}</td><td>${k?k.sample_count:r.sample_count}</td><td>${k?yuan(k.gross_loss_cents):'—'}</td><td>${k?yuan(k.deductible_applied_cents):'—'}</td><td>${bp(r.contract?.payout_bp)}</td><td>${k?yuan(k.cap_applied_cents):'—'}</td><td>${k?yuan(k.base_payable_cents):'—'}</td><td>${pendingCell}</td><td>${yuan(total)}</td><td>${r.settlement?stateBadge(r.settlement.state):badge('待核算')}</td><td>${act}</td></tr>`;
 }).join('')}</tbody></table></div>`;
}

function detail(){
 let d=state.claimDetail;if(!d)return '<p class=notice>选择一张结算单查看损失、免赔、封顶与分摊明细。</p>';
 let lines=d.lines;
 let samples=lines.filter(l=>l.kind==='sample'),adjusts=lines.filter(l=>l.kind==='adjustment');
 return `<article class=panel><h3>${esc(d.route)} · ${esc(d.month)} 结算单 #${d.id} ${stateBadge(d.state)}</h3>
 <div class=metrics><div><strong>${yuan(d.gross_loss_cents)}</strong>样本损失合计</div><div><strong>−${yuan(d.deductible_applied_cents)}</strong>抵免赔额</div><div><strong>${yuan(d.after_ratio_cents)}</strong>比例赔付</div><div><strong>−${yuan(d.cap_applied_cents)}</strong>封顶截断</div><div><strong>${yuan(d.payable_cents)}</strong>应付</div></div>
 <p class=notice>合同：免赔 ${yuan(d.deductible_cents)} / 赔付 ${bp(d.payout_bp)} / 封顶 ${yuan(d.cap_cents)}。本单样本赔款 ${yuan(d.base_payable_cents)}，结转差额 ${yuan(d.adjustment_cents)}。</p>
 <div class=scroll><table><thead><tr><th>类型</th><th>样本号</th><th>货值</th><th>货损率</th><th>损失</th><th>免赔分摊</th><th>比例赔付</th><th>封顶分摊</th><th>赔款</th><th>来源</th></tr></thead><tbody>${lines.map(l=>`<tr><td>${l.kind==='adjustment'?'差额调整':'样本'}</td><td>${esc(l.code)}</td><td>${l.kind==='sample'?yuan(l.cargo_value_cents):'—'}</td><td>${l.kind==='sample'?pct(l.loss_rate):'—'}</td><td>${yuan(l.gross_cents)}</td><td>${l.kind==='sample'?yuan(l.deductible_share_cents):'—'}</td><td>${l.kind==='sample'?yuan(l.after_ratio_cents):'—'}</td><td>${l.kind==='sample'?yuan(l.cap_share_cents):'—'}</td><td>${yuan(l.payable_cents)}</td><td>${l.source_month?`追溯 ${esc(l.source_month)} 单 #${esc(l.source_settlement_id)}`:'—'}</td></tr>`).join('')}
 </tbody><tfoot><tr><th colspan=8>分摊合计（样本 ${samples.length} 行 + 调整 ${adjusts.length} 行）</th><th>${yuan(lines.reduce((a,l)=>a+l.payable_cents,0))}</th><th>${lines.reduce((a,l)=>a+l.payable_cents,0)===d.payable_cents?'与应付一致':'存在差额'}</th></tr></tfoot></table></div></article>`;
}

function blockers(){
 let c=state.claims;if(!c)return '';
 let rows=c.routes.filter(r=>((r.blockers||[]).length||(r.frozen_missing||[]).length)&&r.sample_count);
 if(!rows.length)return '';
 return `<article class=panel><h3>阻止结账与已结数据缺口</h3><p class=notice>缺失货损或货值的样本不会被静默删除，必须补齐后才能结账；已结月份变缺失的样本保留原赔款，暂不生成冲减。</p><ul>${rows.map(r=>{let notes=(r.blockers||[]).slice();if((r.frozen_missing||[]).length)notes.push('已结月份数据缺失：'+r.frozen_missing.join('、'));return `<li>${esc(r.route)}：${notes.map(b=>esc(b)).join('；')}</li>`}).join('')}</ul></article>`;
}

function adjustments(){
 if(!state.adjustments.length)return '';
 return `<article class=panel><h3>更正差额流水</h3><div class=scroll><table><thead><tr><th>线路</th><th>来源月</th><th>样本</th><th>原赔款</th><th>重算赔款</th><th>差额</th><th>转入月</th><th>状态</th><th>入账单</th></tr></thead><tbody>${state.adjustments.map(a=>`<tr><td>${esc(a.route)}</td><td>${esc(a.source_month)}</td><td>${esc(a.sample_code)}</td><td>${yuan(a.old_payable_cents)}</td><td>${yuan(a.new_payable_cents)}</td><td>${yuan(a.delta_cents)}</td><td>${esc(a.target_month)}</td><td>${badge(ADJUSTMENT_STATE_LABELS[a.status]||a.status)}</td><td>${a.booked_settlement_id?'#'+a.booked_settlement_id:'—'}</td></tr>`).join('')}</tbody></table></div></article>`;
}

function unassigned(){
 let rows=state.claims?.unassigned||[];if(!rows.length)return '';
 return `<article class=panel><h3>未归属结算月</h3><p class=notice>这些样本有损失或货值但没有结算月，不参与任何月度结账。请在样本质检中补登结算月。</p><div class=scroll><table><thead><tr><th>样本号</th><th>线路</th><th>货值</th><th>货损率</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.code)}</td><td>${esc(r.route)}</td><td>${yuan(r.cargo_value_cents)}</td><td>${pct(r.loss)}</td></tr>`).join('')}</tbody></table></div></article>`;
}

function contracts(){
 if(!state.contracts.length)return '<p class=notice>尚无合同。管理员需先按线路登记生效月合同。</p>';
 return `<div class=scroll><table><thead><tr><th>线路</th><th>生效月</th><th>免赔额</th><th>赔付比例</th><th>封顶</th><th>备注</th></tr></thead><tbody>${state.contracts.map(c=>`<tr><td>${esc(c.route)}</td><td>${esc(c.effective_month)}</td><td>${yuan(c.deductible_cents)}</td><td>${bp(c.payout_bp)}</td><td>${yuan(c.cap_cents)}</td><td>${esc(c.note)}</td></tr>`).join('')}</tbody></table></div>`;
}

export function render(){
 let c=state.claims;
 let admin=state.user.role==='admin';
 return `<div class=split><section class=panel>
  <h2>线路月度索赔</h2>
  <p>登记样本货值与结算月。月度损失先抵合同免赔额，再乘赔付比例并封顶；赔款逐样本分摊到分，分摊合计与应付严格相等。已结账月份冻结，更正差额转入下一开放月份并追溯原单。</p>
  ${c?monthPicker():''}
  <p><button id=claims-refresh>重新核算</button> <button id=claims-resume ${admin?'':'disabled'}>续跑未完成结账</button></p>
  <p id=claims-error class=error></p>
  ${board()}
  ${blockers()}
  ${detail()}
  ${adjustments()}
  ${unassigned()}
 </section><aside class=side>
  ${admin?`<h2>登记合同</h2><form id=contract>
   ${field('线路','route','text','required placeholder="鲜达线路 01"')}
   ${field('生效月','effective_month','month','required')}
   ${field('月度免赔额（元）','deductible','number','min=0 step=0.01 required')}
   ${field('赔付比例 %','payout','number','min=0 max=100 step=0.01 required')}
   ${field('月度封顶（元）','cap','number','min=0 step=0.01 required')}
   ${field('备注','note','text')}
   <button class=primary>保存合同版本</button></form>
   <p class=eyebrow>合同按月生效，结账取生效月不晚于结算月的最新版本。</p>`:'<h2>合同</h2><p>仅管理员可登记合同与结账。</p>'}
  <h3>合同档案</h3>${contracts()}
 </aside></div>`;
}
