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
        result, usage = self.embeddings([content], timeout)
        return {'vector': result['vectors'][0]}, usage

    def embeddings(self, contents, timeout):
        if not 1 <= len(contents) <= 8 or any(not isinstance(t, str) or not t.strip() for t in contents):
            raise TaskFailure('invalid_embedding_input')
        dimensions = int(os.getenv('EMBEDDING_DIMENSIONS', '1024'))
        model = os.environ['EMBEDDING_MODEL']
        with make_client('EMBEDDING') as client:
            response = client.with_options(timeout=timeout, max_retries=0).embeddings.create(
                model=model, input=contents, dimensions=dimensions, encoding_format='float')
        usage = response.usage.model_dump() if response.usage else None
        from app.rag import vector_literal
        try:
            if len(response.data) != len(contents):
                raise ValueError('count')
            indices = [item.index for item in response.data]
            if any(type(i) is not int for i in indices) or set(indices) != set(range(len(contents))):
                raise ValueError('indices')
            ordered = sorted(response.data, key=lambda item: item.index)
            for item in ordered:
                if len(item.embedding) != dimensions:
                    raise ValueError('dimensions')
                vector_literal(item.embedding)
        except (ValueError, TypeError, AttributeError):
            # Reject the whole batch; no silent single-item fallback that hides extra calls.
            raise TaskFailure('invalid_embedding', usage=usage) from None
        return {'vectors': [item.embedding for item in ordered]}, usage
