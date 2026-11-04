/* The runtime of a game's replay tracer: a DLL that replay.py adds to a
 * copy of the game's executable, which plays one replay and writes a
 * trace of the game's state. The game repository's tracer (replay.hook's
 * TRACER sources) uses these functions; they need only kernel32 and
 * user32, no CRT.
 *
 * The trace is a header, "TRCE", the version (1), the number of fields
 * and the record size, then each field's name and an information line,
 * each ending in a newline; then records of 32-bit fields. replay.py
 * checks every trace for the fields
 *
 *   kind         a TRACE_KIND_ below
 *   seq          the record's number, from 0
 *   passes       passes of the main loop into the update chain
 *   calcs        the game's own count of calculations, which goes up
 *                once each (several a pass when the game fast-forwards)
 *   replayFrame  the replay's frame counter
 *   stage        the stage being played
 *   flags        bit 0 set while the game plays (no menu is open)
 *   score        the score
 *
 * and compares the others too. Settings come from the environment:
 * replay.py sets the TRACE_ variables below and the game hook's own. */

#ifndef TRACE_H
#define TRACE_H

#include <windows.h>

#ifdef __cplusplus
extern "C"
{
#endif

    enum TraceKind
    {
        /* One calculation of the replay. */
        TRACE_KIND_CALC = 1,
        /* How a stage ended, when the next one starts. */
        TRACE_KIND_STAGE_END = 2,
        /* A stage started. */
        TRACE_KIND_STAGE_START = 3,
        /* How the last stage ended, when the replay ends by itself. */
        TRACE_KIND_REPLAY_END = 4,
        /* TRACE_TAIL calculations after the game was back on the title. */
        TRACE_KIND_TITLE = 5,
        /* TRACE_LIMIT passes ran out. */
        TRACE_KIND_LIMIT = 6,
        /* The game crashed: `passes` holds the exception code and
         * `calcs` its address instead. */
        TRACE_KIND_CRASH = 7,
    };

    /* Settings. */

    /* The number in environment variable `name` (decimal, or hex after
     * 0x), or `otherwise`. */
    DWORD TraceEnvNumber(const char *name, DWORD otherwise);
    /* Copies environment variable `name` into `buf`; FALSE without it. */
    BOOL TraceEnvString(const char *name, char *buf, DWORD size);
    /* Reads TRACE_ADDR, name=hex,..., into `addrs`, by the `count`
     * names in `names`; FALSE unless it gives each exactly. */
    BOOL TraceReadAddrs(const char **names, DWORD *addrs, int count);
    /* The next number of a comma-separated list at *s, moving past it
     * and its comma. */
    DWORD TraceNextNumber(const char **s);

    /* Patching the game's code. */

    /* Writes `size` bytes at `at`, through a writable mapping. */
    BOOL TraceWriteCode(void *at, const void *bytes, DWORD size);
    /* Points every 32-bit value in [start, start + size) that is `from`
     * at `to`; returns how many there were. For callbacks a function
     * installs, by the immediates it stores. */
    int TracePatchImmediates(DWORD start, DWORD size, DWORD from, DWORD to);
    /* Points every rel32 call or jump in [start, start + size) to `from`
     * at `to`; returns how many there were. */
    int TracePatchCalls(DWORD start, DWORD size, DWORD from, DWORD to);
    /* Replaces `dll`!`name` in the executable's import table; returns the
     * function it replaced, or NULL. */
    void *TracePatchImport(const char *dll, const char *name, void *to);

    /* The trace. */

    /* Creates the trace file at `path`. */
    BOOL TraceOpen(const char *path);
    /* Adds to the information line, which records what was patched. */
    void TraceInfo(const char *fmt, ...);
    /* Writes the header: `names` holds the `count` field names, each
     * ending in a newline. The information line is final then. */
    void TraceWriteHeader(const char *names, DWORD count);
    /* Buffers a record. */
    void TraceWrite(const void *record, DWORD size);
    /* Writes the buffered records out, as for a stage's end. */
    void TraceFlush(void);
    /* Writes out and closes the trace, then ends the game with `code`. */
    void TraceExit(UINT code);

    /* The environment. */

    /* Replaces timeGetTime in the import table. TRACE_SPEED 0 (the
     * default) advances the clock 17 ms each time it is read, so a
     * windowed game never waits for a frame and every run sees the same
     * times; N runs it N times real time. Only for games whose logic
     * ignores the clock during a replay. */
    BOOL TraceInstallClock(void);
    /* With TRACE_RENDER 0 (the default), replaces Direct3DCreate8 in the
     * import table with null_d3d8.c's, which creates devices that draw
     * nothing. Returns FALSE only if that fails. */
    BOOL TraceInstallRenderer(void);
    /* Calls `crashed` with the exception's code and address on an
     * unhandled exception, instead of Wine's debugger; it should write a
     * TRACE_KIND_CRASH record and TraceExit. */
    void TraceOnCrash(void (*crashed)(DWORD code, DWORD address));

#ifdef __cplusplus
}
#endif

#endif
