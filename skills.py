"""Skill selectors use the adapter's catalog; Python remains stdlib-only."""
import json
import os
import re
import shutil
import subprocess


def adapter_binary():
    explicit = os.environ.get('UACHAT_LIVE_BINARY')
    candidate = explicit or shutil.which('uachat-live-runner') or os.path.expanduser('~/.local/bin/uachat-live-runner')
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


class Catalog:
    def __init__(self, workspace, binary=None):
        self.workspace = workspace
        self.binary = binary or adapter_binary()
        self.supported = False
        self.skills = []
        self.diagnostics = []
        self.roots = []
        self.reload()

    def reload(self):
        if not self.binary:
            self.diagnostics = ['Skill discovery requires the adapter: uachat --install-live']
            return
        try:
            result = subprocess.run([self.binary, '-workspace', self.workspace, '--list-skills'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise ValueError('Adapter does not support skills; run uachat --install-live')
            data = json.loads(result.stdout)
            self.skills, self.diagnostics, self.roots = data['skills'], data['diagnostics'], data['roots']
            self.supported = True
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            self.supported = False
            self.skills = []
            self.diagnostics = [str(error)]

    def suggestions(self, text, reserved=()):
        # Editor callbacks receive the text up to the cursor, completing the
        # current token even in the middle of a multiline prompt.
        match = re.search(r'(?:^|\s)\$([^\s]*)$', text)
        if match:
            prefix = match.group(1).casefold()
            return [('$'+item['name'], item['description']) for item in self.skills
                    if item['user'] and item['name'].casefold().startswith(prefix)]
        if text.startswith('/skill '):
            prefix = text[len('/skill '):]
            if any(c.isspace() for c in prefix):
                return []
            return [(item['name'], item['description']) for item in self.skills
                    if item['user'] and item['name'].casefold().startswith(prefix.casefold())]
        if text.startswith('/') and not any(char.isspace() for char in text):
            prefix = text[1:].casefold()
            return [('/'+item['name'], 'skill · '+item['description']) for item in self.skills
                    if item['user'] and '/'+item['name'] not in reserved
                    and item['name'].casefold().startswith(prefix)]
        return []

    def is_slash_skill(self, prompt, reserved=()):
        token = prompt.split(maxsplit=1)[0] if prompt.strip() else ''
        return token.startswith('/') and token not in reserved and any(
            item['user'] and token == '/'+item['name'] for item in self.skills)

    def invoke(self, prompt, reserved=()):
        if prompt.startswith('/skill '):
            parts = prompt.split(maxsplit=2)
            name = parts[1]
            task = parts[2] if len(parts)>2 else ''
        elif self.is_slash_skill(prompt,reserved):
            parts = prompt.split(maxsplit=1)
            name = parts[0][1:]
            task = parts[1] if len(parts)>1 else ''
        else:
            return prompt
        item = next((item for item in self.skills if item['name'] == name and item['user']), None)
        if item is None:
            raise ValueError('Unknown or hidden skill: '+name+'; /skills lists available skills')
        return '$'+name+(' '+task if task else '')

    def display(self, query=''):
        query = query.casefold()
        rows = []
        for item in self.skills:
            if query and query not in (item['name']+' '+item['description']).casefold():
                continue
            modes = ('auto' if item['auto'] else 'manual only')+(' · hidden from menu' if not item['user'] else '')
            rows.extend([f"{item['name']} · {modes} · {item['source']}", '  '+item['description'], '  '+item['path']])
        if not rows:
            rows = ['No matching skills.']
        rows.extend('skill warning: '+warning for warning in self.diagnostics)
        return '\n'.join(rows)
