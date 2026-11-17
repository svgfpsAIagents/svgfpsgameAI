#!/usr/bin/env python3
"""
helios_bridge.py — local WebSocket bridge from the browser to a Helios Laser DAC.

The browser (joy_scan_laser.svg) opens a WebSocket to ws://127.0.0.1:7777 and
sends one binary message per frame. Each message is:

    [0]     0x01                    frame-data command
    [1]     frame ID (1..250)
    [2..5]  PPS  (uint32 LE)
    [6..7]  point count (uint16 LE)
    [8]     0x00                    flags
    [9]     0x00                    reserved
    [10..]  raw point buffer, 8 bytes per HeliosPoint
            (uint16 x, uint16 y, uint8 r, uint8 g, uint8 b, uint8 i)

This bridge parses the envelope and calls HeliosDac_WriteFrame(dev, pps, buf,
npts, 0). It polls HeliosDac_GetStatus(dev) and only writes when the DAC is
ready for the next frame, so it always plays the latest available frame and
silently drops stale ones if the browser sends faster than the DAC can play.

REQUIRES:
    - Python 3.7+
    - `pip install websockets`
    - libhelios_dac.so / helios_dac.dll / libhelios_dac.dylib in this dir,
      /usr/local/lib, or anywhere dlopen can find it.
    - A Helios Laser DAC plugged in via USB.

RUN:
    pip install websockets
    python3 helios_bridge.py
    # (in another terminal, or the same machine) open joy_scan_laser.svg,
    # open the LASER DAC panel, click STREAM LIVE (VIA = WS).
"""

import asyncio
import ctypes
import os
import sys
import time

# ---------- Load the Helios SDK shared library ----------
LIB_CANDIDATES = [
    os.environ.get('HELIOS_LIB', ''),
    './libhelios_dac.so',
    './helios_dac.dll',
    './libhelios_dac.dylib',
    '/usr/local/lib/libhelios_dac.so',
    '/usr/lib/libhelios_dac.so',
    '/opt/helios/libhelios_dac.so',
]
LIB = None
for p in LIB_CANDIDATES:
    if not p or not os.path.exists(p):
        try:
            # Last resort: let dlopen search the system path
            LIB = ctypes.CDLL(p) if p else None
        except OSError:
            continue
        if LIB:
            break
        continue
    try:
        LIB = ctypes.CDLL(p)
        print(f'[bridge] loaded {p}')
        break
    except OSError as e:
        print(f'[bridge] could not load {p}: {e}', file=sys.stderr)

if LIB is None:
    print('[bridge] FATAL: libhelios_dac.* not found.',
          'Set HELIOS_LIB=/path/to/libhelios_dac.so', file=sys.stderr)
    sys.exit(1)

# API signatures
LIB.HeliosDac_Initialize.restype = ctypes.c_int
LIB.HeliosDac_Initialize.argtypes = []

LIB.HeliosDac_GetStatus.restype = ctypes.c_int
LIB.HeliosDac_GetStatus.argtypes = [ctypes.c_uint]

LIB.HeliosDac_WriteFrame.restype = ctypes.c_int
LIB.HeliosDac_WriteFrame.argtypes = [
    ctypes.c_uint,                          # devNum
    ctypes.c_uint,                          # pps
    ctypes.POINTER(ctypes.c_ubyte),         # frameData
    ctypes.c_uint,                          # numOfPoints
    ctypes.c_uint,                          # blankingShift (called "scanPitch" in some SDK versions)
]

LIB.HeliosDac_Stop.restype = ctypes.c_int
LIB.HeliosDac_Stop.argtypes = [ctypes.c_uint]

LIB.HeliosDac_CloseDevices.restype = ctypes.c_int
LIB.HeliosDac_CloseDevices.argtypes = []

# ---------- Initialize DAC ----------
N_DACS = LIB.HeliosDac_Initialize()
if N_DACS <= 0:
    print('[bridge] FATAL: HeliosDac_Initialize returned', N_DACS,
          '(no DACs found)', file=sys.stderr)
    sys.exit(2)
print(f'[bridge] found {N_DACS} Helios DAC device(s); using device 0')

DEV = 0
HOST = os.environ.get('HELIOS_WS_HOST', '127.0.0.1')
PORT = int(os.environ.get('HELIOS_WS_PORT', '7777'))

# Latest-frame slot — the receiver always overwrites; the player always reads.
_latest = {'buf': None, 'pps': 0, 'npts': 0, 'frame_id': 0, 'ts': 0.0}
_latest_lock = asyncio.Lock()

async def receiver(ws):
    """Pull frames from the WebSocket, keep only the latest."""
    global _latest
    async for msg in ws:
        if isinstance(msg, str):
            # text control message — ignore
            continue
        if len(msg) < 10:
            continue
        # Parse envelope
        frame_id = msg[1]
        pps  = int.from_bytes(msg[2:6], 'little')
        npts = int.from_bytes(msg[6:8], 'little')
        expected = 10 + npts * 8
        if len(msg) < expected:
            print(f'[bridge] short frame: have {len(msg)}, expected {expected}',
                  file=sys.stderr)
            continue
        body = msg[10:expected]
        async with _latest_lock:
            _latest = {
                'buf': body, 'pps': pps, 'npts': npts,
                'frame_id': frame_id, 'ts': time.time()
            }

async def player():
    """Single playback loop: wait for DAC ready, write the latest frame."""
    global _latest
    while True:
        async with _latest_lock:
            snap = dict(_latest) if _latest['buf'] is not None else None
            if snap:
                _latest['buf'] = None  # consume so we never re-play an old frame

        if snap is None:
            await asyncio.sleep(0.001)
            continue

        # Wait for DAC to be ready for the next frame (back-pressure).
        # HeliosDac_GetStatus returns 1 when ready, 0 when still playing.
        wait_start = time.time()
        while LIB.HeliosDac_GetStatus(DEV) != 1:
            await asyncio.sleep(0.001)
            if time.time() - wait_start > 1.0:
                # DAC stuck for >1s — bail and try the next frame
                print('[bridge] DAC not ready for >1s, dropping frame',
                      file=sys.stderr)
                snap = None
                break
        if snap is None:
            continue

        # Copy into a ctypes buffer (must stay alive during the call).
        npts = snap['npts']
        buf = (ctypes.c_ubyte * len(snap['buf'])).from_buffer_copy(snap['buf'])
        rc = LIB.HeliosDac_WriteFrame(DEV, snap['pps'], buf, npts, 0)
        if rc != 1:
            print(f'[bridge] WriteFrame returned {rc} '
                  f'(pps={snap["pps"]}, npts={npts}, fid={snap["frame_id"]})',
                  file=sys.stderr)

async def handler(ws):
    """Per-connection handler: spawn the receiver; player runs globally."""
    peer = ws.remote_address if hasattr(ws, 'remote_address') else '?'
    print(f'[bridge] client connected: {peer}')
    try:
        await receiver(ws)
    except Exception as e:
        print(f'[bridge] receiver error: {e}', file=sys.stderr)
    finally:
        print(f'[bridge] client disconnected: {peer}')

async def main():
    import websockets
    print(f'[bridge] listening on ws://{HOST}:{PORT}')
    async with websockets.serve(handler, HOST, PORT, max_size=None):
        # Start the player task in parallel
        asyncio.create_task(player())
        # Run forever
        await asyncio.Future()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[bridge] shutting down')
        try:
            LIB.HeliosDac_Stop(DEV)
            LIB.HeliosDac_CloseDevices()
        except Exception:
            pass
