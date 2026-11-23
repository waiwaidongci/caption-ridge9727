import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {ROUND, TRIAL} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const SCHEME_KEY_LABEL = item => `${item.label}（${item.key}）`;

function schemeOptions(schemes, selected) {
  return el('select', {required:true}, schemes.map(item =>
    el('option', {value:item.key, selected:item.key === selected}, SCHEME_KEY_LABEL(item))));
}

function createForm(spec, schemes, refresh) {
  const message = feedback();
  const submit = button('创建试跑', {type:'submit', class:'primary', 'data-testid':'create-trial'});
  const form = el('form', {class:'fields', 'data-testid':'trial-form',
    onSubmit:handler(submit, message, async event => {
      const data = new FormData(event.target);
      const item = await workspace.createTrial({
        name:data.get('name'), dataset_id:Number(data.get('dataset_id')),
        old_scheme_key:data.get('old_scheme_key'), new_scheme_key:data.get('new_scheme_key'),
        round_sizes:[Number(data.get('round1_size')), Number(data.get('round2_size'))],
      });
      await refresh();
      location.hash = `#trial/${item.id}`;
    })},
    field({key:'name', label:'试跑名称', type:'string', max_length:120}, '方案切换试跑'),
    (() => {
      const select = el('select', {name:'dataset_id', required:true},
        workspace.datasets.map(item => el('option', {value:item.id}, `${item.name} · ${item.row_count} 行`)));
      return el('label', {class:'field'}, el('span',{},'数据批次（取其中待审工单入组）'), select);
    })(),
    (() => {
      const select = schemeOptions(schemes.schemes);
      select.name = 'old_scheme_key';
      return el('label', {class:'field'}, el('span',{},'旧方案（对照）'), select,
        el('small',{},'试跑前用全部已完工工单拟合并冻结，预测不可变'));
    })(),
    (() => {
      const select = schemeOptions(schemes.schemes);
      select.name = 'new_scheme_key';
      select.value = schemes.schemes[schemes.schemes.length - 1].key;
      return el('label', {class:'field'}, el('span',{},'新方案（候选）'), select,
        el('small',{},'须与旧方案不同；达标后新工单改用此方案'));
    })(),
    el('div', {class:'form-grid'},
      field({key:'round1_size', label:'第一轮看前多少单', type:'int', min:1, max:500}, 8),
      field({key:'round2_size', label:'第二轮看前多少单', type:'int', min:1, max:500}, 8)),
    p(`按入组顺序（工单日期、工单号）取够两轮单数。每轮封存做单侧精确符号检验，误升风险上限 α=${schemes.alpha}（各 2.5%），平局不计。`, 'subtle'),
    submit, message);
  return panel('新建方案试跑', session.canWrite ? form : p('只读角色可查看下方试跑与判定依据。', 'empty'));
}

function roundPanel(trial, roundItem, refresh) {
  const sealed = roundItem.status === 'sealed';
  const children = [
    el('div', {class:'actions'}, pill(ROUND[roundItem.status], roundItem.status),
      p(`目标 ${roundItem.target_count} 单 · 已收实耗 ${roundItem.filled}/${roundItem.enrolled}`, 'subtle')),
  ];
  if (roundItem.missing_job_ids.length) {
    children.push(el('div', {class:'missing-box', 'data-testid':`missing-r${roundItem.round_no}`},
      p('缺单（实耗未收齐，暂不能封存）'),
      el('ul', {class:'method-list'}, roundItem.missing_job_ids.map(job => el('li', {}, job)))));
  } else if (!sealed) {
    children.push(p('实耗已收齐，可以封存并执行符号检验。', 'subtle'));
  }
  if (sealed && roundItem.result) {
    const r = roundItem.result;
    children.push(panel(`第 ${roundItem.round_no} 轮判定依据`,
      el('dl', {class:'dictionary'},
        el('dt', {}, '结论'), el('dd', {}, r.passed ? pill('达标：新方案更优', 'adopted') : pill('未达标', 'retained')),
        el('dt', {}, '新方案更优 / 旧方案更优 / 平局'), el('dd', {}, `${r.wins} / ${r.losses} / ${r.ties}`),
        el('dt', {}, '可比单数 n'), el('dd', {}, value(r.compared)),
        el('dt', {}, '单侧精确符号检验 p 值'), el('dd', {}, value(r.p_value)),
        el('dt', {}, '门槛 α 与临界胜单数'), el('dd', {}, `${value(r.alpha)} · 至少 ${r.critical_wins ?? '—'} 单新方案更优`)),
      p(r.basis, 'intro-copy')));
  }
  if (session.canWrite && !sealed) {
    const message = feedback();
    const seal = button('封存本轮并判定', {class: roundItem.ready_to_seal ? 'primary' : 'quiet',
      'data-testid':`seal-r${roundItem.round_no}`});
    seal.addEventListener('click', handler(seal, message, async () => {
      if (!roundItem.ready_to_seal) throw new Error('实耗未收齐，缺单：' + roundItem.missing_job_ids.join('、'));
      await workspace.sealRound(trial.id, roundItem.round_no);
      await refresh();
    }));
    children.push(el('div', {class:'actions'}, seal, message));
  }
  return panel(`第 ${roundItem.round_no} 轮`, ...children);
}

function backfillPanel(trial, refresh) {
  if (!session.canWrite) return null;
  const message = feedback();
  const textarea = el('textarea', {rows:6, required:true, placeholder:'工单号,实耗分钟\nN01,52\nN03,68',
    'data-testid':'backfill-input'});
  const submit = button('回填实耗（可乱序）', {type:'submit', class:'primary', 'data-testid':'backfill-submit'});
  const form = el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    event.preventDefault();
    const lines = textarea.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const rows = lines.map(line => {
      const [job_id, actual_minutes] = line.split(',').map(part => part.trim());
      if (!job_id || actual_minutes === undefined) throw new Error(`行格式不正确：${line}`);
      return {job_id, actual_minutes:Number(actual_minutes)};
    });
    if (!rows.length) throw new Error('请先填写要回填的工单号与实耗');
    const result = await workspace.backfillTrial(trial.id, rows);
    message.textContent = `已回填 ${result.applied.length} 单` +
      (result.blocked_sealed.length ? `；封存轮补正被忽略：${result.blocked_sealed.join('、')}` : '');
    await refresh();
  })}, p('每行一条「工单号,实耗分钟」，按工单号匹配，回填顺序不限。封存轮的补正只记录、不改动。', 'subtle'),
    textarea, submit, message);
  return panel('实耗回填', form);
}

function detailView(trialId, refresh) {
  return el('div', {id:'trial-detail'}, (() => {
    const node = el('div', {}, p('加载试跑详情…', 'subtle'));
    workspace.trial(trialId).then(trial => {
      if (!node.isConnected) return;
      const statePill = pill(TRIAL[trial.status], trial.status);
      const verdictLine = trial.status === 'open'
        ? p('试跑进行中：封存轮判定冻结，未封存轮缺单等待、可补正。', 'intro-copy')
        : p(trial.status === 'adopted'
            ? `首次达标已采用新方案：${trial.new_scheme.label}。新工单按新方案估算，已入组试跑工单预测照旧。`
            : '两轮均未达标，沿用旧方案。', 'intro-copy');
      const enrollColumns = [
        {key:'position', label:'入组位次'}, {key:'round_no', label:'轮次'},
        {key:'job_id', label:'工单号'}, {key:'job_date', label:'工单日期'},
        {key:'old_prediction', label:'旧方案预测'}, {key:'new_prediction', label:'新方案预测'},
        {key:'actual_minutes', label:'实耗分钟'},
        {label:'旧/新绝对误差', render:row => row.actual_minutes === null ? '—'
          : `${value(row.old_abs_error)} / ${value(row.new_abs_error)}`},
        {label:'状态', render:row => el('span', {}, row.sealed ? '已封存' : (row.actual_minutes === null ? '缺实耗' : '待封存'),
          row.corrected ? pill('已补正', 'collecting') : null)},
      ];
      const roundColumns = [
        {key:'round_no', label:'轮次'}, {key:'target_count', label:'预定单数'},
        {label:'状态', render:row => pill(ROUND[row.status], row.status)},
        {key:'filled', label:'已收实耗'},
        {label:'缺单', render:row => row.missing_job_ids.length ? row.missing_job_ids.join('、') : '—'},
        {label:'检验 p 值', render:row => row.result ? value(row.result.p_value) : '—'},
        {label:'判定', render:row => !row.result ? '—' : (row.result.passed ? '达标' : '未达标')},
      ];
      node.replaceChildren(
        el('div', {class:'result-head'}, h(2, trial.name), statePill, verdictLine,
          p(`旧方案：${trial.old_scheme.label}　新方案：${trial.new_scheme.label}`, 'subtle'),
          trial.active_deployment && p(`当前生效：${trial.active_deployment.label}（${trial.active_deployment.reason}）`, 'intro-copy')),
        el('div', {class:'two-col'},
          el('div', {class:'wide-panel'}, ...trial.rounds.map(item => roundPanel(trial, item, refresh)),
            panel('入组工单与逐单绝对误差', table(enrollColumns, trial.enrollments,
              {caption:'入组工单（按入组顺序）', pageSize:20}))),
          el('aside', {class:'sidebar-panel'},
            panel('轮次总览', table(roundColumns, trial.rounds, {caption:'两轮判定总览'})),
            backfillPanel(trial, refresh),
            panel('检验口径', el('ul', {class:'method-list'}, [
              '逐单比较绝对误差，新旧相等的平局不计入样本。',
              '每轮用单侧精确二项符号检验，H0：新旧等概率，H1：新方案更优。',
              `p 值 ≤ α=${trial.alpha} 判达标，各轮误升风险限 2.5%。`,
              '并发验收或重试只生效一次：封存幂等，首次达标才切换方案。',
            ].map(text => el('li', {}, text)))))));
      // 开放中的试跑在实耗变化后自动刷新一次状态
      if (trial.status === 'open') {
        poll(() => workspace.trial(trialId), item => item.status !== 'open',
          () => node.isConnected && location.hash === `#trial/${trialId}`)
          .then(item => { if (item && node.isConnected) refresh(); }).catch(() => {});
      }
    }).catch(error => { if (node.isConnected) node.replaceChildren(empty(error.message, '#trial', '返回试跑列表')); });
    return node;
  })());
}

export async function trialPage(route, refresh) {
  const {spec} = workspace;
  const schemes = await workspace.trialSchemes();
  const trials = await workspace.trials();
  if (route.id) {
    return el('div', {}, el('div', {class:'actions'}, link('返回试跑列表', '#trial', {class:'quiet'})),
      detailView(Number(route.id), refresh));
  }
  const listColumns = [
    {label:'试跑', render:item => link(item.name, `#trial/${item.id}`)},
    {label:'状态', render:item => pill(TRIAL[item.status], item.status)},
    {label:'旧方案 → 新方案', render:item => `${item.old_scheme.key} → ${item.new_scheme.key}`},
    {label:'轮次单数', render:item => item.rounds.map(r => r.target_count).join(' / ')},
    {label:'创建时间', render:item => date(item.created_at)},
    {label:'结论', render:item => item.verdict ? TRIAL[item.status] : '进行中'},
  ];
  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      createForm(spec, schemes, refresh),
      panel('试跑记录', table(listColumns, trials, {caption:'方案试跑记录', empty:'还没有试跑，先在上方创建。'}))),
    el('aside', {class:'sidebar-panel'}, panel('试跑规则', el('ul', {class:'method-list'}, [
      '试跑前选定两个不同的已完成方案，用全部已完工工单冻结模型与两轮单数。',
      '待审单按入组顺序取够单数，每单先留存旧、新两份预测，再收实耗。',
      '实耗可乱序回填；缺单时该轮等待，页面列出缺哪一单。',
      '补正只影响未封存轮；封存轮判定冻结。',
      '任一轮首次达标即采用新方案；两轮都没达标沿用旧方案。',
    ].map(text => el('li', {}, text))))));
}
