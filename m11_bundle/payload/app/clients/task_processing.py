"""worker 专用模型适配：每个持久化预留对应一次 SDK 请求，SDK 重试关闭。"""

import json
import os

from app.llm import make_client
from app.report_extract import PROMPT
from app.task_policy import TaskFailure


class ModelAdapter:
    def fields(self, prefix, content, timeout):
        model = os.environ[prefix + '_MODEL']
        options = {'extra_body': {'enable_thinking': False}} if model.lower().startswith('qwen3') else {}
        with make_client(prefix) as client:
            response = client.with_options(timeout=timeout, max_retries=0).chat.completions.create(
                model=model, messages=[{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': content}],
                temperature=0, max_tokens=6500, response_format={'type': 'json_object'}, **options)
        try:
            if not response.choices or response.choices[0].finish_reason != 'stop':
                raise ValueError('truncated')
            fields = json.loads(response.choices[0].message.content or '')
            if not isinstance(fields, dict):
                raise ValueError('not object')
        except (ValueError, TypeError):
            raise TaskFailure('invalid_model_output', usage=response.usage.model_dump() if response.usage else None) from None
        return {'fields': fields, 'model': model}, response.usage.model_dump() if response.usage else None

    def embedding(self, content, timeout):
        dimensions = int(os.getenv('EMBEDDING_DIMENSIONS', '1024'))
        model = os.environ['EMBEDDING_MODEL']
        with make_client('EMBEDDING') as client:
            response = client.with_options(timeout=timeout, max_retries=0).embeddings.create(
                model=model, input=[content], dimensions=dimensions, encoding_format='float')
        if len(response.data) != 1 or response.data[0].index != 0 or len(response.data[0].embedding) != dimensions:
            raise TaskFailure('invalid_embedding', usage=response.usage.model_dump() if response.usage else None)
        from app.rag import vector_literal
        vector_literal(response.data[0].embedding)
        return {'vector': response.data[0].embedding}, response.usage.model_dump() if response.usage else None
