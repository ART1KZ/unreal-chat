"""Live inbox transport and a single-owner terminal event loop (stdlib only)."""
from __future__ import annotations
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
import uuid

import editor
from configio import file_lock, atomic_text
from terminal_ui import safe_text, terminal_size, normalize_event

MAX_PENDING_BYTES = 8*1024*1024


class Outbox:
    """UUIDs survive restart; accepted means persisted, never just pipe.write()."""
    def __init__(self,path):
        self.path = path
        self.items = {}
        self._load()

    def _load(self):
        try:
            with open(self.path,encoding='utf-8') as f: raw=json.load(f)
        except FileNotFoundError: return
        except (OSError,ValueError):
            raise ValueError('Не удалось прочитать live outbox; файл не перезаписан.') from None
        if not isinstance(raw,dict): raise ValueError('Неверный формат live outbox; файл не перезаписан.')
        if isinstance(raw,dict):
            for key,item in raw.items():
                try: uuid.UUID(key)
                except (ValueError,TypeError): continue
                if isinstance(item,dict) and isinstance(item.get('text'),str): self.items[key]=item

    def _write(self):
        atomic_text(self.path,json.dumps(self.items,ensure_ascii=False))

    def enqueue(self,text):
        with file_lock(self.path):
            self.items={}; self._load()
            existing = next((key for key,item in self.items.items() if item['text']==text),None)
            if not existing and sum(len(v['text'].encode()) for v in self.items.values())+len(text.encode())>MAX_PENDING_BYTES:
                raise ValueError('Неподтверждённые дополнения превышают 8 MiB; дождись принятия.')
            key = existing or str(uuid.uuid4())
            self.items[key]={'text':text,'created_at':time.time()}
            self._write()
            return key

    def accepted(self,key):
        with file_lock(self.path):
            self.items={}; self._load()
            if key not in self.items:return None
            text=self.items.pop(key)['text']; self._write(); return text

    def drop(self,key):
        return self.accepted(key)


class Composer(editor.Editor):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.phase='подготовка';self.started=time.monotonic();self.pending=0;self.accepted_count=0

    def _menu_lines(self,width):
        elapsed=time.monotonic()-self.started
        status=f'  {self.phase} · {elapsed:.0f}s · Enter дополнение · Esc прервать'
        lines=[editor._fit(self.paint(status,'dim'),max(1,width-1))]
        if self.pending or self.accepted_count:
            lines.append(editor._fit(self.paint(f'  inbox: принято {self.accepted_count} · ждёт подтверждения {self.pending}','dim'),max(1,width-1)))
        lines.extend(super()._menu_lines(width))
        return lines

    def erase(self):
        if self._rows:
            up=f'\033[{self._cursor_row}A' if self._cursor_row else ''
            sys.stdout.write(up+'\r'+editor.CLEAR_TO_END)
            sys.stdout.flush()
        self._rows=self._cursor_row=0


class Transport:
    def __init__(self,proc,events):
        self.proc=proc;self.events=events;self.commands=queue.Queue(maxsize=64)
        self.closed=threading.Event()
        self.reader=threading.Thread(target=self.read,daemon=True)
        self.writer=threading.Thread(target=self.write,daemon=True)
        self.reader.start();self.writer.start()

    def read(self):
        try:
            for line in self.proc.stdout:
                try: event=json.loads(line)
                except ValueError: event={'type':'error','message':'live runner: '+safe_text(line)}
                self.events.put(event)
        finally:
            self.closed.set();self.events.put({'type':'live.eof'})

    def write(self):
        while True:
            frame=self.commands.get()
            if frame is None: return
            try:
                self.proc.stdin.write(json.dumps(frame,ensure_ascii=False)+'\n');self.proc.stdin.flush()
            except (OSError,ValueError):
                self.events.put({'type':'live.write_failed','id':frame.get('id')})
                return

    def send(self,frame):
        if self.closed.is_set(): raise ValueError('Рабочий процесс уже завершился; дополнение не отправлено.')
        try:self.commands.put_nowait(frame)
        except queue.Full: raise ValueError('Очередь транспорта заполнена; текст оставлен в редакторе.') from None

    def close(self):
        try:self.commands.put_nowait(None)
        except queue.Full:pass
        if self.proc.stdin and not self.proc.stdin.closed:self.proc.stdin.close()
        self.writer.join(timeout=.5);self.reader.join(timeout=.5)


def run_live_turn(binary,workspace,request,renderer,spinner,history_path,outbox_path):
    import termios,tty
    from uarchat import SESSION_DIR,terminate,iter_session_records
    outbox=Outbox(outbox_path)
    from skills import Catalog
    from uarchat import command_suggestions
    skill_catalog = Catalog(workspace,binary)
    composer=Composer(history_path=history_path,paint=lambda text,key:renderer.theme.paint(text,key),footer=spinner.footer,
        suggestions=lambda text:command_suggestions(text,renderer.theme,os.environ.get('UACHAT_PROVIDER',''),skill_catalog))
    fd=sys.stdin.fileno(); old=termios.tcgetattr(fd)
    env=os.environ.copy()
    shell='/usr/local/bin/rtk-shell'
    if (env.get('UACHAT_RTK') or 'on').lower() not in ('off','0','false','no') and os.path.isfile(shell): env['SHELL']=shell
    try:
        proc=subprocess.Popen([binary,'-workspace',workspace,'-session-directory',SESSION_DIR,'-parent-pid',str(os.getpid())],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env=env,start_new_session=True)
    except OSError as error:
        renderer.pending_input=request.get('prompt') or ''
        renderer.error('cannot launch live runner: '+safe_text(error));return 127
    renderer.begin_turn()
    events=queue.Queue(); transport=Transport(proc,events)
    composer.pending=len(outbox.items)
    prompt=renderer.theme.paint('↳ ','user','1')
    submitted=[]; confirmed=[]; stopped=False; ready=False; deadline=None; eof=False
    initial_ids={item.get('message_id') for item in request.get('messages',[]) if isinstance(item,dict)}
    try:
        tty.setraw(fd,termios.TCSADRAIN)
        attrs=termios.tcgetattr(fd);attrs[1]=old[1];termios.tcsetattr(fd,termios.TCSADRAIN,attrs)
        sys.stdout.write(editor.PASTE_ON+editor.SHOW_CURSOR)
        transport.send({'type':'start','protocol':1,'request':request})
        composer._render(prompt)
        while not eof:
            changed=False
            for _ in range(100):
                try:event=events.get_nowait()
                except queue.Empty:break
                if not isinstance(event,dict):continue
                kind=event.get('type')
                if kind=='live.eof':eof=True;break
                if kind=='live.ready':
                    if event.get('protocol') != 1:
                        renderer.error('Неподдерживаемый live протокол; обнови адаптер.');terminate(proc)
                    else:ready=True;composer.phase='агент работает'
                    changed=True;continue
                if kind=='live.accepted':
                    text=outbox.accepted(event.get('id'))
                    if text is not None and event.get('id') not in initial_ids:
                        composer.accepted_count+=1;confirmed.append(text)
                        composer.erase();print(renderer.theme.paint('  ✓ дополнение принято в сессию','ok'))
                    composer.pending=len(outbox.items);changed=True;continue
                if kind in ('live.rejected','live.write_failed'):
                    composer._notice=safe_text(event.get('message') or 'Дополнение не подтверждено; осталось в outbox.')
                    changed=True;continue
                if kind=='live.finished':composer.phase='завершение';changed=True;continue
                composer.erase()
                record=normalize_event(event)
                renderer.handle(record)
                if record.get('Kind')=='model_response':
                    calls=[o['Data'].get('Name') for o in record['Data']['Response']['Output'] if o.get('Type')=='tool_call']
                    composer.phase=('выполняется '+str(calls[0])) if calls else 'агент работает'
                changed=True
            if eof:break
            if deadline and time.monotonic()>deadline:
                terminate(proc);deadline=None
            if select.select([fd],[],[],.05)[0]:
                try:
                    kind,value=editor.read_raw_key(fd)
                    if kind=='key' and value=='esc':raise KeyboardInterrupt
                    send=composer.feed_key(kind,value)
                except KeyboardInterrupt:
                    if not stopped:
                        try:transport.send({'type':'stop','mode':'hard'})
                        except ValueError:terminate(proc)
                        stopped=True;composer.phase='остановка';deadline=time.monotonic()+4
                    else:terminate(proc)
                    changed=True;send=False
                except EOFError:
                    send=False
                if send and composer._buffer.strip():
                    text=composer._buffer.strip()
                    if text.startswith('/') and text.split()[0] in __import__('uarchat').COMMANDS and not text.startswith('/skill '):
                        composer._notice='Команды переключения доступны после завершения; Esc остановит текущую работу.'
                    elif stopped or not ready:
                        composer._notice='Агент не принимает ввод сейчас; текст сохранён.'
                    else:
                        try:
                            text=skill_catalog.invoke(text,__import__('uarchat').COMMANDS)
                            key=outbox.enqueue(text)
                            transport.send({'type':'input','id':key,'text':text})
                            submitted.append(key);composer._remember(text)
                            composer._buffer='';composer._pos=0;composer._notice='Дополнение отправлено; ждём сохранения в сессии.'
                            composer.pending=len(outbox.items)
                        except (ValueError,OSError) as error:composer._notice=safe_text(error)
                    changed=True
                elif send:composer._notice=''
                changed=True
            # Layout cache avoids re-scanning a huge unchanged draft at every tick.
            if changed or time.monotonic()-getattr(composer,'last_draw',0)>.2:
                composer._render(prompt);composer.last_draw=time.monotonic()
        composer.erase()
        code=proc.wait()
        # If the pipe closed after persistence but before its receipt reached us,
        # canonical history resolves delivery without ever executing a replay.
        unresolved=set(submitted)&set(outbox.items)
        if unresolved:
            for rec in iter_session_records(os.path.join(SESSION_DIR,request['session_id']+'.session.jsonl')):
                if rec.get('Kind')=='input' and rec['Data'].get('ID') in unresolved:
                    key=rec['Data']['ID'];text=outbox.accepted(key)
                    if text is not None:confirmed.append(text)
                    unresolved.discard(key)
        drafts=[outbox.items[key]['text'] for key in submitted if key in outbox.items]
        renderer.pending_input=composer._buffer
        renderer.live_messages=confirmed
        renderer.unconfirmed_count=len(drafts)
        if drafts:
            renderer.error('Некоторые дополнения не подтверждены. /pending покажет UUID; автоматического повтора нет.')
        if stopped:
            print(renderer.theme.paint('  [live turn interrupted]','warn'));return 130
        renderer.end_turn(code)
        return code
    finally:
        composer.erase()
        if proc.poll() is None:terminate(proc)
        transport.close()
        if proc.stdout:proc.stdout.close()
        sys.stdout.write(editor.PASTE_OFF+editor.SHOW_CURSOR);sys.stdout.flush()
        termios.tcsetattr(fd,termios.TCSADRAIN,old)
        if spinner.footer:spinner.footer.draw()
