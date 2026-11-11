"""Bốn provider cùng một interface. Không chứa agent loop."""
import http.client
import json
import os
import time
import urllib.error
import urllib.request
import uuid


def _load_dotenv():
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.isfile(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('export '):
                        line = line[7:].strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k:
                            os.environ[k] = v
        except Exception:
            pass


_load_dotenv()

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


def post_json(url, headers, payload, max_retries=2):
    req_headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) StatusAgentLab/1.0',
        **headers,
    }
    if 'opencode.ai' in url and 'x-opencode-session' not in req_headers:
        req_headers['x-opencode-session'] = f'ses_{uuid.uuid4().hex}'

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                err_body = exc.read().decode('utf-8', errors='ignore')
            except Exception:
                err_body = ''
            msg = f'Provider trả HTTP {exc.code}'
            if err_body:
                msg += f': {err_body[:300]}'
            else:
                msg += '; kiểm tra model, key và quota.'
            raise RuntimeError(msg) from exc
        except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError, TimeoutError, OSError) as exc:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f'Không kết nối được provider: {exc}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        model = model or os.getenv('MODEL') or os.getenv('API_MODEL')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, default_url = CONFIG[name]

        custom_endpoint = os.getenv('API_ENDPOINT') or os.getenv('OPENAI_BASE_URL')
        if name == 'openai' and custom_endpoint:
            endpoint = custom_endpoint.rstrip('/')
            if not endpoint.endswith('/chat/completions'):
                self.url = f'{endpoint}/chat/completions'
            else:
                self.url = endpoint
        else:
            self.url = default_url

        self.key = os.getenv(key_name) or (os.getenv('API_KEY') if name == 'openai' else None)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        for attempt in range(3):
            data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
            if isinstance(data, dict) and ('error' in data or 'choices' not in data or not data.get('choices')):
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                err_info = data.get('error') or f'Response thiếu choices: {json.dumps(data, ensure_ascii=False)[:300]}'
                raise RuntimeError(f'Provider trả lỗi: {err_info}')
            break
        choice = data['choices'][0]
        message = choice['message']
        tool_calls = message.get('tool_calls') or []
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in tool_calls]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
