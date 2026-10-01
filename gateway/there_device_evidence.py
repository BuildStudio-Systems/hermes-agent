"""Deterministic delivery for explicit THERE device-tool requests.

Model prose is not execution evidence. This gate covers requests naming
there_devices plus an action. Ordinary conversations keep streaming normally.
Only the real plugin can record a response; prior messages are never consulted.
"""
import copy
import json
import logging
import re
import threading

logger = logging.getLogger(__name__)


# Python's \b treats CJK characters as word characters, so "用there_devices检查"
# never matched \bthere_devices\b. Use ASCII-only boundaries and accept the
# owner's Chinese/Japanese action verbs as well as the English action names.
_TOOL_NAME = re.compile(r'(?<![A-Za-z0-9_])there_devices(?![A-Za-z0-9_])', re.I)
_ACTION = re.compile(
    r'(?<![A-Za-z0-9_])(?:list|inspect|operate|propose|job)(?![A-Za-z0-9_])'
    r'|列出|检查|查看|诊断|执行|运行|重启|启动|停止|提案'
    r'|一覧|確認|診断|実行|再起動|起動|停止|提案', re.I)


def requires_device_evidence(message):
    return isinstance(message, str) and bool(_TOOL_NAME.search(message) and _ACTION.search(message))


class DeviceEvidence:
    def __init__(self):
        self._records = []
        self._lock = threading.Lock()
        self._stream = None
        self._stream_message = ''
        self._stream_count = 0

    def start_stream(self, message, callback):
        """Attach the gateway's nonblocking delta sink for this turn only."""
        with self._lock:
            self._stream_message = message
            self._stream = callback
            self._emit_locked(final=False)

    def finish_stream(self):
        with self._lock:
            self._emit_locked(final=True)
            self._stream = None

    def _emit_locked(self, *, final):
        if self._stream is None:
            return
        records = self._records[self._stream_count:]
        if not records and (not final or self._stream_count):
            return
        rendered = self._render_records(self._stream_message, records)
        # Each receipt is a complete fenced block. Later receipts append to
        # the same introduction; concatenated deltas equal the saved answer.
        if self._stream_count:
            rendered = '\n\n' + rendered.split('\n\n', 1)[1]
        self._stream_count = len(self._records)
        try:
            self._stream(rendered)
        except Exception:
            # Transport failure must not turn an already-completed operation
            # into a tool error that the model might retry. Preserve evidence.
            self._stream = None
            logger.warning('Device receipt stream unavailable; evidence retained')

    def record(self, arguments, response):
        # Store only a current plugin invocation, never a supplied transcript.
        with self._lock:
            self._records.append((copy.deepcopy(arguments), copy.deepcopy(response)))
            self._emit_locked(final=False)
            return self._stream is not None

    def render(self, message):
        with self._lock:
            records = copy.deepcopy(self._records)
        return self._render_records(message, records)

    @staticmethod
    def _render_records(message, records):
        if re.search(r'[\u3040-\u30ff]', message):
            intro = '今回のデバイスツール実行記録です。以下にない要求の結果は未確認です。'
            missing = '今回はデバイスツールの実行記録がありません。デバイスの現在の状態や操作の成功は確認できません。推測した結果は表示しません。再実行は行っていません。'
        elif re.search(r'[\u3400-\u9fff]', message):
            intro = '以下为本轮设备工具的实际回执；未列出的请求结果尚未确认。登记的操作数量不代表全部操作均已测试。'
            missing = '本轮没有设备工具调用回执，无法确认设备当前状态或操作成功。已拦截未经验证的模型回答，未自动重试。'
        else:
            intro = 'Actual device-tool receipts for this turn follow. Any requested result not listed remains unverified. Registered recipe counts are not counts of tested operations.'
            missing = 'No device-tool receipt was recorded this turn. Current device state or operation success cannot be verified. Unverified model prose was withheld; no automatic retry was performed.'
        if not records:
            return missing
        rendered = [intro]
        for args, response in records:
            request = {k: args[k] for k in ('action', 'device', 'operation', 'job') if k in args}
            # The broker already restricts inventory and diagnostic output.
            # Do not repeat full proposed scripts in a chat execution receipt.
            data = {k: response[k] for k in (
                'device_count', 'operation_count', 'devices', 'error', 'reason', 'status',
                'state', 'exit_code', 'output', 'truncated', 'snapshot',
                'id', 'device', 'description', 'result', 'review_url') if k in response}
            if data.get('state') == 'pending':
                data['execution_confirmed'] = False
                data['review_url'] = '/api/v1/device-control/console'
            body = json.dumps({'request': request, 'response': data}, ensure_ascii=False, indent=2)
            # A diagnostic string is not a request to publish a local artifact.
            body = body.replace('MEDIA:', '\\u004dEDIA:')
            if len(body) > 100000:
                body = json.dumps({'request': request, 'receipt_too_large': True,
                                   'result_unverified_in_chat': True})
            longest = max((len(m.group()) for m in re.finditer(r'`+', body)), default=0)
            fence = '`' * max(3, longest + 1)
            rendered.append(fence + 'json\n' + body + '\n' + fence)
        return '\n\n'.join(rendered)


def verified_result(result, evidence, message):
    """Replace only this turn's final delivery, keeping prior history intact."""
    final = evidence.render(message)
    updated = dict(result)
    updated['final_response'] = final
    messages = result.get('messages')
    if isinstance(messages, list) and messages:
        updated['messages'] = list(messages)
        last = messages[-1]
        if isinstance(last, dict) and last.get('role') == 'assistant' and not last.get('tool_calls'):
            updated['messages'][-1] = {**last, 'content': final}
    return updated
