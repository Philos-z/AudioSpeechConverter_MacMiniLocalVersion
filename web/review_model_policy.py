"""An explicit, source-bound model choice for this task's content reviews."""
from pathlib import Path
import copy
import website_recovery as recovery

CONFIG='translation/content_review_model.json'
SCHEMA='task_content_review_model_v1'
MODELS={'gpt-6-astra','gpt-6.1-sol','gpt-6-sol'}


def client_factory_for_job(root,folder,base_factory):
    root=Path(root).resolve();folder=Path(folder).resolve()
    if folder!=recovery._safe(root,Path('jobs')/folder.name):
        raise ValueError('审校模型配置不属于当前任务')
    def create(request_root):
        if Path(request_root).resolve()!=root:raise ValueError('审校客户端根目录不匹配')
        client=base_factory(root);path=recovery._safe(folder,CONFIG)
        if not path.is_file():return client
        policy=recovery._read(path)
        if (not isinstance(policy,dict) or set(policy)!={'schema','job_id','input_source_sha256','model','reasoning_effort'}
                or policy.get('schema')!=SCHEMA or policy.get('job_id')!=folder.name
                or policy.get('model') not in MODELS or policy.get('reasoning_effort') not in {'low','medium','high'}):
            raise ValueError('任务审校模型配置不完整或超出明确范围')
        source=recovery._read(recovery._safe(folder,'source.json'));video=Path(source.get('input',''))
        if (not video.is_absolute() or not video.is_relative_to(root)
                or recovery._sha(recovery._safe(root,video.relative_to(root)))!=policy['input_source_sha256']):
            raise ValueError('任务审校模型配置与原片不符')
        client.model=policy['model'];client.reasoning_effort=policy['reasoning_effort']
        client.content_review_policy={'path':str(path.relative_to(root)),'sha256':recovery._sha(path),
                                     'configuration':copy.deepcopy(policy)}
        return client
    return create


def make_client(root,jid,*,purpose='content_review',client_factory=None):
    if purpose not in {'content_review','source_adjudication'}:
        raise ValueError('任务审校用途无效')
    from proxy_client import ProxyClient
    root=Path(root).resolve()
    return client_factory_for_job(root,root/'jobs'/jid,client_factory or ProxyClient)(root)
