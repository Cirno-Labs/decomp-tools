/* The tracer's null renderer: Direct3D 8 that draws nothing.
 *
 * TraceInstallRenderer (trace.h) puts NullDirect3DCreate8 in the game's
 * import table when TRACE_RENDER is 0. The object it returns creates
 * devices that work as usual except that drawing, clearing and
 * presenting do nothing and succeed, so the game runs all of its own
 * code, draw callbacks included, without the rasterizing (llvmpipe,
 * under Xvfb) that takes most of a run's time. Textures, surfaces and
 * states still go through Wine; what the game reads back of the screen
 * (its copies of the back buffer) has nothing drawn on it. Mesa's no-op
 * driver (GALLIUM_NOOP) would do the same below Wine, but wined3d
 * crashes on it, and wined3d's no3d renderer gives a game no device.
 *
 * This file is C so that d3d8.h declares the interfaces' vtables as
 * structures: the compiler checks every replacement against the method
 * it replaces. */

#include <windows.h>
#include <d3d8.h>

#include "trace.h"

static IDirect3D8 *(WINAPI *s_realDirect3DCreate8)(UINT sdkVersion);
/* How many draws, clears and presents were dropped. */
static DWORD s_nullDraws;

static HRESULT(STDMETHODCALLTYPE *s_realCreateDevice)(IDirect3D8 *d3d, UINT adapter, D3DDEVTYPE type, HWND window,
                                                      DWORD flags, D3DPRESENT_PARAMETERS *params,
                                                      IDirect3DDevice8 **device);

static HRESULT STDMETHODCALLTYPE NullPresent(IDirect3DDevice8 *device, CONST RECT *src, CONST RECT *dst, HWND window,
                                             CONST RGNDATA *dirty)
{
    s_nullDraws++;
    return D3D_OK;
}

static HRESULT STDMETHODCALLTYPE NullClear(IDirect3DDevice8 *device, DWORD count, CONST D3DRECT *rects, DWORD flags,
                                           D3DCOLOR color, float z, DWORD stencil)
{
    s_nullDraws++;
    return D3D_OK;
}

static HRESULT STDMETHODCALLTYPE NullDrawPrimitive(IDirect3DDevice8 *device, D3DPRIMITIVETYPE type, UINT start,
                                                   UINT count)
{
    s_nullDraws++;
    return D3D_OK;
}

static HRESULT STDMETHODCALLTYPE NullDrawIndexedPrimitive(IDirect3DDevice8 *device, D3DPRIMITIVETYPE type,
                                                          UINT minIndex, UINT vertices, UINT start, UINT count)
{
    s_nullDraws++;
    return D3D_OK;
}

static HRESULT STDMETHODCALLTYPE NullDrawPrimitiveUP(IDirect3DDevice8 *device, D3DPRIMITIVETYPE type, UINT count,
                                                     CONST void *data, UINT stride)
{
    s_nullDraws++;
    return D3D_OK;
}

static HRESULT STDMETHODCALLTYPE NullDrawIndexedPrimitiveUP(IDirect3DDevice8 *device, D3DPRIMITIVETYPE type,
                                                            UINT minIndex, UINT vertices, UINT count,
                                                            CONST void *indices, D3DFORMAT format,
                                                            CONST void *data, UINT stride)
{
    s_nullDraws++;
    return D3D_OK;
}

/* Makes the `size` bytes of the vtable at `vtbl` writable: it is
 * Wine's, shared by every object of the interface. */
static BOOL Unprotect(void *vtbl, SIZE_T size, DWORD *old)
{
    return VirtualProtect(vtbl, size, PAGE_READWRITE, old);
}

static HRESULT STDMETHODCALLTYPE NullCreateDevice(IDirect3D8 *d3d, UINT adapter, D3DDEVTYPE type, HWND window,
                                                  DWORD flags, D3DPRESENT_PARAMETERS *params,
                                                  IDirect3DDevice8 **device)
{
    HRESULT res = s_realCreateDevice(d3d, adapter, type, window, flags, params, device);
    IDirect3DDevice8Vtbl *vtbl;
    DWORD old;

    if (FAILED(res) || *device == NULL)
    {
        return res;
    }
    vtbl = (IDirect3DDevice8Vtbl *)(*device)->lpVtbl;
    if (Unprotect(vtbl, sizeof(*vtbl), &old))
    {
        vtbl->Present = NullPresent;
        vtbl->Clear = NullClear;
        vtbl->DrawPrimitive = NullDrawPrimitive;
        vtbl->DrawIndexedPrimitive = NullDrawIndexedPrimitive;
        vtbl->DrawPrimitiveUP = NullDrawPrimitiveUP;
        vtbl->DrawIndexedPrimitiveUP = NullDrawIndexedPrimitiveUP;
        VirtualProtect(vtbl, sizeof(*vtbl), old, &old);
    }
    return res;
}

static IDirect3D8 *WINAPI NullDirect3DCreate8(UINT sdkVersion)
{
    IDirect3D8 *d3d = s_realDirect3DCreate8(sdkVersion);
    IDirect3D8Vtbl *vtbl;
    DWORD old;

    if (d3d == NULL)
    {
        return NULL;
    }
    vtbl = (IDirect3D8Vtbl *)d3d->lpVtbl;
    if (vtbl->CreateDevice != NullCreateDevice && Unprotect(vtbl, sizeof(*vtbl), &old))
    {
        s_realCreateDevice = vtbl->CreateDevice;
        vtbl->CreateDevice = NullCreateDevice;
        VirtualProtect(vtbl, sizeof(*vtbl), old, &old);
    }
    return d3d;
}

BOOL TraceInstallRenderer(void)
{
    if (TraceEnvNumber("TRACE_RENDER", 0) != 0)
    {
        return TRUE;
    }
    *(void **)&s_realDirect3DCreate8 = TracePatchImport("d3d8.dll", "Direct3DCreate8", (void *)NullDirect3DCreate8);
    TraceInfo(" nullRenderer=%d", s_realDirect3DCreate8 != NULL);
    return s_realDirect3DCreate8 != NULL;
}
