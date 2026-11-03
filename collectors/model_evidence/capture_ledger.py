from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4

from .contract import ContractError,encode,digest,require
from .capture_contract import LedgerSnapshot,event,manifest,bounded_json,MAX_EVENTS,EVENT_BYTES,APPEND_BYTES,REPLAY_BYTES,SEGMENT_BYTES


def directory(path,create=False):
    require(isinstance(path,(str,Path)),'capture-path')
    path=Path(path)
    require(path.is_absolute() and '..' not in path.parts,'capture-path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for index,part in enumerate(path.parts[1:]):
            try:child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:
                require(create and index==len(path.parts)-2,'capture-directory-missing')
                os.mkdir(part,0o700,dir_fd=fd)
                os.fsync(fd)
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise


def read_file(fd,name,limit,check):
    require('/' not in name and name not in ('.','..'),'capture-entry-path')
    check()
    child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        before=os.fstat(child)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=limit,'capture-file-kind-or-size')
        parts=[];size=0
        while True:
            check();raw=os.read(child,min(65536,limit+1-size))
            if not raw:break
            parts.append(raw);size+=len(raw)
            require(size<=limit,'capture-file-size')
        after=os.fstat(child);entry=os.stat(name,dir_fd=fd,follow_symlinks=False)
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        require(all(getattr(before,k)==getattr(after,k) for k in fields) and entry.st_ino==after.st_ino and entry.st_dev==after.st_dev,'capture-file-changed')
        return b''.join(parts)
    finally:os.close(child)


def publish(fd,name,raw,check,crash,replace=False):
    require('/' not in name and name not in ('.','..'),'capture-entry-path')
    check();temp='.tmp-'+uuid4().hex
    child=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        view=memoryview(raw)
        while view:
            check();written=os.write(child,view[:65536]);require(written>0,'capture-short-write');view=view[written:]
            crash('during-write')
        os.fsync(child);crash('after-file-fsync');check()
    finally:os.close(child)
    crash('before-publication');check()
    if replace:
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
    else:
        try:os.link(temp,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        except FileExistsError:require(read_file(fd,name,len(raw),check)==raw,'capture-immutable-conflict')
        os.unlink(temp,dir_fd=fd)
    os.fsync(fd)


def write_output(path,raw,*,check=lambda:None):
    require(type(raw) is bytes and len(raw)<=REPLAY_BYTES,'capture-output-size')
    target=Path(path)
    require(target.is_absolute() and '..' not in target.parts,'capture-path')
    fd=None
    try:
        fd=directory(target.parent)
        publish(fd,target.name,raw,check,lambda _:None)
    except OSError:raise ContractError('capture-output-io') from None
    finally:
        if fd is not None:os.close(fd)


def read_input(path,*,check=lambda:None):
    target=Path(path);fd=None
    try:
        fd=directory(target.parent)
        return read_file(fd,target.name,REPLAY_BYTES,check)
    except OSError:raise ContractError('capture-input-io') from None
    finally:
        if fd is not None:os.close(fd)


class Ledger:
    def __init__(self,path,recorder_id,epoch,*,check=lambda:None,crash=lambda _:None):
        self.path=Path(path);self.recorder_id=recorder_id;self.epoch=epoch;self.check=check;self.crash=crash
        self.empty=manifest({'schema':'flightops-boundary-ledger/1','recorder_id':recorder_id,'recorder_epoch':epoch,
            'segments':[],'last_sequence':0,'last_event_digest':None,'last_source_at':None})
        self.identity={'schema':'flightops-boundary-identity/1','recorder_id':recorder_id,'recorder_epoch':epoch}

    @contextmanager
    def writer(self,create=False):
        fd=lock=None
        try:
            fd=directory(self.path,create)
            if create:
                with os.scandir(fd) as entries:require(all(e.name=='.writer.lock' for e in entries),'capture-ledger-already-exists')
            else:
                info=os.stat('manifest.json',dir_fd=fd,follow_symlinks=False)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'capture-manifest-kind')
            lock=os.open('.writer.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
            info=os.fstat(lock);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'capture-lock-kind')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ContractError('capture-writer-busy') from None
            self.check();yield fd
        except OSError:raise ContractError('capture-ledger-io') from None
        finally:
            if lock is not None:os.close(lock)
            if fd is not None:os.close(fd)

    def create(self):
        with self.writer(True) as fd:
            with os.scandir(fd) as entries:require(all(e.name=='.writer.lock' for e in entries),'capture-ledger-already-exists')
            publish(fd,'identity.json',encode(self.identity),self.check,self.crash)
            publish(fd,'manifest.json',encode(self.empty),self.check,self.crash)
        return self

    def _snapshot(self,fd):
        self.check()
        try:m=manifest(bounded_json(read_file(fd,'manifest.json',REPLAY_BYTES,self.check),REPLAY_BYTES))
        except (OSError,ContractError):return LedgerSnapshot(None,[],['manifest-unavailable'],None).fields()
        require(m['recorder_id']==self.recorder_id and m['recorder_epoch']==self.epoch,'capture-epoch-mismatch')
        events=[];gaps=set();used=0;previous=None;sequence=0;last_segment=None
        try:
            if bounded_json(read_file(fd,'identity.json',EVENT_BYTES,self.check),EVENT_BYTES)!=self.identity:gaps.add('identity-custody-mismatch')
        except (OSError,ContractError):gaps.add('identity-custody-unavailable')
        for segment in m['segments']:
            self.check()
            if segment['previous_segment_digest']!=last_segment:gaps.add('segment-chain-gap')
            last_segment=segment['digest']
            if used>=REPLAY_BYTES:gaps.add('replay-byte-limit');break
            try:raw=read_file(fd,segment['name'],min(SEGMENT_BYTES,REPLAY_BYTES-used),self.check)
            except (OSError,ContractError):gaps.add('segment-unavailable');continue
            used+=len(raw)
            if used>REPLAY_BYTES:gaps.add('replay-byte-limit');break
            parsed=[];invalid=False
            for line in raw.splitlines(keepends=True):
                self.check()
                if len(events)+len(parsed)>=MAX_EVENTS:invalid=True;gaps.add('replay-event-limit');break
                try:parsed.append(event(bounded_json(line,EVENT_BYTES)))
                except ContractError:invalid=True;gaps.add('invalid-event')
            if not raw.endswith(b'\n'):invalid=True;gaps.add('partial-tail')
            if not invalid and hashlib.sha256(raw).hexdigest()!=segment['digest']:gaps.add('segment-digest-mismatch')
            if len(parsed)!=segment['event_count']:gaps.add('segment-count-mismatch')
            if parsed and (parsed[0]['sequence']!=segment['first_sequence'] or parsed[-1]['sequence']!=segment['last_sequence']):gaps.add('segment-range-mismatch')
            for row in parsed:
                if row['recorder_id']!=self.recorder_id or row['recorder_epoch']!=self.epoch:gaps.add('event-epoch-mismatch')
                if row['sequence']!=sequence+1 or row['previous_event_digest']!=previous:gaps.add('event-chain-gap')
                sequence=row['sequence'];previous=digest(row);events.append(row)
        if sequence!=m['last_sequence'] or previous!=m['last_event_digest']:gaps.add('manifest-chain-gap')
        times=[e['recorded_at'] for e in events]
        as_of=max(times) if times else None
        if as_of!=m['last_source_at']:gaps.add('manifest-clock-mismatch')
        names=set();limit=MAX_EVENTS+16
        with os.scandir(fd) as entries:
            for entry in entries:
                self.check()
                if len(names)==limit:gaps.add('ledger-entry-limit');break
                names.add(entry.name)
                if entry.is_symlink():gaps.add('unsafe-ledger-entry')
        expected={'.writer.lock','identity.json','manifest.json'}|{s['name'] for s in m['segments']}
        if names-expected:gaps.add('unreferenced-ledger-entry')
        try:
            if bounded_json(read_file(fd,'manifest.json',REPLAY_BYTES,self.check),REPLAY_BYTES)!=m:gaps.add('manifest-advanced')
        except (OSError,ContractError):gaps.add('manifest-unavailable-after-read')
        return LedgerSnapshot(m,events,sorted(gaps),as_of).fields()

    def snapshot(self):
        fd=None
        try:
            fd=directory(self.path)
            return self._snapshot(fd)
        except OSError:raise ContractError('capture-ledger-io') from None
        finally:
            if fd is not None:os.close(fd)

    def append(self,records):
        require(type(records) is list and 0<len(records)<=MAX_EVENTS,'capture-append-count')
        rows=[];used=0
        for value in records:
            row=event(value);used+=len(encode(row));require(used<=APPEND_BYTES,'capture-append-size');rows.append(row)
        with self.writer() as fd:
            snapshot=self._snapshot(fd)
            require(not snapshot['gaps'],'capture-ledger-incomplete')
            m=deepcopy(snapshot['manifest']);known={encode(r) for r in snapshot['events']}
            rows=[r for r in rows if encode(r) not in known]
            if not rows:return snapshot
            require(len(snapshot['events'])+len(rows)<=MAX_EVENTS,'capture-event-limit')
            for row in rows:
                require(row['recorder_id']==self.recorder_id and row['recorder_epoch']==self.epoch,'capture-epoch-mismatch')
                require(row['sequence']==m['last_sequence']+1 and row['previous_event_digest']==m['last_event_digest'],'capture-append-chain')
                require(m['last_source_at'] is None or row['recorded_at']>=m['last_source_at'],'capture-ledger-clock-regressed')
                m.update(last_sequence=row['sequence'],last_event_digest=digest(row),last_source_at=row['recorded_at'])
            raw=b''.join(encode(row) for row in rows);checksum=hashlib.sha256(raw).hexdigest();name='seg-'+checksum+'.jsonl'
            require(sum(len(encode(r)) for r in snapshot['events'])+len(raw)<=REPLAY_BYTES,'capture-replay-limit')
            self.crash('before-segment')
            publish(fd,name,raw,self.check,self.crash)
            self.crash('after-segment-before-manifest')
            m['segments'].append({'name':name,'digest':checksum,'previous_segment_digest':m['segments'][-1]['digest'] if m['segments'] else None,
                'first_sequence':rows[0]['sequence'],'last_sequence':rows[-1]['sequence'],'event_count':len(rows)})
            manifest(m);self.check();self.crash('before-manifest')
            publish(fd,'manifest.json',encode(m),self.check,self.crash,True)
            return self._snapshot(fd)

    def seal_snapshot(self,output_path):
        snapshot=self.snapshot()
        require(not Path(output_path).is_relative_to(self.path),'capture-output-inside-ledger')
        write_output(output_path,encode(snapshot),check=self.check)
        return snapshot
