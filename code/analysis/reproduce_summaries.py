#!/usr/bin/env python3
"""Recompute descriptive dissertation summaries from exported rows; no model/API calls."""
import argparse,csv,json,math,statistics
from pathlib import Path
from collections import defaultdict
DATA=Path(__file__).resolve().parent/'data'
CONDS=['M0R0','M0R1','M1R0','M1R1']
def read(name):
 with (DATA/name).open(newline='') as f:return list(csv.DictReader(f))
def yes(x):return str(x).lower() in ('true','1')
def mean(xs):return statistics.mean(xs)
def groups(rows,keys):
 d=defaultdict(list)
 for r in rows:d[tuple(r[k] for k in keys)].append(r)
 return d

def reproduce():
 runs=read('t3_runs.csv');items=read('t3_items.csv');resources=read('t3_resources.csv');scores=read('t12_scores.csv');grades=read('relevance_grades.csv')
 assert len(runs)==60 and len(items)==1500 and len(resources)==60 and len(scores)==32 and len(grades)==15
 assert len({r['observation_id'] for r in runs})==60
 ri=groups(items,['observation_id']);res={r['observation_id']:r for r in resources}
 for run in runs:
  obs=run['observation_id'];ir=ri[(obs,)];assert sorted(int(r['item_id']) for r in ir)==list(range(1,26))
  assert all(r['condition_code']==run['condition_code'] for r in ir)
  assert sum(yes(r['correct']) for r in ir)==int(run['correct_items'])
  assert sum(r['submission_state']=='accepted' for r in ir)==int(run['submitted_items'])
  assert int(run['correct_items'])+int(run['submitted_incorrect_items'])+int(run['missing_items'])==25
  assert res[obs]['condition_code']==run['condition_code']
  assert int(res[obs]['correct_items'])==int(run['correct_items'])
  assert int(res[obs]['submitted_items'])==int(run['submitted_items'])
 t3=[];levels=[];rg=groups(runs,['condition_code'])
 for condition in CONDS:
  rr=rg[(condition,)];assert len(rr)==15
  correct=[int(r['correct_items']) for r in rr];submitted=sum(int(r['submitted_items']) for r in rr)
  rs=[res[r['observation_id']] for r in rr]
  t3.append({'condition':condition,'n_runs':15,'correct_mean':mean(correct),'correct_sd':statistics.stdev(correct),'required_slots':375,'submitted':submitted,'correct':sum(correct),'submission_coverage':submitted/375,'accuracy_given_submission':sum(correct)/submitted if submitted else None,'total_tokens_mean':mean(int(r['total_tokens']) for r in rs),'model_calls_mean':mean(int(r['provider_calls']) for r in rs),'cumulative_model_call_seconds_mean':mean(float(r['model_call_seconds']) for r in rs)})
  for level,lo,hi in [('A',1,8),('B',9,17),('C',18,25)]:
   ir=[r for r in items if r['condition_code']==condition and lo<=int(r['item_id'])<=hi]
   c=sum(yes(r['correct']) for r in ir);s=sum(r['submission_state']=='accepted' for r in ir);den=15*(hi-lo+1)
   levels.append({'condition':condition,'level':level,'required_slots':den,'correct':c,'submitted':s,'complete_item_score_rate':c/den,'submission_coverage':s/den,'accuracy_given_submission':c/s if s else None})
 lookup={r['condition']:r['correct_mean'] for r in t3}
 deltas={'architecture_M1_minus_M0':(lookup['M1R0']+lookup['M1R1']-lookup['M0R0']-lookup['M0R1'])/2,'retrieval_R1_minus_R0':(lookup['M0R1']+lookup['M1R1']-lookup['M0R0']-lookup['M1R0'])/2}
 assert math.isclose(deltas['architecture_M1_minus_M0'],3.4,abs_tol=1e-10)
 t12=[];pairs=[]
 for (cohort,task,condition),rr in sorted(groups(scores,['cohort','task','condition']).items()):
  n=5 if cohort=='primary_retrieval' else 3;assert len(rr)==n
  vals=[float(r['final_score']) for r in rr]
  t12.append({'cohort':cohort,'task':task,'condition':condition,'n':n,'final_mean':mean(vals),'final_sd':statistics.stdev(vals),'content_mean':mean(float(r['content_score']) for r in rr),'initial_final_mean':mean(float(r['initial_final_score']) for r in rr),'delivery_pass_n':sum(int(r['delivery_pass']) for r in rr)})
 for (cohort,task,replicate),rr in sorted(groups(scores,['cohort','task','replicate']).items()):
  d={r['condition']:r for r in rr};ref,cmp=('C0','C1') if cohort=='primary_retrieval' else ('M0','M1');assert set(d)=={ref,cmp}
  pairs.append({'cohort':cohort,'task':task,'replicate':replicate,'reference':ref,'comparison':cmp,'final_difference':float(d[cmp]['final_score'])-float(d[ref]['final_score']),'content_difference':float(d[cmp]['content_score'])-float(d[ref]['content_score'])})
 retrieval=[];expected={r['task']:r for r in read('retrieval_metrics.csv')}
 for (task,),rr in sorted(groups(grades,['task']).items()):
  rr.sort(key=lambda r:int(r['rank']));assert [int(r['rank']) for r in rr]==[1,2,3,4,5]
  g=[int(r['grade']) for r in rr];dcg=lambda gs:sum((2**v-1)/math.log2(i+2) for i,v in enumerate(gs))
  idcg=dcg(sorted(g,reverse=True));p=sum(v>0 for v in g)/5;ndcg=dcg(g)/idcg if idcg else 0.;deliv=sum(yes(r['delivered']) for r in rr)
  row={'task':task,'grades':g,'precision_at_5':p,'within_returned_set_ndcg_at_5':ndcg,'delivered_returned_ratio':deliv/5,'directly_useful_returned':sum(v==2 for v in g),'directly_useful_delivered':sum(int(r['grade'])==2 and yes(r['delivered']) for r in rr)}
  for key in ['precision_at_5','within_returned_set_ndcg_at_5','delivered_returned_ratio']:assert math.isclose(row[key],float(expected[task][key]),abs_tol=1e-12)
  retrieval.append(row)
 return {'scope':'Descriptive recalculation from exported data; no rescoring, no confidence-interval reproduction and no raw prompt-delivery re-audit. SD is dispersion, not a confidence interval. Level score rates count completely correct items, not the original 100-point rubric.','t3_conditions':t3,'t3_levels':levels,'t3_contrasts':deltas,'t12_groups':t12,'t12_pairs':pairs,'retrieval':retrieval}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);a=p.parse_args();result=reproduce()
 if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
 print('Validated 60 T3 runs, 1500 run-item rows, 32 T1/T2 scores and 15 relevance labels.')
 for r in result['t3_conditions']:print(f"T3 {r['condition']}: {r['correct_mean']:.4f}/25 correct; coverage {r['submission_coverage']:.2%}")
 for r in result['t12_groups']:print(f"{r['cohort']} {r['task']} {r['condition']}: {r['final_mean']:.4f}/10 (n={r['n']})")
 if a.output:print(f'Wrote {a.output}')
if __name__=='__main__':main()
