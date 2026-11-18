"""残留物组裁决引擎。

组级结论只能建立在"同一检测轮次"的组分数据之上。检测轮次按复检链深度
（relation 跳数）界定：根报告为第 0 轮（初检），每多一层复检深一轮。

朴素地"取每个组分最新值"会把不同复检批次拼成一份组结论（跨批拼凑），
等整组重测又会卡住已到出证时点的批次。本模块的规则：

* 全部组分的采纳报告处于同一深度  -> 以该轮次重算组总量并裁决；
* 组分深度不齐（部分已进入更深复检批）
  -> 不跨批拼凑；取所有组分共同覆盖的最深一轮还原组结论并保留，
     同时列出尚未更新到当前轮次的组分；没有任何共同齐套轮次时暂缓出证；
* 复检报告本身不合格（REFERENCE/CONFLICT 等）-> M5 不予采纳，
  组分仍指向旧版采纳报告，旧版组结论原样保留；
  原始组分记录留在隔离签，绝不删除。

区间求和按区间算术：下界和为组下界，上界和为组上界；
任一上界为开区间则组上界开。判定沿用 M4 的 PASS/FAIL/UNCERTAIN 规则。
所有数值使用 Decimal；重跑同源输入得到完全相同的结论（幂等）。
"""
import csv
from decimal import Decimal
from pathlib import Path

INITIAL = 'INITIAL'
RETEST = 'RETEST'


def group_definitions():
    path = Path(__file__).resolve().parents[3] / 'sample_data' / 'groups.csv'
    with path.open(encoding='utf-8-sig', newline='') as f:
        defs = []
        for row in csv.DictReader(f):
            defs.append({
                'id': row['id'],
                'product': row['product'],
                'analytes': tuple(a for a in row['analytes'].split('|') if a),
                'limit': str(Decimal(row['limit_mgkg'])),
            })
        return defs


def _dec(value):
    return Decimal(str(value))


def _judge(lower, upper, limit):
    """区间上界≤限量为PASS，下界>限量为FAIL，其余UNCERTAIN。"""
    if upper <= limit:
        return 'PASS'
    if lower > limit:
        return 'FAIL'
    return 'UNCERTAIN'


def _sum_components(reports):
    """对同一轮次的组分做区间求和。"""
    lower = sum((_dec(r['lower']) for r in reports), Decimal(0))
    upper = sum((_dec(r['upper']) for r in reports), Decimal(0))
    upper_exclusive = any(bool(r['upper_exclusive']) for r in reports)
    return lower, upper, upper_exclusive


def _parent_id(node):
    # M5 采纳报告用 relation，计算行用 replaces；两者等价。
    parent = node.get('replaces')
    return parent if parent else node.get('relation')


def _chain(current, roots):
    """沿 relation 返回 [根报告, ..., 当前报告] 的复检链（深度=len-1）。"""
    chain = []
    seen = set()
    node = current
    while node is not None and node['id'] not in seen:
        seen.add(node['id'])
        chain.append(node)
        parent_id = _parent_id(node)
        node = roots.get(parent_id) if parent_id else None
    chain.reverse()
    return chain


def _round_label(depth):
    return INITIAL if depth == 0 else RETEST


def _component_payload(analyte, report, depth):
    if report is None:
        return {'analyte': analyte, 'report_id': None, 'round': None, 'depth': None,
                'lower': None, 'upper': None, 'upper_exclusive': False,
                'code': None, 'replaces': None, 'line': None}
    return {
        'analyte': analyte,
        'report_id': report['id'],
        'round': _round_label(depth),
        'depth': depth,
        'lower': str(report['lower']),
        'upper': str(report['upper']),
        'upper_exclusive': bool(report['upper_exclusive']),
        'code': report['code'],
        'replaces': _parent_id(report),
        'line': report['line'],
    }


def _verdict(reports, limit):
    lower, upper, exclusive = _sum_components(reports)
    return lower, upper, exclusive, _judge(lower, upper, limit)


def adjudicate_groups(chosen, definitions, roots=None):
    """返回 (group_verdicts, group_business)。

    chosen: M5 select_reports 选出的每份样本/项目的采纳报告，
    带 id/line/sample_id/batch/product/analyte/method、
    lower/upper/upper_exclusive/limit/code/replaces 字段。
    roots: 按报告ID索引的全部有效报告（含被复检替代的历史报告），
    用于还原共同齐套轮次的组结论；不传则只依据 chosen。
    """
    roots = roots or {}
    verdicts = []
    sample_ids = sorted({r['sample_id'] for r in chosen})
    for definition in definitions:
        limit = _dec(definition['limit'])
        analytes = definition['analytes']
        for sample_id in sample_ids:
            latest = {}
            for r in chosen:
                if (r['sample_id'] == sample_id
                        and r['product'] == definition['product']
                        and r['analyte'] in analytes):
                    # 每个样本/组分在 M5 之后恰有一条采纳报告
                    latest[r['analyte']] = r
            if not latest:
                continue

            # 每个组分的复检链与当前深度
            chains = {a: _chain(latest[a], roots) for a in latest}
            depths = {a: len(chains[a]) - 1 for a in latest}
            current_depth = max(depths.values())
            missing = [a for a in analytes if a not in latest]
            # 所有组分共同覆盖的最深轮次
            common_depth = min(depths.values()) if not missing else -1

            components = [_component_payload(a, latest.get(a), depths.get(a))
                          for a in analytes]
            present = [latest[a] for a in analytes if a in latest]
            base = {
                'group': definition['id'],
                'product': definition['product'],
                'sample_id': sample_id,
                'batch': present[0]['batch'],
                'limit': str(limit),
                'analytes': list(analytes),
                'components': components,
                'current_depth': current_depth,
                'basis_report_ids': sorted(r['id'] for r in present),
            }

            if not missing and common_depth == current_depth:
                # 全部组分同轮次齐套：按本轮采纳报告重算
                lower, upper, exclusive, code = _verdict(present, limit)
                base.update({
                    'status': 'UPDATED',
                    'round': _round_label(current_depth),
                    'depth': current_depth,
                    'code': code,
                    'lower': str(lower),
                    'upper': str(upper),
                    'upper_exclusive': exclusive,
                    'pending_analytes': [],
                    'retest_analytes': sorted(
                        a for a, d in depths.items() if d > 0),
                    'change_note': ('全部组分同轮次齐套，按本轮检测报告重算'
                                    if current_depth > 0
                                    else '全部组分初检齐套，按初检报告裁决'),
                })
                verdicts.append(base)
                continue

            # 深度不齐或组分缺失：不允许跨批拼凑。
            lagging = sorted(a for a, d in depths.items() if d < current_depth)
            if common_depth >= 0:
                # 还原共同齐套轮次：取每条链在 common_depth 处的报告
                common_reports = [chains[a][common_depth] for a in analytes if a in chains]
                if len(common_reports) == len(analytes):
                    lower, upper, exclusive, code = _verdict(common_reports, limit)
                    base.update({
                        'status': 'PENDING',
                        'round': _round_label(common_depth),
                        'depth': common_depth,
                        'code': code,
                        'lower': str(lower),
                        'upper': str(upper),
                        'upper_exclusive': exclusive,
                        'pending_analytes': lagging + missing,
                        'retest_analytes': sorted(
                            a for a, d in depths.items() if d > common_depth),
                        'basis_report_ids_initial': sorted(r['id'] for r in common_reports),
                        'change_note': '组分复检未齐套，保留第%d轮齐套组结论，不跨复检批拼凑' % common_depth,
                    })
                    verdicts.append(base)
                    continue

            base.update({
                'status': 'PENDING',
                'round': RETEST if current_depth > 0 else INITIAL,
                'depth': current_depth,
                'code': None,
                'lower': None,
                'upper': None,
                'upper_exclusive': False,
                'pending_analytes': lagging + missing,
                'retest_analytes': sorted(a for a, d in depths.items() if d > 0),
                'change_note': '组分组分未齐套，暂不出具组级结论',
            })
            verdicts.append(base)

    business = {
        'total': len(verdicts),
        'pass': sum(v['code'] == 'PASS' for v in verdicts),
        'fail': sum(v['code'] == 'FAIL' for v in verdicts),
        'uncertain': sum(v['code'] == 'UNCERTAIN' for v in verdicts),
        'pending': sum(v['status'] == 'PENDING' for v in verdicts),
        'unit': '残留物组',
    }
    return verdicts, business
