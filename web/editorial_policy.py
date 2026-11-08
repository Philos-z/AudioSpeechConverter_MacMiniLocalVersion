"""Frozen per-job editorial standards; defaults never migrate existing jobs.

The fluency policy changes the text acceptance goal, not evidence authority,
source binding, model-call budgets, user holds or any audio approval.
"""
import hashlib
import json
from pathlib import Path
import unicodedata

STRICT = 'strict_v1'
FLUENCY = 'fluency_first_v1'
POLICIES = frozenset({STRICT, FLUENCY})
FLUENCY_DESCRIPTION = {
    'schema': 'editorial_policy_v1', 'editorial_policy': FLUENCY,
    'decision_maker': 'proxy', 'ask_for_editorial_choices': False,
    'meaning_deviation_allowed': True, 'semantic_equivalence_required': False,
    'source_identity_and_row_bindings_preserved': True,
    'actual_machine_review_required': True, 'human_listening_inferred': False,
}
FLUENCY_GUIDANCE = (
    '本任务已冻结用户授权的fluency_first_v1流畅优先策略。以中文自然、通顺、连贯为首要标准，'
    '由你自主决定意译、简化、取舍和专名的自然表达，允许与原文有意义偏差。'
    '源文只作理解参考，不要求逐字或完整事实等价；源语含糊、专名拼写、数字、单位、否定、条件或事实差异本身不构成拒绝理由，'
    '不要因此要求用户确认原词、额外识别或逐项取舍。只有明显乱码、无法理解、不通顺或当前中文跨句自相矛盾等实质流畅性问题才拒绝。'
    '必须保留每个指定id与其源行、说话人和时间绑定，不得新增、重复、遗漏或挪动id，不得改造原文或把机器检查称为人工听音。'
    '已有批准不代表本次批准；实际不通过必须如实approved=false并记录问题，不能伪造事实完整保留或人工认可。'
)


def normalize(value=None):
    if value is None:
        return STRICT
    if not isinstance(value, str) or value not in POLICIES:
        raise ValueError('未知或未明确冻结的编辑策略')
    return value


def _path(root, value):
    root=Path(root).absolute();value=Path(value)
    if '..' in value.parts:
        raise ValueError('编辑策略路径越界')
    path=value if value.is_absolute() else root/value
    if not path.is_relative_to(root):
        raise ValueError('编辑策略路径不属于当前项目')
    current=root
    for part in path.relative_to(root).parts:
        current=current/part
        if current.is_symlink():
            raise ValueError('编辑策略路径不能使用符号链接')
    return path


def default_policy(root):
    """Only server creation uses this; an old job never consults this default."""
    root=Path(root).absolute();path=_path(root,'config/editorial_policy.json')
    if not path.exists():
        return STRICT
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get('schema')!='editorial_policy_defaults_v1':
        raise ValueError('默认编辑策略格式不合法')
    if not isinstance(value.get('default_policy'),str):raise ValueError('默认策略必须显式指定')
    policy=normalize(value['default_policy'])
    if policy == FLUENCY:
        ref=value.get('authorization_ref')
        if (value.get('decision_maker')!='proxy' or value.get('ask_for_editorial_choices') is not False
                or value.get('meaning_deviation_allowed') is not True
                or not isinstance(ref,dict) or set(ref)!={'path','sha256'}):
            raise ValueError('流畅优先默认策略缺少真实授权出处')
        authorization=_path(root,ref['path'])
        if not authorization.is_file() or hashlib.sha256(authorization.read_bytes()).hexdigest()!=ref['sha256']:
            raise ValueError('流畅优先授权文件或SHA已改变')
    return policy


def fields(policy):
    policy=normalize(policy)
    if policy==STRICT:
        return {}
    digest=hashlib.sha256(json.dumps({'description':FLUENCY_DESCRIPTION,'guidance':FLUENCY_GUIDANCE},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {'editorial_policy':policy,'editorial_policy_sha256':digest,
        'editorial_policy_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def check_record(record, policy):
    """An absent field means strict; no approval can cross policy modes."""
    if not isinstance(record,dict) or normalize(record.get('editorial_policy')) != normalize(policy):
        raise ValueError('审校记录与任务冻结编辑策略不一致')
    if normalize(policy)==FLUENCY and any(record.get(k)!=v for k,v in fields(policy).items()):
        raise ValueError('流畅优先审校策略身份不完整')


def text_errors(source, target):
    if not isinstance(source,str) or not source.strip():
        return ['原文源行缺失']
    if not isinstance(target,str) or not 1<=len(target.strip())<=600:
        return ['中文必须是非空且有界的完整字符串']
    if '\ufffd' in target or any(unicodedata.category(c) in {'Cs','Cc'} and c not in '\n\t' for c in target):
        return ['中文含明显损坏字符或控制字符']
    return []


def policy_state_for_folder(folder):
    """Use real task state, or one source-bound formal episode delegation."""
    folder=Path(folder).absolute();root=folder.parent.parent
    if folder.parent!=root/'jobs':raise ValueError('编辑策略任务路径越界')
    state_path=_path(root,Path('web/state/jobs')/(folder.name+'.json'))
    if state_path.exists():
        job=json.loads(state_path.read_text())
        if not isinstance(job,dict) or job.get('id',folder.name)!=folder.name or not isinstance(job.get('options',{}),dict):
            raise ValueError('任务编辑策略身份不合法')
        normalize(job.get('options',{}).get('editorial_policy'))
        return job
    binding_path=_path(root,folder/'editorial_policy_binding.json')
    if not binding_path.exists():return {'id':folder.name,'options':{}}
    value=json.loads(binding_path.read_text())
    if (not isinstance(value,dict) or value.get('schema')!='episode_editorial_policy_binding_v1'
            or value.get('job_id')!=folder.name or not isinstance(value.get('parent_id'),str)
            or type(value.get('episode_index')) is not int or value['episode_index']<1):
        raise ValueError('分集编辑策略绑定不完整')
    parent_id=value['parent_id']
    if parent_id==folder.name or '/' in parent_id or '..' in parent_id:raise ValueError('分集父任务身份不合法')
    parent_path=_path(root,Path('web/state/jobs')/(parent_id+'.json'));parent=json.loads(parent_path.read_text())
    policy=normalize(value.get('editorial_policy'))
    if (parent.get('id')!=parent_id or normalize(parent.get('options',{}).get('editorial_policy'))!=policy
            or parent.get('source_sha256')!=value.get('parent_source_sha256')):
        raise ValueError('分集与父任务冻结策略或原源不符')
    refs={}
    for name in ('episode_plan','clip','episode_asr'):
        ref=value.get(name)
        if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:raise ValueError('分集缺少完整绑定引用')
        path=_path(root,ref['path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
            raise ValueError('分集绑定文件或SHA已改变')
        refs[name]=path
    parent_folder=root/'jobs'/parent_id
    if (refs['episode_plan']!=parent_folder/'episode_plan.json' or refs['clip'].parent!=parent_folder/'episode_inputs'
            or refs['episode_asr'].parent!=parent_folder/'episode_asr'):
        raise ValueError('分集证据不属于指定父任务')
    plan=json.loads(refs['episode_plan'].read_text());canonical=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if (plan.get('source_sha256')!=value['parent_source_sha256']
            or len([e for e in plan.get('episodes',[]) if e.get('episode_index')==value['episode_index']])!=1
            or folder.name!=parent_id+'_e'+str(value['episode_index']).zfill(3)+'_'+canonical[:8]):
        raise ValueError('分集原源、序号或正式计划身份不符')
    source_path=_path(root,folder/'source.json')
    if source_path.exists():
        source=json.loads(source_path.read_text())
        if _path(root,source.get('input',''))!=refs['clip']:raise ValueError('分集当前原片不是绑定clip')
    result={**parent,'id':folder.name,'source_sha256':value['clip']['sha256'],
        '_editorial_parent_id':parent_id,'_editorial_binding':{'path':str(binding_path.relative_to(root)),
            'sha256':hashlib.sha256(binding_path.read_bytes()).hexdigest()}}
    return result


# This resolver never reads global defaults, including for state-less episodes.
def policy_for_folder(folder):
    return normalize(policy_state_for_folder(folder).get('options',{}).get('editorial_policy'))


def assert_unheld(folder):
    """A quality preference never revokes cancellation or a task-specific hold."""
    folder=Path(folder).absolute();root=folder.parent.parent;state=policy_state_for_folder(folder)
    ids={folder.name,state.get('_editorial_parent_id',folder.name)}
    records=[state]
    queue_path=_path(root,'web/state/tagesschau_daily_automation.json')
    if queue_path.exists():
        queue=json.loads(queue_path.read_text())
        records.extend(value for jid,value in queue.get('watched',{}).items() if jid in ids and isinstance(value,dict))
        for key in ('daily_intents','episodes'):
            records.extend(value for value in queue.get(key,{}).values() if isinstance(value,dict) and value.get('job_id') in ids)
        if any(isinstance(value,dict) and value.get('job_id') in ids for value in queue.get('projection',{}).get('active_decisions',[])):
            raise ValueError('任务已有未决用户决定，编辑策略不解除该等待')
    for value in records:
        hold=value.get('user_hold')
        if (value.get('status') in {'canceled','cancelled','paused','user_hold','deferred_by_user'}
                or value.get('cancel_requested_at') or value.get('automatic_resumption_allowed') is False
                or (isinstance(hold,dict) and (hold.get('automatic_resumption_allowed') is False
                    or hold.get('status') in {'deferred_by_user','user_hold','paused'}))):
            raise ValueError('取消或用户暂缓仍有效，流畅优先策略不授权自动接续')
    return state
