"""Terminal-safe text, honest context telemetry and a reserved bottom status row."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading

# Untrusted model/tool/session text must never execute OSC/CSI terminal commands.
_ESCAPES = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]')
_CONTROLS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def safe_text(value: object) -> str:
    return _CONTROLS.sub('', _ESCAPES.sub('', str(value))).replace('\t', '    ')


def terminal_size():
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, AttributeError, ValueError):
        size = shutil.get_terminal_size((80, 24))
    # PTYs such as script(1) may report 0x0 until a terminal attaches.
    return os.terminal_size((size.columns or 80, size.lines or 24))


def item_record(record):
    if not isinstance(record, dict):
        return {}
    if record.get('type') == 'item':
        data = record.get('data')
        record = data.get('Item') if isinstance(data, dict) else None
    return record if isinstance(record, dict) else {}


def positive_int(value):
    try:
        number = int(value)
        return number if number > 0 and not isinstance(value, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def count(value):
    return positive_int(value) or 0


def tokens_label(number):
    return f'{number / 1000:.1f}k' if number >= 1000 else str(number)


class SessionMetrics:
    """Latest request occupancy is NOT the sum of repeated input usage.

    Occupancy is an estimate: last measured prompt + its generated output.
    The harness does not expose a live tokenizer/compaction/context API.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.context_tokens = None
        self.input_tokens = self.output_tokens = self.cached_tokens = 0
        self.elapsed = None

    def observe(self, usage):
        if not isinstance(usage, dict):
            return
        if 'ContextEstimate' in usage:
            self.context_tokens = count(usage['ContextEstimate'])
            return
        prompt = count(usage.get('InputTokens'))
        output = count(usage.get('OutputTokens'))
        if 'InputTokens' in usage and not usage.get('Compaction'):
            self.context_tokens = prompt + output
        self.input_tokens += prompt
        self.output_tokens += output
        self.cached_tokens += count(usage.get('CachedInputTokens'))

    def load(self, path):
        self.reset()
        try:
            with open(path, encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    try:
                        rec = item_record(json.loads(line))
                    except ValueError:
                        continue
                    data = rec.get('Data')
                    if rec.get('Kind') == 'model_response' and isinstance(data, dict):
                        response = data.get('Response')
                        if isinstance(response, dict):
                            self.observe(response.get('Usage'))
        except OSError:
            pass
        # Summary requests live outside the SDK journal. Include their durable
        # billing totals without replacing the last conversation occupancy.
        suffix='.session.jsonl'
        if str(path).endswith(suffix):
            name=os.path.basename(path)[:-len(suffix)]
            checkpoint=os.path.join(os.path.dirname(path),'context',name,'checkpoint.json')
            try:
                if os.path.getsize(checkpoint)<8*1024*1024:
                    with open(checkpoint,encoding='utf-8') as stream: data=json.load(stream)
                    usage=data.get('TotalUsage') or data.get('Usage') or {}
                    if isinstance(usage,dict): self.observe(dict(usage,Compaction=True))
            except (OSError,ValueError,AttributeError): pass

    def context_label(self, window):
        used = self.context_tokens
        if used is None:
            return f'ctx —/{tokens_label(window)}' if window else 'ctx ?'
        if not window:
            return f'ctx ~{tokens_label(used)} / ?'
        return f'ctx ~{used / window * 100:.1f}% · {tokens_label(used)}/{tokens_label(window)}'


class TerminalFooter:
    """One protected physical bottom row; normal output retains scrollback.

    Caller owns stdout, or serializes concurrent writes (Spinner does so by
    stopping before rendering records). No alternate screen or background UI
    thread; shutdown always restores the terminal's full scroll region.
    """
    def __init__(self, text, enabled=True):
        self.text = text
        self.enabled = bool(enabled and sys.stdout.isatty())
        self.active = False
        self.size = None
        self._frame = None
        self.lock = threading.RLock()

    def reserved_rows(self):
        return 1 if self.active and terminal_size().lines >= 4 else 0

    def start(self):
        if not self.enabled:
            return
        self.active = True
        size = terminal_size()
        # Allocate a blank last row before protecting it (preserve shell output).
        sys.stdout.write('\n')
        if size.lines >= 4:
            sys.stdout.write(f'\033[1;{size.lines-1}r\033[{size.lines-1};1H')
        self.size = size
        self.draw()

    def draw(self, force=True):
        if not self.active:
            return
        from editor import _fit
        with self.lock:
            size = terminal_size()
            prefix = ''
            if size != self.size:
                prefix = f'\033[1;{size.lines-1}r' if size.lines >= 4 else '\033[r'
                self.size = size
            try:
                text = self.text()
            except Exception:
                text = 'uachat · status unavailable'
            frame = (size, text)
            if not force and not prefix and frame == self._frame:
                return
            self._frame = frame
            # Save/restore also shields the user's editing cursor.
            out = '\0337' + prefix
            if size.lines >= 4:
                out += f'\033[{size.lines};1H\033[2K' + _fit(text, max(1, size.columns-1))
            sys.stdout.write(out + '\0338')
            sys.stdout.flush()

    def close(self):
        if not self.active:
            return
        with self.lock:
            size = terminal_size()
            sys.stdout.write('\0337' + f'\033[{size.lines};1H\033[2K' + '\033[r\0338\033[?25h')
            sys.stdout.flush()
            self.active = False


def normalize_event(record):
    """Validate external JSON shapes before event/render code uses them."""
    rec = dict(item_record(record))
    raw = rec.get('Data')
    data = dict(raw) if isinstance(raw, dict) else {}
    rec['Data'] = data
    if rec.get('Kind') == 'model_response':
        raw = data.get('Response')
        response = dict(raw) if isinstance(raw, dict) else {}
        data['Response'] = response
        raw = response.get('Usage')
        response['Usage'] = raw if isinstance(raw, dict) else {}
        raw = response.get('Failure')
        if raw and not isinstance(raw, dict):
            response['Failure'] = {'Message':str(raw)}
        outputs = []
        raw = response.get('Output')
        for output in raw if isinstance(raw, list) else []:
            if not isinstance(output, dict): continue
            item = dict(output)
            raw = item.get('Data')
            payload = dict(raw) if isinstance(raw, dict) else {}
            summary = payload.get('Summary')
            payload['Summary'] = [str(v) for v in summary] if isinstance(summary, list) else ([summary] if isinstance(summary, str) else [])
            item['Data'] = payload
            outputs.append(item)
        response['Output'] = outputs
    elif rec.get('Kind') == 'tool_call_status':
        raw = data.get('Status')
        data['Status'] = raw if isinstance(raw, dict) else {}
        operations = []
        raw = data.get('Operations')
        for op in raw if isinstance(raw, list) else []:
            if not isinstance(op, dict): continue
            op = dict(op)
            raw = op.get('State')
            state = dict(raw) if isinstance(raw, dict) else {}
            raw = state.get('Result')
            state['Result'] = raw if isinstance(raw, dict) else {}
            op['State'] = state
            operations.append(op)
        data['Operations'] = operations
    return rec


def wrap_text(text, width):
    """Display-column wrapping, preserving explicit lines and code indentation."""
    import unicodedata
    width = max(2, width)
    def cells(char):
        if unicodedata.combining(char) or unicodedata.category(char) in ('Mn','Me','Cf'):
            return 0
        return 2 if unicodedata.east_asian_width(char) in ('W','F') else 1
    for original in safe_text(text).split('\n'):
        chunk, used = '', 0
        for char in original:
            step = cells(char)
            if used + step > width and chunk:
                split = chunk.rfind(' ')
                if split > len(chunk)//2:
                    yield chunk[:split]
                    chunk = chunk[split+1:]
                    used = sum(cells(c) for c in chunk)
                else:
                    yield chunk
                    chunk, used = '', 0
            chunk += char
            used += step
        yield chunk
