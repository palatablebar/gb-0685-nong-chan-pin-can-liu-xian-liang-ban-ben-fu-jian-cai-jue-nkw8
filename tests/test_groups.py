from copy import deepcopy
from decimal import Decimal
import pytest
from app.workflow import run, load
from app.modules.m5_calculate.groups import adjudicate_groups, group_definitions, _judge
from app.modules.m7_archive.export_out import bundle


def state():
    return run(load('group.csv'), {})


def verdict(s, sample):
    return next(v for v in s['group_verdicts'] if v['sample_id'] == sample)


def test_group_definitions_loaded():
    defs = group_definitions()
    assert defs and defs[0]['id'] == 'RG1'
    assert defs[0]['analytes'] == ('GA', 'GB', 'GC')
    assert defs[0]['limit'] == '0.05'


def test_initial_complete_group_adjucated():
    v = verdict(state(), 'G001')
    assert v['status'] == 'UPDATED' and v['round'] == 'INITIAL'
    assert v['code'] == 'PASS'
    assert (v['lower'], v['upper']) == ('0.045', '0.045')
    assert v['basis_report_ids'] == ['G0101', 'G0102', 'G0103']


def test_partial_retest_keeps_previous_group_conclusion():
    # G002：GA、GC 已复检，GB 未更新。单项合格不代表组安全；
    # 不得用 (GA复检 + GB初检 + GC复检) 跨批拼凑，必须保留初检轮次 FAIL。
    s = state()
    v = verdict(s, 'G002')
    assert v['status'] == 'PENDING'
    assert v['code'] == 'FAIL'  # 旧版组结论保留
    assert (v['lower'], v['upper']) == ('0.06', '0.06')  # 初检 0.03+0.02+0.01
    assert v['pending_analytes'] == ['GB']
    assert v['retest_analytes'] == ['GA', 'GC']
    assert v['basis_report_ids_initial'] == ['G0201', 'G0202', 'G0203']
    # 朴素拼凑值 0.01+0.02+0.005=0.035 绝不能出现在组总量上
    assert v['upper'] != '0.035'


def test_full_same_round_retest_recomputes_total():
    # G003：三组分同轮次复检齐套，初检 FAIL 翻转为复检 PASS。
    v = verdict(state(), 'G003')
    assert v['status'] == 'UPDATED' and v['round'] == 'RETEST'
    assert v['code'] == 'PASS'
    assert (v['lower'], v['upper']) == ('0.03', '0.03')
    assert v['basis_report_ids'] == ['G0311', 'G0312', 'G0313']


def test_full_retest_can_turn_pass_into_fail():
    # G004：复检整体升高，组结论由 PASS 变 FAIL，组总量同步变化。
    v = verdict(state(), 'G004')
    assert v['round'] == 'RETEST' and v['code'] == 'FAIL'
    assert v['upper'] == '0.065'


def test_interval_components_sum_with_interval_arithmetic():
    # G005：GC 为 ND=[0,0.005)，组总量是区间 [0.008,0.013)，不能把 ND 当零。
    v = verdict(state(), 'G005')
    assert v['code'] == 'PASS'
    assert v['lower'] == '0.008' and v['upper'] == '0.013'
    assert v['upper_exclusive'] is True
    gc = next(c for c in v['components'] if c['analyte'] == 'GC')
    assert gc['lower'] == '0' and gc['upper'] == '0.005' and gc['upper_exclusive'] is True


def test_failed_retest_report_is_quarantined_and_old_conclusion_kept():
    # G006：复检报告 G0611 悬空引用 -> REFERENCE 隔离；初检组结论原样保留。
    s = state()
    assert any(q['id'] == 'G0611' and q['code'] == 'REFERENCE' for q in s['quarantine'])
    v = verdict(s, 'G006')
    assert v['status'] == 'UPDATED' and v['round'] == 'INITIAL' and v['code'] == 'PASS'
    assert v['basis_report_ids'] == ['G0601', 'G0602', 'G0603']


def test_raw_component_records_never_lost():
    s = state()
    # 被替代的初检根报告仍在 measurements 中；隔离行保留来源行、ID、原因码与原值。
    measurement_ids = {r['id'] for r in s['measurements']}
    assert {'G0201', 'G0301', 'G0311', 'G0611'} <= measurement_ids
    quarantined = next(q for q in s['quarantine'] if q['id'] == 'G0611')
    assert quarantined['line'] and quarantined['raw'].get('result')


def test_group_business_counts():
    s = state()
    b = s['group_business']
    assert b['unit'] == '残留物组'
    assert b['total'] == b['pass'] + b['fail'] + b['uncertain']
    assert b['pending'] == 1 and b['fail'] == 2 and b['pass'] == 4


def test_rerun_same_source_is_idempotent():
    a = run(load('group.csv'), {})
    b = run(load('group.csv'), {})
    assert a['run_id'] == b['run_id']
    assert a['group_verdicts'] == b['group_verdicts']
    assert a['group_business'] == b['group_business']


def test_input_order_independent():
    a = run(load('group.csv'), {}, 5)
    rev = deepcopy(a)
    rev['measurements'].reverse()
    va, ba = adjudicate_groups(a['calculations'], a['group_definitions'],
                               {r['id']: r for r in a['measurements']})
    vb, bb = adjudicate_groups(rev['calculations'], rev['group_definitions'],
                               {r['id']: r for r in rev['measurements']})
    key = lambda rows: sorted((r['sample_id'], r['group'], r['code'], r['lower'], r['upper']) for r in rows)
    assert key(va) == key(vb) and ba == bb


def test_multi_hop_retest_rounds_are_not_patched():
    # G003 三组分都在第1轮复检（v2）；仅 GA 再做第2轮复检（v3）。
    # 深度不齐 -> 保留第1轮齐套组结论 0.03，绝不拼出 0.012+0.01+0.01=0.032。
    from app.modules.m5_calculate.rules import select_reports
    s = run(load('group.csv'), {}, 5)
    measurements = deepcopy(s['measurements'])
    v3 = deepcopy(next(r for r in measurements if r['id'] == 'G0311'))
    v3.update(id='G0399', version='3', reported='2026-08-13', relation='G0311',
              result='0.012', lower='0.012', upper='0.012')
    measurements.append(v3)
    chosen, bad = select_reports(measurements)
    assert bad == {'G0611': 'REFERENCE'}  # 样例自带的悬空复检，与本用例无关
    roots = {r['id']: r for r in measurements if r['id'] not in bad}
    calc = [{'id': r['id'], 'line': r['_line'], 'sample_id': r['sample_id'],
             'batch': r['batch'], 'product': r['product'], 'analyte': r['analyte'],
             'method': r['method'], 'lower': r['lower'], 'upper': r['upper'],
             'upper_exclusive': r['upper_exclusive'], 'limit': r['limit'],
             'code': r['code'], 'replaces': r['relation']} for r in chosen]
    verdicts, _ = adjudicate_groups(calc, s['group_definitions'], roots)
    g3 = next(x for x in verdicts if x['sample_id'] == 'G003')
    assert g3['status'] == 'PENDING' and g3['depth'] == 1
    assert g3['code'] == 'PASS' and g3['upper'] == '0.03'
    assert g3['pending_analytes'] == ['GB', 'GC']
    assert g3['basis_report_ids_initial'] == ['G0311', 'G0312', 'G0313']


def test_judge_rules():
    assert _judge(Decimal('0.04'), Decimal('0.05'), Decimal('0.05')) == 'PASS'
    assert _judge(Decimal('0.06'), Decimal('0.06'), Decimal('0.05')) == 'FAIL'
    assert _judge(Decimal('0.04'), Decimal('0.06'), Decimal('0.05')) == 'UNCERTAIN'


def test_missing_component_is_pending_without_verdict():
    s = run(load('group.csv'), {}, 5)
    # 抽掉 G001 的 GC 采纳报告 -> 组分未齐套，不得出具组结论
    s['calculations'] = [r for r in s['calculations']
                         if not (r['sample_id'] == 'G001' and r['analyte'] == 'GC')]
    verdicts, _ = adjudicate_groups(s['calculations'], s['group_definitions'],
                                    {r['id']: r for r in s['measurements']})
    v = next(x for x in verdicts if x['sample_id'] == 'G001')
    assert v['status'] == 'PENDING' and v['code'] is None
    assert 'GC' in v['pending_analytes']


def test_reconcile_and_archive_include_groups():
    s = state()
    assert s['reconcile']['balanced'] is True
    assert s['reconcile']['group_business']['total'] == 6
    assert any(g['pending'] == 1 for g in s['reconcile']['group_grades'])
    csv_text = bundle(s)
    assert '残留物组' in csv_text
    assert 'RG1/G002' in csv_text
    assert s['archive']['evidence_count'] == (
        len(s['calculations']) + len(s['group_verdicts']) + len(s['quarantine']))


def test_existing_samples_have_no_groups_and_stay_balanced():
    for sample in ('dirty.csv', 'clean.csv'):
        s = run(load(sample), {})
        assert s['group_verdicts'] == []
        assert s['group_business'] == {'total': 0, 'pass': 0, 'fail': 0,
                                       'uncertain': 0, 'pending': 0, 'unit': '残留物组'}
        assert s['reconcile']['balanced'] is True
