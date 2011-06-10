## Copyright 2011, Scott W. Dunlop <swdunlop@gmail.com> All rights reserved.
##
## AndBug is free software: you can redistribute it and/or modify it under 
## the terms of version 3 of the GNU Lesser General Public License as 
## published by the Free Software Foundation.
##
## AndBug is distributed in the hope that it will be useful, but WITHOUT ANY
## WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS 
## FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for 
## more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with AndBug.  If not, see <http://www.gnu.org/licenses/>.

import andbug, andbug.data
from andbug.data import defer
from threading import Lock
from Queue import Queue

class RequestError(Exception):
    'raised when a request for more information from the process fails'
    def __init__(self, code):
        Exception.__init__(self, 'request failed, code %s' % code)
        self.code = code

class Element(object):
    def __repr__(self):
        return '<%s>' % self

class SessionElement(Element):
    def __init__(self, sess):
        assert isinstance(sess, Session)
        self.sess = sess
    @property
    def conn(self):
        return self.sess.conn

class Frame(SessionElement):
    def __init__(self, sess, fid):
        SessionElement.__init__(self, sess)
        self.fid = fid
        self.loc = None
        self.tid = None
    def __str__(self):
        return 'frame %s, at %s' % (self.fid, self.loc)   

    @classmethod 
    def unpackFrom(impl, sess, buf):
        return sess.pool(impl, sess, buf.unpackFrameId())
    
    def packTo(self, buf):
        buf.packFrameId(self.fid)

    @property
    def native(self):
        return self.loc.native

    @property
    def values(self):
        vals = {}
        if self.native: return vals
        
        sess = self.sess
        conn = self.conn
        buf = conn.buffer()
        buf.packObjectId(self.tid)
        buf.packFrameId(self.fid)
        slots = self.loc.slots
        buf.packInt(len(slots))

        for slot in slots:
            buf.packInt(slot.index)
            buf.packU8(slot.tag) #TODO: GENERICS

        code, buf = conn.request(0x1001, buf.data())
        if code != 0:
            raise RequestError(code)
        ct = buf.unpackInt()

        for x in range(0, ct):
            s = slots[x]
            vals[s.name] = unpack_value(sess, buf)

        return vals
                                
class Thread(SessionElement):
    def __init__(self, sess, tid):
        SessionElement.__init__(self, sess)
        self.tid = tid
    
    def __repr__(self):
        return '<%s>' % self

    def __str__(self):
        return 'thread %s' % (self.name or hex(self.tid))

    def suspend(self):  
        conn = self.conn
        buf = conn.buffer()
        buf.packObjectId(self.tid)
        code, buf = conn.request(0x0B01, buf.data())
        if code != 0:
            raise RequestError(code)

    def resume(self):
        conn = self.conn
        buf = conn.buffer()
        buf.pack('o', self.tid)
        code, buf = conn.request(0x0B03, buf.data())
        if code != 0:
            raise RequestError(code)

    def packTo(self, buf):
        buf.packObjectId(self.tid)

    @classmethod
    def unpackFrom(impl, sess, buf):
        tid = buf.unpackObjectId()
        return sess.pool(impl, sess, tid)

    @property
    def frames(self):
        tid = self.tid
        sess = self.sess
        conn = self.conn
        buf = conn.buffer()
        buf.pack('oii', self.tid, 0, -1)
        code, buf = conn.request(0x0B06, buf.data())
        if code != 0:
            raise RequestError(code)
        ct = buf.unpackInt()

        def load_frame():
            f = Frame.unpackFrom(sess, buf)
            f.loc = Location.unpackFrom(sess, buf)
            f.tid = tid
            return f

        return andbug.data.view(load_frame() for i in range(0,ct))

    @property
    def frameCount(self):   
        conn = self.conn
        buf = conn.buffer()
        buf.packObjectId(self.tid)
        code, buf = conn.request(0x0B07, buf.data())
        if code != 0:
            raise RequestError(code)
        return buf.unpackInt()

    @property
    def name(self): 
        conn = self.conn
        buf = conn.buffer()
        buf.packObjectId(self.tid)
        code, buf = conn.request(0x0B01, buf.data())
        if code != 0:
            raise RequestError(code)
        return buf.unpackStr()

class Location(SessionElement):
    def __init__(self, sess, cid, mid, loc):
        SessionElement.__init__(self, sess)
        self.cid = cid
        self.mid = mid
        self.loc = loc
        self.line = None

    def __str__(self):
        if self.loc >= 0:
            return '%s:%i' % (self.method, self.loc)
        else:
            return str(self.method)

    def packTo(self, buf):
        c = self.klass
        buf.ipack('1tm8', c.tag, self.cid, self.mid, self.loc)

    @classmethod
    def unpackFrom(impl, sess, buf):
        tag, cid, mid, loc = buf.unpack('1tm8')
        return sess.pool(impl, sess, cid, mid, loc)

    def hook(self, queue = None):
        conn = self.conn
        buf = conn.buffer()
        # 40:EK_METHOD_ENTRY, 1: SP_THREAD, 1 condition of type Location (7)
        buf.pack('11i1', 40, 1, 1, 7) 

        self.packTo(buf)
        code, buf = conn.request(0x0F01, buf.data())
        if code != 0:
            raise RequestError(code)
        eid = buf.unpackInt()
        return self.sess.hook(eid, queue)
    
    @property
    def native(self):
        return self.loc == -1

    @property
    def method(self):
        #TODO: move to Method.unpackFrom(...)
        return self.sess.pool(Method, self.sess, self.cid, self.mid)

    @property
    def klass(self):
        #TODO: move to Class.unpackFrom(...)
        return self.sess.pool(Class, self.sess, self.cid)

    @property
    def slots(self):
        l = self.loc
        def filter_slots():
            for slot in self.method.slots:
                f = slot.firstLoc
                if f > l: continue
                if l - f > slot.locLength: continue
                yield slot
        return tuple() if self.native else tuple(filter_slots())

class Slot(SessionElement):
    def __init__(self, sess, cid, mid, index):
        SessionElement.__init__(self, sess)
        self.cid = cid
        self.mid = mid
        self.index = index
        self.name = None

    def __str__(self):
        if self.name:
            return 'slot %s at index %i' % (self.name, self.index)
        else:
            return 'slot at index %i' % (self.index)

    def load_slot(self):
        #TODO: should be Class.unpackFrom
        self.sess.pool(Class, self.sess, cid).load_slots()

    firstLoc = defer(load_slot, 'firstLoc')
    locLength = defer(load_slot, 'locLength')
    name = defer(load_slot, 'name')
    jni = defer(load_slot, 'jni')
    gen = defer(load_slot, 'gen')

    @property
    def tag(self):
        return ord(self.jni[0])

class Method(SessionElement):
    def __init__(self, sess, cid, mid):
        SessionElement.__init__(self, sess)
        self.cid = cid
        self.mid = mid

    @property
    def klass(self):
        #TODO: should be Class.unpackFrom
        return self.sess.pool(Class, self.sess, self.cid)

    def __str__(self):
        return '%s.%s%s' % (
            self.klass, self.name, self.jni 
    )       
     
    def __repr__(self):
        return '<method %s>' % self

    def load_line_table(self):
        sess = self.sess
        conn = sess.conn
        pool = sess.pool
        cid = self.cid
        mid = self.mid
        data = conn.buffer().pack('om', cid, mid)
        code, buf = conn.request(0x0601, data)
        if code != 0: raise RequestError(code)
        
        f, l, ct = buf.unpack('88i')
        if (f == -1) or (l == -1):             
            self.firstLoc = None
            self.lastLoc = None
            self.lineTable = andbug.data.view([])
            #TODO: How do we handle native methods?
 
        self.firstLoc = pool(Location, sess, cid, mid, f)
        self.lastLoc = pool(Location, sess, cid, mid, l)

        ll = {}
        self.lineLocs = ll
        def line_loc():
            loc, line  = buf.unpack('8i')
            loc = pool(Location, sess, cid, mid, loc)
            loc.line = line
            ll[line] = loc

        for i in range(0,ct):
            line_loc()
    
    firstLoc = defer(load_line_table, 'firstLoc')
    lastLoc = defer(load_line_table, 'lastLoc')
    lineTable = defer(load_line_table, 'lineTable')

    def load_method(self):
        self.klass.load_methods()

    name = defer(load_method, 'name')
    jni = defer(load_method, 'jni')
    gen = defer(load_method, 'gen')
    flags = defer(load_method, 'flags' )

    def load_slot_table(self):
        sess = self.sess
        conn = self.conn
        pool = sess.pool
        cid = self.cid
        mid = self.mid
        data = conn.buffer().pack('om', cid, mid)
        code, buf = conn.request(0x0605, data)
        if code != 0: raise RequestError(code)
    
        act, sct = buf.unpack('ii')
        #TODO: Do we care about the argCnt ?
         
        def load_slot():
            codeIndex, name, jni, gen, codeLen, index  = buf.unpack('l$$$ii')
            #TODO: should be Slot.unpackFrom
            slot = pool(Slot, sess, cid, mid, index)
            slot.firstLoc = codeIndex
            slot.locLength = codeLen
            slot.name = name
            slot.jni = jni
            slot.gen = gen

            return slot

        self.slots = andbug.data.view(load_slot() for i in range(0,sct))

    slots = defer(load_slot_table, 'slots')
                
#TODO: SESSION
class Class(SessionElement): 
    def __init__(self, sess, cid):
        SessionElement.__init__(self, sess)
        self.cid = cid
    
    def __str__(self):
        return self.name
    
    def __repr__(self):
        return '<class %s>' % self

    def hookEntries(self, queue):
        conn = self.conn
        buf = conn.buffer()
        # 40:EK_METHOD_ENTRY, 1: SP_THREAD, 1 condition of type ClassRef (4)
        buf.pack('11i1t', 40, 1, 1, 4, self.cid) 
        code, buf = conn.request(0x0F01, buf.data())
        if code != 0:
            raise RequestError(code)
        eid = buf.unpackInt()
        return self.sess.hook(eid, queue)
        
    def load_methods(self):
        cid = self.cid
        sess = self.sess
        conn = self.conn
        pool = sess.pool
        buf = conn.buffer()
        buf.pack("t", cid)
        code, buf = conn.request(0x020F, buf.data())
        if code != 0:
            raise RequestError(code)

        ct = buf.unpackU32()
                
        def load_method():
            mid, name, jni, gen, flags = buf.unpack('m$$$i')
            obj = pool(Method, sess, cid, mid)
            obj.name = name
            obj.jni = jni
            obj.gen = gen
            obj.flags = flags
            return obj
    
        self.methodList = andbug.data.view(load_method() for i in range(0, ct))
        self.methodByJni = andbug.data.multidict()
        self.methodByName = andbug.data.multidict()

        for item in self.methodList:
            jni = item.jni
            name = item.name
            self.methodByJni[jni] = item
            self.methodByName[name] = item
    
    methodList = defer(load_methods, 'methodList')
    methodByJni = defer(load_methods, 'methodByJni')
    methodByName = defer(load_methods, 'methodByName')

    methodList = defer(load_methods, 'methodList')
    methodByJni = defer(load_methods, 'methodByJni')
    methodByName = defer(load_methods, 'methodByName')

    def load_class(self):
        self.sess.load_classes()
        assert self.tag != None
        assert self.flags != None

    tag = defer(load_class, 'tag')
    jni = defer(load_class, 'jni')
    gen = defer(load_class, 'gen')
    flags = defer(load_class, 'flags')

    def methods(self, name=None, jni=None):
        if name and jni:
            seq = self.methodByName[name]
            seq = filter(x in seq, self.methodByJni[jni])
        elif name:
            seq = andbug.data.view(self.methodByName[name])
        elif jni:
            seq = self.methodByJni[jni]
        else:
            seq = self.methodList
        return andbug.data.view(seq)
    
    @property
    def name(self):
        name = self.jni
        if name.startswith('L'): name = name[1:]
        if name.endswith(';'): name = name[:-1]
        name = name.replace('/', '.')
        return name

class Hook(SessionElement):
    def __init__(self, sess, ident, queue = None):
        SessionElement.__init__(self, sess)
        self.queue = queue or Queue()
        self.ident = ident
        #TODO: unclean
        with self.sess.ectl:
            self.sess.emap[ident] = self

    def put(self, data):
        return self.queue.put(data)
            
    def get(self, block = False, timeout = None):
        return self.queue.get(block, timeout)

    def clear(self):
        #TODO: unclean
        #TODO: EventRequest.Clear
        with self.sess.ectl:
            del self.sess.emap[ident]

unpack_impl = [None,] * 256

def register_unpack_impl(ek, fn):
    unpack_impl[ek] = fn

#TODO: CONTEXT/SESSION
def unpack_events(sess, buf):
    sp, ct = buf.unpack('1i')
    for i in range(0, ct):
        ek = buf.unpackU8()
        im = unpack_impl[ek]
        if im is None:
            raise RequestError(ek)
        else:
            yield im(sess, buf)

#TODO: CONTEXT/SESSION
def unpack_method_entry(sess, buf):
    rid = buf.unpackInt()
    t = Thread.unpackFrom(sess, buf)
    loc = Location.unpackFrom(sess, buf)

    #TODO: Do we even care about loc?
    return rid, t, loc

register_unpack_impl(40, unpack_method_entry)

class Session(object):
    def __init__(self, conn = None):
        self.pool = andbug.data.pool()
        self.conn = conn
        self.emap = {}
        self.ectl = Lock()
        if conn is not None:
            conn.hook(0x4064, self.processEvent)
            #TODO: REDUNDANT

    def hook(self, ident, queue = None):
        return Hook(self, ident, queue)

    def processEvent(self, ident, buf):
        pol, ct = buf.unpack('1i')

        for i in range(0,ct):
            ek = buf.unpackU8()
            im = unpack_impl[ek]
            if im is None:
                raise RequestError(ek)
            evt = im(self, buf)
            with self.ectl:
                hook = self.emap.get(evt[0])
            if hook is not None:
                hook.put(evt[1:])
                          
    def connect(self, portno = None):
        if portno: 
            self.portno = portno
            if self.conn is None: 
                self.conn = andbug.proto.connect('127.0.0.1', self.portno)
        
            self.conn.hook(0x4064, self.processEvent)
        return self.conn
    
    def load_classes(self):
        code, buf = self.connect().request(0x0114)
        if code != 0:
            raise RequestError(code)

        def load_class():
            tag, cid, jni, gen, flags = buf.unpack('1t$$i')
            obj = self.pool(Class, self, cid)
            obj.tag = tag
            obj.cid = cid
            obj.jni = jni
            obj.gen = gen
            obj.flags = flags
            return obj 
                        
        ct = buf.unpackU32()

        self.classList = andbug.data.view(load_class() for i in range(0, ct))
        self.classByJni = andbug.data.multidict()
        for item in self.classList:
            self.classByJni[item.jni] = item

    classList = defer(load_classes, 'classList')
    classByJni = defer(load_classes, 'classByJni')

    def classes(self, jni=None):
        if jni:
            seq = self.classByJni[jni]
        else:
            seq = self.classList
        return andbug.data.view(seq)
    
    def suspend(self):
        code, buf = self.conn.request(0x0108, '')
        if code != 0:
            raise RequestError(code)

    def resume(self):
        code, buf = self.conn.request(0x0109, '')
        if code != 0:
            raise RequestError(code)

    def exit(self, code = 0):
        conn = self.conn
        buf = conn.buffer()
        buf.pack('i', code)
        code, buf = conn.request(0x010A, '')
        if code != 0:
            raise RequestError(code)

    @property
    def threads(self):
        pool = self.pool
        code, buf = self.conn.request(0x0104, '')
        if code != 0:
            raise RequestError(code)
        ct = buf.unpackInt()

        def load_thread():
            tid = buf.unpackObjectId()
            return pool(Thread, self, tid)
        return andbug.data.view(load_thread() for x in range(0,ct))

class RefType(SessionElement):
    def __init__(self, sess, tag, tid):
        SessionElement.__init__(self, sess)
        self.tag = tag
        self.tid = tid
    
    def __repr__(self):
        return '<ref %s %s#%x>' % (self.jni, chr(self.tag), self.tid)

    @classmethod 
    def unpackFrom(impl, sess, buf):
        return sess.pool(impl, sess, buf.unpackU8(), buf.unpackTypeId())

    def packTo(self, buf):
        buf.packObjectId(self.tid)

    def load_signature(self):
        conn = self.conn
        buf = conn.buffer()
        self.packTo(buf)
        code, buf = conn.request(0x020d, buf.data())
        if code != 0:
            raise RequestError(code)
        self.jni = buf.unpackStr()
        self.gen = buf.unpackStr()

    gen = defer(load_signature, 'gen')
    jni = defer(load_signature, 'jni')

class Object(SessionElement):
    def __init__(self, sess, oid):
        if oid == 0: raise VoidError()
        SessionElement.__init__(self, sess)
        self.oid = oid

    def __repr__(self):
        return '<obj %s #%x>' % (self.jni, self.oid)
    
    @classmethod
    def unpackFrom(impl, sess, buf):
        oid = buf.unpackObjectId()
        # oid = 0 indicates a GC omgfuckup in Dalvik
        # which is NOT as uncommon as we would like..
        if not oid: return None 
        return sess.pool(impl, sess, oid)

    def packTo(self, buf):
        buf.packObjectId(self.oid)

    @property
    def gen(self):
        return self.reftype.gen
    
    @property
    def jni(self):
        return self.reftype.jni

    def load_reftype(self):
        conn = self.sess.conn
        buf = conn.buffer()
        self.packTo(buf)
        code, buf = conn.request(0x0901, buf.data())
        if code != 0:
            raise RequestError(code)
        self.reftype = RefType.unpackFrom(self.sess, buf)
    
    reftype = defer(load_reftype, 'reftype')

class String(Object):
    def __repr__(self):
        return '#' + repr(str(self))

    def __str__(self):
        return self.data

    @property
    def data(self):
        conn = self.conn
        buf = conn.buffer()
        self.packTo(buf)
        code, buf = conn.request(0x0A01, buf.data())
        if code != 0:
            raise RequestError(code)
        return buf.unpackStr()

unpack_value_impl = [None,] * 256
def register_unpack_value(tag, func):
    for t in tag:
        unpack_value_impl[ord(t)] = func

register_unpack_value('[', lambda p, b: b.unpackObjectId())
register_unpack_value('B', lambda p, b: b.unpackU8())
register_unpack_value('C', lambda p, b: chr(b.unpackU8()))
register_unpack_value('F', lambda p, b: b.unpackFloat()) #TODO: TEST
register_unpack_value('D', lambda p, b: b.unpackDouble()) #TODO:TEST
register_unpack_value('I', lambda p, b: b.unpackInt())
register_unpack_value('J', lambda p, b: b.unpackLong())
register_unpack_value('S', lambda p, b: b.unpackShort()) #TODO: TEST
register_unpack_value('V', lambda p, b: b.unpackVoid())
register_unpack_value('Z', lambda p, b: (True if b.unpackU8() else False))
register_unpack_value('L', Object.unpackFrom)
register_unpack_value('tglc', Object.unpackFrom) #TODO: IMPL
register_unpack_value('s', String.unpackFrom)

def unpack_value(sess, buf, tag = None):
    if tag is None: tag = buf.unpackU8()
    fn = unpack_value_impl[tag]
    if fn is None:
        raise RequestError(tag)
    else:
        return fn(sess, buf)