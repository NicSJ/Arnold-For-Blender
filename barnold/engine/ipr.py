# -*- coding: utf-8 -*-

__author__ = "Ildar Nikolaev"
__email__ = "nildar@users.sourceforge.net"

import sys
import numpy
import mmap

ABORT = 1
UPDATE = 2


def ipr():
    import weakref
    from types import ModuleType

    code = __spec__.loader.get_code(__name__)

    def _exec(engine, data, width, height):
        _main = sys.modules["__main__"]
        try:
            mod = ModuleType("__main__")
            mod.__file__ = __file__

            mod._engine_ = weakref.ref(engine)
            mod._data_ = data
            mod._width_ = width
            mod._height_ = height
            mod._mmap_size_ = None
            mod._mmap_ = None

            sys.modules["__main__"] = mod
            exec(code, mod.__dict__)
        finally:
            sys.modules["__main__"] = _main
        return mod

    return _exec


def _worker(data, new_data, redraw_event, mmap_size, mmap_name, state):
    print("+++ _worker: started")

    import os
    import ctypes

    dir = os.path.dirname(__file__)
    if dir not in sys.path:
        sys.path.append(dir)

    import arnold

    nodes = {}
    nptrs = []  # nodes linked by AiNodeSetPtr
    links = []  # nodes linked by AiNodeLink

    def _AiNodeSetArray(node, param, value):
        t, a = value
        _len = len(a)
        if t == arnold.AI_TYPE_POINT:
            _len //= 3
        elif t == arnold.AI_TYPE_UINT:
            pass
        _a = arnold.AiArrayConvert(_len, 1, t, ctypes.c_void_p(a.ctypes.data))
        arnold.AiNodeSetArray(node, param, _a)

    _AiNodeSet = {
        'NodeSocketShader': lambda n, i, v: True,
        'NodeSocketBool': lambda n, i, v: arnold.AiNodeSetBool(n, i, v),
        'NodeSocketInt': lambda n, i, v: arnold.AiNodeSetInt(n, i, v),
        'NodeSocketFloat': lambda n, i, v: arnold.AiNodeSetFlt(n, i, v),
        'NodeSocketColor': lambda n, i, v: arnold.AiNodeSetRGBA(n, i, *v),
        'NodeSocketVector': lambda n, i, v: arnold.AiNodeSetVec(n, i, *v),
        'NodeSocketVectorXYZ': lambda n, i, v: arnold.AiNodeSetPnt(n, i, *v),
        'NodeSocketString': lambda n, i, v: arnold.AiNodeSetStr(n, i, v),
        'ArnoldNodeSocketColor': lambda n, i, v: arnold.AiNodeSetRGB(n, i, *v),
        'ArnoldNodeSocketByte': lambda n, i, v: arnold.AiNodeSetByte(n, i, v),
        'ArnoldNodeSocketProperty': lambda n, i, v: True,
        'BOOL': lambda n, p, v: arnold.AiNodeSetBool(n, p, v),
        'BYTE': lambda n, p, v: arnold.AiNodeSetByte(n, p, v),
        'INT': lambda n, p, v: arnold.AiNodeSetInt(n, p, v),
        'FLOAT': lambda n, p, v: arnold.AiNodeSetFlt(n, p, v),
        'POINT2': lambda n, p, v: arnold.AiNodeSetPnt2(n, p, *v),
        'RGB': lambda n, p, v: arnold.AiNodeSetRGB(n, p, *v),
        'RGBA': lambda n, p, v: arnold.AiNodeSetRGBA(n, p, *v),
        'VECTOR': lambda n, p, v: arnold.AiNodeSetVec(n, p, *v),
        'STRING': lambda n, p, v: arnold.AiNodeSetStr(n, p, v),
        'MATRIX': lambda n, p, v: arnold.AiNodeSetMatrix(n, p, arnold.AtMatrix(*v)),
        'ARRAY': _AiNodeSetArray,
        'LINK': lambda n, p, v: links.append((n, p, v)),
        'NODE': lambda n, p, v: nptrs.append((n, p, v)),
    }

    arnold.AiBegin()
    try:
        #arnold.AiMsgSetConsoleFlags(arnold.AI_LOG_ALL)
        #arnold.AiMsgSetConsoleFlags(0x000E)

        #from pprint import pprint as pp
        #pp(data)

        ## Nodes
        for node in data['nodes']:
            nt, np = node
            anode = arnold.AiNode(nt)
            for n, (t, v) in np.items():
                _AiNodeSet[t](anode, n, v)
            nodes[id(node)] = anode
        options = arnold.AiUniverseGetOptions()
        for n, (t, v) in data['options'].items():
            _AiNodeSet[t](options, n, v)
        for n, p, v in nptrs:
            arnold.AiNodeSetPtr(n, p, nodes[id(v)])
        for n, p, v in links:
            arnold.AiNodeLink(nodes[id(v)], p, n)
        del nodes, nptrs, links, data

        ## Outputs
        filter = arnold.AiNode("gaussian_filter")
        arnold.AiNodeSetStr(filter, "name", "__filter")
        driver = arnold.AiNode("driver_display")
        arnold.AiNodeSetStr(driver, "name", "__driver")
        arnold.AiNodeSetBool(driver, "rgba_packing", False)
        outputs_aovs = (
            b"RGBA RGBA __filter __driver",
        )
        outputs = arnold.AiArray(len(outputs_aovs), 1, arnold.AI_TYPE_STRING, *outputs_aovs)
        arnold.AiNodeSetArray(options, "outputs", outputs)

        _rect = lambda n, w, h: numpy.frombuffer(
            mmap.mmap(-1, w * h * 4 * 4, n), dtype=numpy.float32
        ).reshape([h, w, 4])
        rect = _rect(mmap_name, *mmap_size)

        def _callback(x, y, width, height, buffer, data):
            #print("+++ _callback:", x, y, width, height, ctypes.cast(buffer, ctypes.c_void_p))
            if buffer:
                try:
                    if new_data.empty():
                        _buffer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float))
                        a = numpy.ctypeslib.as_array(_buffer, shape=(height, width, 4))
                        rect[y : y + height, x : x + width] = a
                        redraw_event.set()
                        return
                finally:
                    arnold.AiFree(buffer)
            elif new_data.empty():
                return
            arnold.AiRenderAbort()

        cb = arnold.AtDisplayCallBack(_callback)
        arnold.AiNodeSetPtr(driver, "callback", cb)

        while state.value != ABORT:
            for sl in range(-3, 1):
                arnold.AiNodeSetInt(options, "AA_samples", sl)
                res = arnold.AiRender(arnold.AI_RENDER_MODE_CAMERA)
                if res != arnold.AI_SUCCESS:
                    # TODO: clear new_data, process may hangs
                    break

            data = {}
            _data = new_data.get()
            while _data is not None:
                data.update(_data)
                if new_data.empty():
                    _nodes = data.get('nodes')
                    if _nodes is not None:
                        for name, params in _nodes.items():
                            node = arnold.AiNodeLookUpByName(name)
                            for n, (t, v) in params.items():
                                _AiNodeSet[t](node, n, v)
                    opts = data.get('options')
                    if opts is not None:
                        for n, (t, v) in opts.items():
                            _AiNodeSet[t](options, n, v)
                    size = data.get('mmap_size')
                    if size is not None:
                        rect = _rect(mmap_name, *size)
                    break
                _data = new_data.get()
    finally:
        arnold.AiEnd()
    print("+++ _worker: finished")


def _main():
    import multiprocessing as _mp
    import threading

    import bpy
    _mp.set_executable(bpy.app.binary_path_python)

    #import logging
    #logger = _mp.log_to_stderr()
    #logger.setLevel(logging.INFO)

    global _engine_, _data_, _width_, _height_, _mmap_size_, _mmap_

    _mmap_name = "blender/barnold/ipr/pid-%d" % id(_engine_)
    _mmap_ = mmap.mmap(-1, 64 * 1024 * 1024, _mmap_name)  # 64Mb

    state = _mp.Value('i', 0)
    redraw_event = _mp.Event()

    def tag_redraw():
        while redraw_event.wait() and state.value != ABORT:
            redraw_event.clear()
            e = _engine_()
            if e is not None:
                e.tag_redraw()
            del e

    def _mmap_size(opts):
        m = max(_width_, _height_)
        if m > 300:
            c = 900 / (m + 600)
            w = int(_width_ * c)
            h = int(_height_ * c)
        else:
            w = _width_
            h = _height_
        opts['xres'] = ('INT', w)
        opts['yres'] = ('INT', h)

        global _mmap_
        _mmap_ = mmap.mmap(-1, w * h * 4 * 4, _mmap_name)

        return w, h

    _mmap_size_ = _mmap_size(_data_['options'])
    new_data = _mp.Queue()

    def update(width, height, view_matrix):
        global _width_, _height_, _mmap_size_, _mmap_

        data = {}

        if _width_ != width or _height_ != height:
            opts = {}
            _width_ = width
            _height_ = height
            _mmap_size_ = _mmap_size(opts)
            data['mmap_size'] = _mmap_size_
            data['options'] =  opts

        if _view_matrix != view_matrix:
            _view_matrix[:] = view_matrix
            data['nodes'] = {
                '__camera': {
                    'matrix': ('MATRIX', numpy.reshape(view_matrix.inverted().transposed(), -1))
                }
            }

        if data:
            new_data.put(data)

        return _mmap_size_, numpy.frombuffer(_mmap_, dtype=numpy.float32)

    redraw_thread = threading.Thread(target=tag_redraw)
    process = _mp.Process(target=_worker, args=(
        _data_, new_data, redraw_event, _mmap_size_, _mmap_name, state
    ))

    def stop():
        print(">>> stop (1): started")
        state.value = ABORT
        print(">>> stop (2): ABORT")
        new_data.put(None)
        new_data.close()
        print(">>> stop (2): data")
        redraw_event.set()
        print(">>> stop (3):", redraw_thread)
        redraw_thread.join()
        print(">>> stop (4):", redraw_thread)
        print(">>> stop (5):", process)
        process.join(5)
        print(">>> stop (6):", process)
        if process.is_alive():
            process.terminate()
        print(">>> stop (7):", process)

    redraw_thread.start()
    process.start()

    return update, stop


if __name__ == "__main__":
    update, stop = _main()
    del _data_