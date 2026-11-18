from collections import defaultdict
RANK={'PASS':0,'UNCERTAIN':1,'FAIL':2}

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine']);accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    batches=defaultdict(list)
    for r in state['calculations']:batches[r['batch']].append(r)
    grades=[{'batch':b,'code':max((r['code'] for r in rows),key=lambda c:RANK[c]),'projects':len(rows),'samples':len({r['sample_id'] for r in rows})} for b,rows in sorted(batches.items())]
    # 组级判级按 FAIL>UNCERTAIN>PASS 取最高风险；待更新组不掩盖已出具的风险，但单独计数。
    group_grades=[]
    group_batches=defaultdict(list)
    for g in state.get('group_verdicts',[]):group_batches[g['batch']].append(g)
    for b,rows in sorted(group_batches.items()):
        coded=[g for g in rows if g.get('code')]
        group_grades.append({'batch':b,'code':max((g['code'] for g in coded),key=lambda c:RANK[c]) if coded else 'PENDING',
            'groups':len(rows),'pending':sum(g['status']=='PENDING' for g in rows)})
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],'accounted':accounted,'balanced':accounted==state['input_count'],'business':state['business'],'group_business':state.get('group_business',{'total':0,'pass':0,'fail':0,'uncertain':0,'pending':0,'unit':'残留物组'}),'group_grades':group_grades,'grades':grades}
